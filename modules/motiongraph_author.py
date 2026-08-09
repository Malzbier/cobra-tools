"""Append animation choices to a PC2 .motiongraph and its companion files.

Verified in game on Planet Coaster 2: three appended clips each get their own
State, including clips the donor graph knew nothing about, and all three appear in
the prop's animation dropdown and play when selected.

Scope of what "works" has been verified: the appended clips PLAY. Per-entry
SELECTION was broken from that script's first build until 2026-07-29 - it
resolved the enumnamer list with a bare find(".//ptrs") and no fallback, so every
appended name was written as a sibling element and silently dropped on repack.
Builds shipped 5 enum names against 8 choice rows, and in game the dropdown
ignored the selection. resolve_enum_holder plus the post-condition at the end of
append_clips exist to make that class of mismatch fail loudly instead.

The caller supplies parsed trees for the three files that must stay in step:

    .motiongraph          the graph itself - State definitions and enum branches
    .enumnamer            one name per selectable entry
    .sceneryanimchoices   one row per entry, carrying its duration and label

plus a list of ClipSpec. Writing the trees back out and emitting the loc .txt
files is left to the caller, so this module does no file I/O.

TWO CONSTRAINTS THAT ARE NOT NEGOTIABLE
---------------------------------------
1. APPEND-ONLY. Placed props store the choice INDEX, so inserting or reordering
   silently repoints every instance already placed in a park. Entries may only
   be added at the end, and existing rows must keep their index forever.

2. DEFINITIONS GET FRESH IDS; only genuinely shared structures stay refs. An
   earlier version rewrote every id= to ref=, which turned each cloned subtree
   into a reference to the DONOR - so the repointed StateOutput had nothing of
   its own to land on and every appended entry replayed the donor's clip. In
   game all three additions appeared in the dropdown and did nothing at all.
   That failure is silent; see freshen().

Note on determinism: the OVL writer does not produce byte-identical output for
identical input (observed 57983 / 57993 / 58003 bytes across three runs of the
same script). Regression-check this module on its SEMANTIC output - state count,
choice count, per-row duration and label, and the set of <mani> clip refs - and
never on a file hash or size.
"""
import copy
import logging
import os
import re
import xml.etree.ElementTree as ET

from constants import ConstantsProvider
from constants.audio_hashes import load_audio_event_hashes
from modules.formats.shared import fnv1_32

# animation_flags is a bitfield. Retail clips use 17 = 0b10001 = looping |
# flag_on_loop; clearing bit 0 gives 16, which plays once and holds the final
# pose instead of snapping back. "Turn and stay turned" - what a Rubik's cube
# needs - is only reachable with 16
FLAGS_LOOPING = 17
FLAGS_ONE_SHOT = 16

# The graph has two VariableResultEnum nodes; the one that drives the scenery
# dropdown is the one bound to this variable
LOOP_ANIM_SELECTION = "LoopAnimSelection"
LUA_RESULTS_MARKER = f'VariableName = "{LOOP_ANIM_SELECTION}"'


# The spec file the Blender addon writes and the build consumes. Bump only on a
# BREAKING change; readers reject anything they do not know
SPEC_VERSION = 1

# animation_flags values the retail corpus actually uses, plus 16
# Measured across all 506 PC2 scenery assets (620 AnimationActivity entries):
#     0 (248x)  1 (11x)  17 (343x)  49 (18x)
# 16 appears NOWHERE in retail - it is 17 with the looping bit cleared, our own
# value, and confirmed working in game. Anything outside this set is rejected rather
# than written into a graph, because a bad flag is silent: it produces a dropdown
# entry that plays wrongly instead of an error
KNOWN_FLAGS = frozenset((0, 1, 16, 17, 49))

# label_symbol is NOT required: it is generated from the target's prefix (see
# loc_symbol). enum_name is optional and defaults to the clip's short name; it
# stays accepted because one clip can legitimately back two dropdown entries
# A v1 spec carrying either is still valid - the values are ignored, not honoured,
# which is the point: a stale prefix in an old spec cannot reach the asset
_SPEC_REQUIRED = ("clip", "label_text", "loop")
SPEC_VERSIONS_READ = (1, 2)

# `label_symbol` BECOMES A FILENAME: both consumers write "<label_symbol>.txt" into
# a localisation directory. Unvalidated, a spec could therefore write outside that
# directory - "..\\..\\evil" was accepted, and `label_text` is the file's contents,
# so it was an arbitrary write with attacker-chosen data. Specs are meant to be
# shared between authors, so treating one as trusted input is wrong
#
# The charset is measured, not chosen: across the 63 PC2 assets that have a
# motiongraph, every loc symbol is [A-Za-z0-9_] (166 of 166 bar a single one with a
# space) and 121 of 122 enum names are [A-Za-z0-9]. Underscore is allowed in symbols
# because retail uses it as the separator, and in enum names because it costs nothing
# Retail's two outliers ("StaticTada!", "Angry Look") are Frontier's own strings, not
# a licence for ours
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9_]+\Z")
# Clip names additionally allow '$', PC2's asset-qualifier separator
_SAFE_CLIP = re.compile(r"\A[A-Za-z0-9_$]+\Z")

# curve_type is DERIVED from the event type, never chosen. Measured across every
# datastream entry in the retail scenery corpus; the three groups sum exactly
CURVE_TYPE_FOR = {
	"AudioEvent": 1,
	"VFXEnable": 65537,
	"VFXDisable": 65537,
	"AudioLoopingEvent": 0,
	"AudioRTPC": 0,
	"ParticleEmissionRate": 0,
	"AudioLoopingEventWithRTPC": 0,
}
# VFX events name a prefab CHILD through ds_name and carry an EMPTY location;
# audio events name a Wwise event through ds_name and place it via location,
# whose vocabulary is a locator name plus a distance suffix (bare Default /
# Close / Far is the object's own origin). Mixing the two is silent in game
_VFX_TYPES = frozenset(("VFXEnable", "VFXDisable", "ParticleEmissionRate"))


def parse_events(rows, where, game=None):
	"""Validate one clip's `events` list; returns normalised event dicts.

	Shared by every consumer so the rules cannot drift. `at` is the trigger
	position as a fraction of the clip, which is how the curve encodes timing -
	the curve itself always spans the whole clip.

	`game` is optional and only gates the CATALOGUE checks below: an audio
	event's name against the measured event-bank hash set, and a VFX event's
	optional `particle` against the measured .particleeffect list. Both are
	real, checkable offline (mod-side catalogue mining proved it - see
	constants/audio_hashes.py and constants/<game>/particles.py) and both are
	otherwise silent in game. Omitting `game`, or an unmeasured game, skips
	these checks rather than rejecting - a validator that rejects valid input
	for lack of data is worse than one that does not check at all.
	"""
	if rows is None:
		return []
	if not isinstance(rows, list):
		raise ValueError(f"{where}.events must be a list, got {type(rows).__name__}")
	out = []
	for i, e in enumerate(rows):
		at_ = f"{where}.events[{i}]"
		if not isinstance(e, dict):
			raise ValueError(f"{at_} must be an object, got {type(e).__name__}")
		for k in ("name", "type"):
			if not isinstance(e.get(k), str) or not e[k].strip():
				raise ValueError(f"{at_}.{k} must be a non-empty string")
		type_ = e["type"]
		if type_ not in CURVE_TYPE_FOR:
			raise ValueError(
				f"{at_}.type {type_!r} is not a datastream type the engine "
				f"uses; retail only has {sorted(CURVE_TYPE_FOR)}")
		if not _SAFE_NAME.match(e["name"]):
			raise ValueError(
				f"{at_}.name {e['name']!r} may only contain letters, digits "
				f"and _ - it is an engine resource name")
		# `name` on an audio event IS the Wwise event; catalogue-check it here
		# On a VFX event `name` is a prefab child the author invents (nothing to
		# check against a catalogue) - see `particle` below for the field that is
		if type_ not in _VFX_TYPES and game is not None:
			known = load_audio_event_hashes(game)
			if known is not None and fnv1_32(e["name"].lower().encode()) not in known:
				raise ValueError(
					f"{at_}.name {e['name']!r} does not hash to any known "
					f"{game} event bank entry, so it would be SILENT in game. "
					f"Check the spelling against the Wwise event list.")
		pos = e.get("at", 0.1)
		if isinstance(pos, bool) or not isinstance(pos, (int, float)):
			raise ValueError(f"{at_}.at must be a number, got {pos!r}")
		if not 0.0 <= pos <= 1.0:
			raise ValueError(
				f"{at_}.at must be within the clip (0.0 to 1.0), got {pos}")
		loc = e.get("location")
		if loc is not None and (not isinstance(loc, str) or not _SAFE_NAME.match(loc)):
			raise ValueError(
				f"{at_}.location {loc!r} may only contain letters, digits and _")
		if type_ in _VFX_TYPES:
			if loc:
				raise ValueError(
					f"{at_}: a {type_} event places itself by naming a prefab "
					f"child in `name`, so it must not set `location`")
			loc = ""
		elif loc is None:
			loc = "Default"
		# `particle` is informational only - it never reaches the graph (the
		# graph only ever stores the prefab child's name) - but it is real,
		# catalogue-checkable data the author would otherwise have to type into
		# a prefab .particleeffect reference from memory. build()'s report
		# turns it into the exact child->particle pairing to paste there
		particle = e.get("particle")
		if particle is not None:
			if type_ not in _VFX_TYPES:
				raise ValueError(
					f"{at_}: `particle` only applies to VFX events "
					f"(VFXEnable/VFXDisable/ParticleEmissionRate), not {type_}")
			if not isinstance(particle, str) or not _SAFE_NAME.match(particle):
				raise ValueError(
					f"{at_}.particle {particle!r} may only contain letters, "
					f"digits and _ - it is a .particleeffect resource name")
			if game is not None:
				known = ConstantsProvider().get(game, {}).get("particles")
				if known and particle.lower() not in known:
					raise ValueError(
						f"{at_}.particle {particle!r} is not a known "
						f".particleeffect name in {game} - check the spelling "
						f"against the particle list.")
		out.append({"name": e["name"], "type": type_, "at": float(pos),
					"location": loc, "particle": particle})
	return out

# The loc symbol is a PRIVATE key: the choice row stores "[symbol]" and the game
# resolves it against a <symbol>.txt shipped in the same OVL. Nothing outside the
# asset references it. Retail's own symbols follow no derivable rule (measured:
# none of the retail assets with choices+enum+manis match any consistent scheme);
# that is Frontier's internal history, not a contract. For a new asset we own
# both ends, so the symbol is generated and never authored
LOC_SYMBOL_ROOT = "InfoPanel_AnimationType_"


def loc_symbol(asset_name, enum_name):
    """The loc symbol for one dropdown entry. Generated, never hand-typed.

    Takes the ASSET name, not the clip prefix. They are deliberately different
    things and conflating them produced a real bug:

      * the clip prefix is read from .mani ENTRY names, which the OVL stores
        LOWERCASED ("myprop"). Clip lookup is case-insensitive, so that is
        fine for qualifying clips.
      * the loc symbol is a FILENAME and a lookup key. Deriving it from the
        lowercased clip prefix silently changed the shipped, in-game-verified
        "InfoPanel_AnimationType_MyProp_MyClip" into "..._myprop_MyClip".

    Retail also shows the two are independent - assets ship clip prefixes and
    symbol stems that disagree in case and even in name. The symbol identifies
    the ASSET; the prefix identifies the model.

    Hand-typing it was a live defect: the addon's "Loc Prefix" field defaulted to
    "InfoPanel_AnimationType_" with no asset segment, so accepting the default
    produced "InfoPanel_AnimationType_MyClip" - which any other mod with a MyClip
    clip would also claim, colliding in the game's loc namespace.
    """
    return f"{LOC_SYMBOL_ROOT}{asset_name}_{enum_name}"


def short_clip(clip):
    """The clip-local name, with any asset qualifier discarded.

    Deliberately DISCARDS rather than validates the prefix. The prefix that
    matters is the target asset's, which the build reads from the target itself -
    so whatever the author typed is not merely unnecessary, it is a chance to be
    wrong. Dropping it makes a wrong prefix unshippable instead of discouraged.
    """
    return clip.split("$", 1)[1] if "$" in clip else clip


def resolve_prefix(ovl):
    """The asset's clip prefix, read from the clips it already has.

    This is why prefix generation belongs in the build and not in Blender: when
    appending to an existing asset the prefix must match what is already there,
    and only the target knows that. Blender can guess, and a wrong guess produces
    clips the motiongraph never resolves - silent, because a dropdown entry that
    references a missing clip simply plays nothing.

    Returns None when the asset has no prefixed clips, which is the create flow.
    """
    prefixes = set()
    for name in ovl.loaders:
        if name.endswith(".mani") and "$" in name:
            prefixes.add(name.rsplit(".", 1)[0].split("$", 1)[0])
    if not prefixes:
        return None
    if len(prefixes) > 1:
        raise ValueError(
            f"asset has clips under {len(prefixes)} different prefixes "
            f"({sorted(prefixes)}); cannot tell which one new clips belong to. "
            f"Retail assets really do ship several, one per sub-model, so this "
            f"needs choosing explicitly rather than guessing.")
    return prefixes.pop()


def apply_clip_prefix(ovl, prefix, clips=None):
    """Give every bare clip in `ovl` the asset's prefix, in place.

    Blender names an Action "MyClip" and export_manis writes that verbatim
    (export_manis.py: mani_info.name = b_action.name), so a freshly exported
    asset has bare clips. The motiongraph references Prefix$Clip, so they have to
    be retargeted before anything resolves.

    Uses OvlFile.rename, which is the ONLY mechanism that works here. Verified the
    hard way - see test_manis_rename.py:
      * setting ManisInfo.name and calling ManisFile.save() CORRUPTS the file
        (6 of 7 names came back blank), because the clip names inside the .manis
        are derived from the .mani ENTRY names rather than authoritative
      * rename_contents is a clean no-op, it works at the wrong layer
      * rename on the entries propagates inward correctly

    Renames are built as EXACT per-clip pairs, using the FULL loader name
    (extension included) on both sides - never a bare stem. rename does
    substring replacement, unanchored, across every loader in the asset, so a
    short or partial pattern rewrites whatever else happens to contain it: a
    bare stem "m" matches inside "x.motiongraph", and "bend" matches inside a
    sibling clip "bendy.mani". The full name narrows that a great deal, but
    cannot close it entirely - one clip's full name can still be a genuine
    substring of another's (e.g. "a.mani" inside "aa.mani") - so the pairs are
    also checked against every other loader and against each other before
    anything is renamed, and refuse rather than risk corrupting a name that
    was never meant to change.

    Returns the list of (old, new) pairs applied.
    """
    pairs = []
    for name in sorted(ovl.loaders):
        if not name.endswith(".mani") or "$" in name:
            continue
        stem = name.rsplit(".", 1)[0]
        if clips is not None and stem.lower() not in {c.lower() for c in clips}:
            continue
        pairs.append((name, f"{prefix}${name}"))

    # rename applies every pair in sequence to the SAME string (so one pair's
    # own new name can feed into and be further rewritten by the next), and it
    # runs over every loader in the asset, not just the ones being renamed -
    # so a pair's old name must not be a substring of any OTHER loader's name,
    # nor of any other pair's new name, or that unrelated string is partially
    # rewritten too. Refuse rather than corrupt: unlike the collision below,
    # there is no way to name the "intended" outcome, only pick names that
    # cannot be renamed together as one batch
    all_strings = set(ovl.loaders) | {new for _, new in pairs}
    unsafe = []
    for old, new in pairs:
        for other in all_strings:
            if other in (old, new):
                continue
            if old in other:
                unsafe.append((old, other))
    if unsafe:
        raise ValueError(
            f"{len(unsafe)} clip rename(s) are unsafe as a batch: rename does "
            f"unanchored substring replacement across every loader, so a name "
            f"that is a substring of another gets partially rewritten too - "
            f"{', '.join(f'{o!r} is contained in {s!r}' for o, s in unsafe[:4])}"
            f"{' ...' if len(unsafe) > 4 else ''}. Rename the clashing clip(s) "
            f"under names that are not substrings of each other, or one at a "
            f"time.")

    # A .manis REPLACES only when injected under the entry name the asset already
    # uses; under any other name it lands ALONGSIDE. Export as "myprop_.manis"
    # into an asset whose manis is "animation.manisetb896236e.manis" and you get
    # both, so the asset holds seven "bend.mani" beside seven "myprop$bend.mani"
    # and this rename collides. Avoid it by exporting the .manis under the
    # entry name the target asset already uses, so it REPLACES rather than
    # coexists - verified to work, but nothing enforces it, and the natural
    # name to export under is the wrong one
    #
    # ovl.rename reports the collision as a bare AssertionError from inside its own
    # guts ("Can not rename, as new names collide with existing names"), naming no
    # clip and suggesting no action, so catch it here where the cause is known
    existing = set(ovl.loaders)
    clash = [(old, new) for old, new in pairs if new in existing]
    if clash:
        raise ValueError(
            f"{len(clash)} clip(s) would collide with clips already in the asset: "
            f"{', '.join(f'{o} -> {n}' for o, n in clash[:4])}"
            f"{' ...' if len(clash) > 4 else ''}. The asset holds a prefixed copy "
            f"of each already, which means the .manis landed ALONGSIDE the existing "
            f"one instead of replacing it. Export it under the entry name the asset "
            f"already uses (the build does this deliberately), or build from a clean "
            f"donor.")

    if pairs:
        ovl.rename(pairs)
        logging.info("prefixed %d clip(s) with %r", len(pairs), prefix)
    return pairs


def replace_clips(ovl, manis_paths):
    """Make `manis_paths` the asset's ONLY animations. Drops what was there.

    This is what creating an asset from a donor needs: a new model with its own
    clips wants ONLY those clips. The donor's are not merely surplus, they are
    wrong - they animate a skeleton this model does not have.

    Injection alone does not achieve that. A .manis replaces only when it lands
    under the entry name the asset already uses, and that name is an opaque donor
    hash ("animation.manisetb896236e.manis"). Getting it wrong is silent and leaves
    BOTH sets, which is how an asset ends up shipping the donor's animations. The
    shipping build hard-codes that hash on purpose; this removes the need to know it.

    Deliberately explicit rather than making add_files replace by default: an asset
    CAN legitimately hold several .manis - measured across 506 retail scenery
    assets, 60 have exactly one but SC_Villager_Animatronic and TY_Animatronics
    have three each and SMT_Quartet two, one per character. Silently dropping the
    others there would be data loss, so the caller says which behaviour it wants.

    Returns (removed_entry_names, added_paths).
    """
    # Remove the .manis FIRST and let it take its own clips with it: a .manis owns
    # its .mani entries, so removing it cascades. Passing both to ovl.remove in one
    # call raises KeyError on the children, which are gone by the time it reaches
    # them. Then sweep any .mani left without a parent
    removed = []
    containers = sorted(n for n in ovl.loaders if n.endswith(".manis"))
    if containers:
        ovl.remove(containers)
        removed.extend(containers)
    orphans = sorted(n for n in ovl.loaders if n.endswith(".mani"))
    if orphans:
        ovl.remove(orphans)
        removed.extend(orphans)
    if removed:
        logging.info("replace_clips: removed %d animation entrie(s) (%d container, "
                     "%d clip)", len(removed), len(containers), len(orphans))
    old = removed
    paths = [manis_paths] if isinstance(manis_paths, str) else list(manis_paths)
    for p in paths:
        ovl.add_files([p], os.path.dirname(p))
    logging.info("replace_clips: added %s", [os.path.basename(p) for p in paths])
    return old, paths


def check_prefix_consistent(ovl, prefix):
    """Every clip in the asset sits under `prefix`. Raises if not.

    A mismatch here is silent in game - the dropdown entry exists and plays
    nothing - so it has to fail the build instead.
    """
    bad = [n for n in ovl.loaders
           if n.endswith(".mani") and not n.lower().startswith(f"{prefix.lower()}$")]
    if bad:
        raise ValueError(
            f"{len(bad)} clip(s) are not under the asset prefix {prefix!r}: "
            f"{sorted(bad)[:5]}{' ...' if len(bad) > 5 else ''}")


def parse_spec(payload, game=None):
    """Validate an animation spec dict and return its normalised clip rows.

    Raises ValueError with a message naming the offending entry. The build script
    used to index the payload directly, so a spec missing a key produced a bare
    KeyError with no indication of WHICH entry, and an out-of-range
    animation_flags was written into the graph unchecked.

    Deliberately lives here rather than in the build script: the Blender addon and
    the build both need the same rules, and two copies would drift.

    Note `duration` is validated if present but is NOT authoritative - the build
    reads the real duration from the .manis and uses the spec's value only to detect
    drift. Hand-typed durations were wrong on every entry when they were trusted.

    `game` is forwarded to parse_events for its catalogue checks; see there.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"spec must be a JSON object, got {type(payload).__name__}")
    got = payload.get("spec_version")
    if got not in SPEC_VERSIONS_READ:
        raise ValueError(
            f"unsupported spec_version {got!r}, this reader handles "
            f"{', '.join(str(v) for v in SPEC_VERSIONS_READ)}")

    clips = payload.get("clips")
    if clips is None:
        raise ValueError("spec has no 'clips' key")
    if not isinstance(clips, list):
        raise ValueError(f"'clips' must be a list, got {type(clips).__name__}")
    if not clips:
        raise ValueError("spec has an empty 'clips' list - nothing to append. "
                         "Building would silently succeed and change nothing.")

    rows = []
    seen_enum, seen_symbol = {}, {}
    for i, c in enumerate(clips):
        where = f"clips[{i}]"
        if not isinstance(c, dict):
            raise ValueError(f"{where} must be an object, got {type(c).__name__}")
        missing = [k for k in _SPEC_REQUIRED if k not in c]
        if missing:
            raise ValueError(f"{where} is missing {', '.join(missing)}")

        for k in ("clip", "label_text"):
            v = c[k]
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{where}.{k} must be a non-empty string, got {v!r}")
        if "enum_name" in c and (not isinstance(c["enum_name"], str)
                                 or not c["enum_name"].strip()):
            raise ValueError(
                f"{where}.enum_name must be a non-empty string, got {c['enum_name']!r}")

        # clip becomes an identifier in the graph; enum_name (explicit or
        # defaulted below) becomes the tail of a generated loc symbol, which
        # becomes a FILENAME. label_text is exempt: it is prose shown to the
        # player and is only ever file CONTENT, never a path
        v = c["clip"]
        if not _SAFE_CLIP.match(v):
            raise ValueError(
                f"{where}.clip {v!r} may only contain letters, digits, _ and "
                f"$. This is not cosmetic: it becomes part of the name of a "
                f"localisation file, so a separator or '..' here would write "
                f"outside the localisation directory.")

        # `loop` is a plain bool; the caller maps it to animation_flags (17/16)
        # The spec deliberately does NOT carry the bitfield - see export_animspec
        loop = c["loop"]
        if not isinstance(loop, bool):
            raise ValueError(f"{where}.loop must be true or false, got {loop!r}")

        # v2: `weight` places the row in the Auto choice's random pool. Absent
        # means weight 1; null opts the row out of the pool entirely
        weight = c.get("weight", 1)
        if weight is not None:
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError(f"{where}.weight must be a number or null, "
                                 f"got {weight!r}")
            if weight <= 0:
                raise ValueError(f"{where}.weight must be > 0, got {weight}")

        dur = c.get("duration")
        if dur is not None:
            if isinstance(dur, bool) or not isinstance(dur, (int, float)):
                raise ValueError(f"{where}.duration must be a number, got {dur!r}")
            if dur <= 0:
                raise ValueError(f"{where}.duration must be > 0, got {dur}")

        # Normalise here, once: any asset qualifier the author typed is DISCARDED,
        # and the real prefix is applied later from the target. A v1 spec carrying
        # "OldAsset$MyClip" therefore cannot drag a stale prefix into a new asset
        short = short_clip(c["clip"])
        if not short:
            raise ValueError(f"{where}.clip {c['clip']!r} has no name after the '$'")
        enum = c.get("enum_name") or short

        # Checked on the FINAL value, not on c.get("enum_name") before the
        # default applies: short_clip only strips the FIRST '$', so a clip
        # like "A$B$C" defaults to "B$C" - a defaulted enum_name is exactly as
        # much a loc-symbol tail, and exactly as much a filename, as an
        # explicit one, and skipping this for the defaulted case was the bug
        if not _SAFE_NAME.match(enum):
            raise ValueError(
                f"{where}.enum_name {enum!r} may only contain letters, digits "
                f"and _. This is not cosmetic: it becomes part of the name of "
                f"a localisation file, so a separator or '..' here would "
                f"write outside the localisation directory."
                + ("" if "enum_name" in c else
                   f" (defaulted from clip {c['clip']!r} - give an explicit "
                   f"enum_name instead)"))

        # APPEND-ONLY means index is identity, so a duplicate enum_name is
        # ambiguous rather than merely untidy - two rows would claim the same
        # entry. The same CLIP twice is fine and deliberate (a looping entry and a
        # one-shot entry over one clip), which is exactly why enum_name is the key
        # checked here and the clip is not
        if enum in seen_enum:
            raise ValueError(
                f"{where}.enum_name {enum!r} duplicates clips[{seen_enum[enum]}]"
                + ("" if "enum_name" in c else
                   f" - both defaulted to the clip name {short!r}, so give one of "
                   f"them an explicit enum_name"))
        seen_enum[enum] = i

        rows.append({
            "clip": short,          # bare; the build qualifies it
            "enum_name": enum,
            "label_text": c["label_text"],
            "loop": loop,
            "duration": dur,
            "weight": weight,
            # v2: audio/VFX events to fire during this clip. Both consumers arm
            # them now - the generator emits into the fresh state, append_clips
            # strips the donor's inherited streams first and then emits these
            "events": parse_events(c.get("events"), where, game),
        })

    # v2 payload-level fields, validated here so every consumer gets the same
    # rules; consumers read them off the payload after this returns
    bt = payload.get("blend_time")
    if bt is not None:
        if isinstance(bt, bool) or not isinstance(bt, (int, float)) or bt <= 0:
            raise ValueError(f"blend_time must be a number > 0, got {bt!r}")
    fam = payload.get("family")
    if fam is not None and (not isinstance(fam, str) or not fam.strip()):
        raise ValueError(f"family must be a non-empty string, got {fam!r}")
    al = payload.get("auto_label")
    if al is not None and (not isinstance(al, str) or not al.strip()):
        raise ValueError(f"auto_label must be a non-empty string, got {al!r}")
    return rows


def load_spec(path, game=None):
    """parse_spec from a file, with readable errors for missing/malformed JSON."""
    import json
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"spec file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"spec file is not valid JSON ({path}): {e}")
    return parse_spec(payload, game)


def durations_from_manis(manis_file):
    """{clip name lowercased: duration in seconds} for every clip in a ManisFile.

    Lowercased because the manis stores clip names folded to lower case while the
    graph references them with their original casing. Matching on the raw string
    silently finds nothing, which looks exactly like a clip that is not there.
    """
    return {getattr(mi, "name", "").lower(): getattr(mi, "duration", None)
            for mi in manis_file.mani_infos}


def choice_duration(clip_duration):
    """The value to write into a .sceneryanimchoices row.

    This is NOT what drives playback - the clip plays at its own length. It is
    what the trigger/sequencer layer consumes: TriggerTargetMotionGraph.Duration,
    GetPartTriggerDuration and the sequencer timeline's playDurationSeconds. So
    it decides how long a block occupies the timeline, and a value shorter than
    the clip makes the next sequenced event fire while the animation is still
    running.

    Retail rounds to one decimal (7.9667 -> 8.0, 3.3333 -> 3.3), which is a
    hand-authoring artifact rather than a constraint: the field is a float, and
    rounding buys nothing while introducing up to one frame of drift. We write
    the exact length.

    The floor is real though. Retail declares a 2-frame Static (0.0333 s) as
    exactly 1.0 across every asset sampled - a sub-frame block is unusable on a
    timeline UI - so degenerate clips get the same treatment.
    """
    if clip_duration is None:
        raise ValueError("clip has no duration")
    return max(1.0, clip_duration)


def duration_from_frames(frame_count, fps):
    """Clip duration in seconds, matching what the manis exporter already writes.

    Kept identical to plugin/export_manis.py on purpose. Authoring durations by
    hand duplicates a number the exporter already derives, and the two were
    drifting: a 90-frame clip at 24 fps was being hand-written as 3.75 (90/24)
    while the exporter stores 3.708 (89/24). One frame, two sources of truth.
    """
    return (frame_count - 1) / fps


class ClipSpec:
    """One appended dropdown entry.

    clip          the clip reference as it appears in <mani>, i.e. Asset$Clip
    enum_name     appended to the enumnamer
    label_symbol  loc symbol; the choice row stores "[symbol]" and the game
                  resolves it against a shipped <symbol>.txt
    label_text    what the player sees in the dropdown
    duration      seconds; prefer duration_from_frames() over a literal
    loop          False emits FLAGS_ONE_SHOT
    events        audio/VFX events to arm on this clip, in parse_events()'s
                  normalised shape. append_clips emits these directly - no
                  retail curve to clone, see append_datastreams.
    weight        random-pool weight for a GENERATED graph's Auto choice;
                  None opts out. append_clips does not read it - only the
                  generator's Auto choice means anything by it - it rides
                  along here because this is also the carrier the Blender
                  exporter fills to build an animspec JSON, and the JSON
                  schema (parse_spec) has one weight per clip regardless of
                  which path later consumes the file.
    """

    def __init__(self, clip, enum_name, label_symbol, label_text, duration, loop=True,
                 events=None, weight=1):
        self.clip = clip
        self.enum_name = enum_name
        self.label_symbol = label_symbol
        self.label_text = label_text
        self.duration = duration
        self.loop = loop
        self.events = list(events or ())
        self.weight = weight

    @property
    def animation_flags(self):
        return FLAGS_LOOPING if self.loop else FLAGS_ONE_SHOT

    @classmethod
    def from_clip(cls, clip, label, duration, loop=True, prefix=None,
                  enum_name=None, asset_name=None, events=None, weight=1):
        """Build a spec from the clip name plus the TARGET's prefix.

        `clip` may arrive bare ("MyClip") or already qualified ("Old$MyClip"); any
        qualifier is discarded and `prefix` applied, so a stale or mistyped prefix
        cannot reach the asset. See short_clip for why discarding beats validating.

        `enum_name` stays authorable because it is genuine intent, not a name that
        must agree with another name: one clip can legitimately back two dropdown
        entries (a looping "MyClip" and a one-shot "MyClipOnce"), and retail does
        this too - an enum entry can cover several clips. It defaults to the clip's
        short name.

        The loc symbol is always generated. It is never a parameter.
        """
        short = short_clip(clip)
        if not short:
            raise ValueError(f"clip name {clip!r} has no name after the '$'")
        enum = enum_name or short
        if prefix is None:
            raise ValueError(
                f"no asset prefix for clip {short!r} - it must come from the "
                f"target asset (resolve_prefix), not from the author")
        # asset_name defaults to the clip prefix for callers that have only one,
        # but a caller with the real asset name should pass it: the prefix comes
        # from lowercased entry names, and the symbol is a filename
        return cls(f"{prefix}${short}", enum,
                   loc_symbol(asset_name or prefix, enum), label, duration, loop,
                   events=events, weight=weight)

    @classmethod
    def unqualified(cls, clip, label, duration, loop=True, enum_name=None,
                     events=None, weight=1):
        """A spec that states intent but names no target asset.

        This is what the Blender addon produces: it knows the Action, the label and
        whether it loops, and deliberately does NOT know the asset prefix or the
        loc symbol. Both are filled in by the build against the real target.

        `clip` is the bare clip-local name and `label_symbol` is None. Passing one
        of these to append_clips is a programming error, not a user error - the
        build must qualify it first - so the emitter asserts rather than guessing.
        """
        short = short_clip(clip)
        if not short:
            raise ValueError(f"clip name {clip!r} has no name after the '$'")
        return cls(short, enum_name or short, None, label, duration, loop,
                   events=events, weight=weight)

    @property
    def is_qualified(self):
        """True once a target prefix has been applied."""
        return "$" in self.clip and self.label_symbol is not None

    def qualify(self, prefix, asset_name=None):
        """Return a copy bound to `prefix`, with its loc symbol generated.

        `asset_name` names the symbol and defaults to `prefix`; see loc_symbol for
        why the two are not interchangeable.
        """
        return ClipSpec(f"{prefix}${short_clip(self.clip)}", self.enum_name,
                        loc_symbol(asset_name or prefix, self.enum_name),
                        self.label_text, self.duration, self.loop, self.events)

    def __repr__(self):
        return (f"ClipSpec({self.clip!r}, dur={self.duration}, "
                f"flags={self.animation_flags})")


def find_enum_nodes(graph_root):
    """EVERY mrfmember1 whose lua_method is VariableResultEnum and whose bound
    variable is LoopAnimSelection.

    Retail scenery graphs have TWO, and an earlier version of this function
    returned only the first with the comment "the other one is not the dropdown".
    That was wrong. Both are the same dropdown, sitting on the two branches of the
    IsSequenceTriggerControlled bool above them:

        branch 0   continuous loop playback
        branch 1   trigger-sequenced "play once every N seconds"

    Extending only the first leaves the timed mode on retail's branch set, so a
    newly appended entry falls off the end and the graph drops back to that node's
    branch 0 - a random sequence over the donor's clips. In game that reads as
    "the timed mode ignores my selection and always plays the same thing".
    """
    out = []
    for el in graph_root.iter():
        lm = el.find("./lua_method")
        if lm is None or (lm.text or "").strip() != "MotionGraph.VariableResultEnum":
            continue
        for tn in el.iter("target_name"):
            if (tn.text or "").strip() == LOOP_ANIM_SELECTION:
                out.append(el)
                break
    return out


def find_enum_node(graph_root):
    """First enum node only. Kept for callers that just want to locate the
    dropdown; anything that MUTATES must use find_enum_nodes and handle all."""
    nodes = find_enum_nodes(graph_root)
    return nodes[0] if nodes else None


def freshen(sub, next_id):
    """Give every DEFINITION inside `sub` a brand-new id and follow any ref that
    pointed at one of them. Returns the next free id.

    Refs pointing OUTSIDE the subtree are left alone - those are genuinely
    shared structures and must stay shared. See constraint 2 in the module
    docstring for what happens when this is inverted.
    """
    idmap = {}
    for el in sub.iter():
        old = el.get("id")
        if old is not None:
            idmap[old] = str(next_id)
            el.set("id", str(next_id))
            next_id += 1
    for el in sub.iter():
        r = el.get("ref")
        if r is not None and r in idmap:
            el.set("ref", idmap[r])
    return next_id


def _next_free_id(graph_root):
    ids = [int(e.get("id")) for e in graph_root.iter() if (e.get("id") or "").isdigit()]
    return max(ids) + 1


def _append_lua_result(lua_el):
    """Widen the LoopAnimSelection entry in lua_results by one ResultParams slot.

    The node's declared result count and the number of enum branches must agree;
    desyncing them is one of the ways the engine rejects a graph outright.

    An earlier version located the insertion point with `head.find("},", rp)`,
    which finds the close of the FIRST slot rather than the end of the table. It
    therefore nested each new slot inside `[1]` and reused the same index every
    time, producing

        ResultParams = { [1] = {   [6] = {   [6] = {   [6] = {  }, }, }, }, [2] ...

    while the declared count stayed at retail's five. Brace-match instead, and
    count only TOP-LEVEL slots.
    """
    src = lua_el.text
    marker = src.find(LUA_RESULTS_MARKER)
    if marker < 0:
        raise ValueError(f"{LOOP_ANIM_SELECTION} entry not found in lua_results")
    rp = src.rfind("ResultParams = {", 0, marker)
    if rp < 0:
        raise ValueError("no ResultParams for the LoopAnimSelection entry")
    open_brace = rp + len("ResultParams = {") - 1

    depth, close = 0, None
    for j in range(open_brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                close = j
                break
    if close is None:
        raise ValueError("unbalanced braces in lua_results ResultParams")

    n, d2 = 0, 0
    for ch in src[open_brace + 1:close]:
        if ch == "{":
            d2 += 1
        elif ch == "}":
            d2 -= 1
        elif ch == "[" and d2 == 0:
            n += 1

    lua_el.text = src[:close] + f" [{n + 1}] = {{  }}, " + src[close:]


def _count_lua_result_slots(lua_el):
    """Top-level ResultParams slots on the LoopAnimSelection entry, or None."""
    src = lua_el.text or ""
    marker = src.find(LUA_RESULTS_MARKER)
    rp = src.rfind("ResultParams = {", 0, marker) if marker >= 0 else -1
    if rp < 0:
        return None
    open_brace = rp + len("ResultParams = {") - 1
    depth, close = 0, None
    for j in range(open_brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                close = j
                break
    if close is None:
        return None
    n, d2 = 0, 0
    for ch in src[open_brace + 1:close]:
        if ch == "{":
            d2 += 1
        elif ch == "}":
            d2 -= 1
        elif ch == "[" and d2 == 0:
            n += 1
    return n


def resolve_enum_holder(enum_el):
    """The element whose CHILDREN are the enum name entries.

    Accepts either that element or the .enumnamer root, because getting this
    wrong fails silently and expensively. The two shapes seen in PC2 are

        <EnumnamerRoot><strings><pointer>Name</pointer>...
        <...><ptrs><pointer>Name</pointer>...

    A caller that resolved this with a single `find(".//ptrs")` and no fallback
    got the ROOT back, so every appended name became a sibling <strings> element
    instead of a <pointer> inside the list. The OVL writer serialises only the
    genuine list, so the additions vanished on repack, the dropdown kept the
    donor's name count, and the selection resolved to nothing in game.
    """
    for path in (".//ptrs", "./strings"):
        found = enum_el.find(path)
        if found is not None:
            return found
    # already the holder? only if its children look like name entries
    kids = list(enum_el)
    if kids and all(k.tag not in ("strings", "ptrs") for k in kids):
        return enum_el
    raise ValueError(
        f"cannot locate the enum name list under <{enum_el.tag}> - "
        f"children are {[k.tag for k in kids]}")


def append_clips(graph_root, enum_holder, choices_root, specs):
    """Append one dropdown entry per ClipSpec. Mutates all three trees in place.

    `enum_holder` may be the .enumnamer root or the list element itself; see
    resolve_enum_holder.

    Returns a list of dicts describing what was added - index, state id, branch
    count_0, and the loc symbol/text the caller still has to write out.
    """
    _check_specs_qualified(specs)
    enum_holder = resolve_enum_holder(enum_holder)
    n_enum_before = len(list(enum_holder))

    # Refuse to append a name the asset ALREADY has. Append-only means a duplicate
    # cannot be removed afterwards, so a re-run of the same spec would permanently
    # corrupt the asset - and silently: the result stays internally consistent
    # (names, rows, branches and lua slots all agree), so every post-condition below
    # still passes. Found by running the animspec CLI twice on one OVL, which
    # produced the same enum names at both 5-7 and 8-10
    existing = {(e.text or "").strip() for e in enum_holder}
    clashing = [s.enum_name for s in specs if s.enum_name in existing]
    if clashing:
        raise ValueError(
            f"already present in this asset: {', '.join(sorted(set(clashing)))}. "
            f"Entries are APPEND-ONLY because placed props store the choice index, "
            f"so re-adding a name would duplicate it with no way to remove it. "
            f"Append only the NEW clips, or start from the unmodified asset.")
    # `existing` only ever catches a name repeated against the asset's PAST
    # entries - it is a snapshot taken once, before any of `specs` is applied,
    # so two specs in the SAME call sharing a name pass it just as silently:
    # every post-condition below still agrees, because both really do land
    seen, dup_within = set(), []
    for s in specs:
        if s.enum_name in seen:
            dup_within.append(s.enum_name)
        seen.add(s.enum_name)
    if dup_within:
        raise ValueError(
            f"duplicate enum_name(s) within this call: "
            f"{', '.join(sorted(set(dup_within)))}. Two specs cannot share one "
            f"name - the entries are append-only, so a duplicate could never be "
            f"told apart or removed afterward.")
    soe = graph_root.find("./state_output_entries/states")
    if soe is None:
        raise ValueError("state_output_entries/states not found in graph")
    nodes = find_enum_nodes(graph_root)
    if not nodes:
        raise ValueError(f"no VariableResultEnum node bound to {LOOP_ANIM_SELECTION}")
    branch_counts_before = [len(n.find("./children").findall("./mrfchild"))
                            for n in nodes]
    lua = graph_root.find("./lua_results")
    if lua is None or not lua.text:
        raise ValueError("graph has no lua_results")
    if LUA_RESULTS_MARKER not in lua.text:
        raise ValueError(f"{LOOP_ANIM_SELECTION} entry not found in lua_results")

    added = []
    for spec in specs:
        next_id = _next_free_id(graph_root)

        # --- define the State inside state_output_entries, as retail does -----
        # A State is DEFINED once here and REFERENCED from the branch that plays
        # it; that is the structure retail's own graphs use
        donor = next((sr for sr in soe.findall("./statereference")
                      if sr.find("./state") is not None
                      and sr.find("./state").get("id") is not None
                      and sr.find(".//mani") is not None), None)
        if donor is None:
            raise ValueError("no full State definition to copy")
        new_sr = copy.deepcopy(donor)
        new_state = new_sr.find("./state")
        # the state gets its own definitions too, so editing its <mani> and
        # animation_flags cannot bleed back into the donor state
        #
        # new_id is read back from new_state AFTER freshen, never assigned
        # separately: freshen walks sub.iter(), which yields new_state itself
        # first (it is the root of the subtree, and every donor state has an
        # id by construction - see the donor selection above), so it is
        # always the first id freshen allocates. Assigning a DIFFERENT id here
        # afterward - which this used to do - overwrote what freshen had just
        # given the state, orphaning any ref inside the subtree that pointed
        # at the donor's original id: freshen rewrote it to point at the id
        # the state briefly held, not the one it ends up with
        next_id = freshen(new_state, next_id)
        new_id = new_state.get("id")
        new_state.find(".//mani").text = spec.clip
        new_state.find(".//data").set("animation_flags", str(spec.animation_flags))
        # A State cloned from a donor brings the donor's AdditionalDataStreams
        # with it, so without stripping first, the new clip fires the donor's
        # sounds on its own timing - every audio and VFX event on the donor
        # state rides along to the appended clip. Arm the SPEC's own events
        # only after that, so they fill the empty list rather than joining
        # the donor's
        n_ds = strip_datastreams(new_state)
        if n_ds:
            logging.info(f"  stripped {n_ds} inherited datastream(s) from {spec.clip}")
        if spec.events:
            # bone_i_d points at the state's OWN sync_prop_through_variable id,
            # not an actual bone - measured on every armed retail entry. freshen()
            # already gave this state's definitions fresh ids, so this is the new
            # state's id, not the donor's
            sync = new_state.find(".//sync_prop_through_variable")
            if sync is None or sync.get("id") is None:
                raise ValueError(
                    f"{spec.clip}: state has no defined "
                    f"sync_prop_through_variable to bind events to")
            n_ev = append_datastreams(new_state, spec.events, sync.get("id"))
            logging.info(f"  armed {n_ev} event(s) on {spec.clip}")
        soe.append(new_sr)

        # --- clone a branch in EVERY enum node, pointing at that one State ----
        # Both enum nodes select from the same dropdown, so both need a branch at
        # the new index. Retail does exactly this: its state 12 is referenced from
        # branch 2 of BOTH nodes. Extending one only would leave the other mode on
        # the donor's branch set
        for node in nodes:
            children = node.find("./children")
            branches = children.findall("./mrfchild")
            clone = copy.deepcopy(branches[-1])
            next_id = freshen(clone, next_id)
            clone.set("count_0", str(len(branches) + 1))
            next_id = _repoint_state_outputs(clone, new_id, next_id)
            children.append(clone)

        # --- lua_results / enumnamer / choices --------------------------------
        _append_lua_result(lua)

        names = list(enum_holder)
        ET.SubElement(enum_holder, names[-1].tag).text = spec.enum_name

        rows = [e for e in choices_root.iter() if e.get("index") is not None]
        newrow = copy.deepcopy(rows[-1])
        newrow.set("index", str(int(rows[-1].get("index")) + 1))
        # choice_duration is what already knows None is invalid ("clip has no
        # duration") and applies the sub-frame floor - writing spec.duration
        # directly skipped both, and duration is optional in the spec schema,
        # so a missing one reached the sequencer's own playDurationSeconds as
        # the literal text "None"
        newrow.set("duration", str(choice_duration(spec.duration)))
        lbl = newrow.find("./label")
        # the copied label still carries the donor's share id; leaving it would
        # make this row an alias of the donor's string instead of its own
        for a in ("id", "ref"):
            lbl.attrib.pop(a, None)
        lbl.text = f"[{spec.label_symbol}]"
        {c: p for p in choices_root.iter() for c in p}[rows[-1]].append(newrow)
        choices_root.set("count", str(len(rows) + 1))

        added.append({
            "clip": spec.clip,
            "index": newrow.get("index"),
            "state_id": new_id,
            "branches": [n.find("./children").findall("./mrfchild")[-1].get("count_0")
                         for n in nodes],
            "flags": spec.animation_flags,
            "label_symbol": spec.label_symbol,
            "label_text": spec.label_text,
        })
        logging.info(f"+ {spec.clip} State id={new_id} index={newrow.get('index')}")

    _check_post_conditions(nodes, branch_counts_before, enum_holder,
                           n_enum_before, choices_root, soe, lua, len(specs))
    return added


def strip_datastreams(state_el):
    """Drop every audio/VFX event inherited by a cloned State. Returns the count.

    A State cloned from a donor brings the donor's `AdditionalDataStreams` with it,
    so an appended clip fires the donor's sounds on its own timing - every audio
    and VFX event the donor state carried rides along to every new clip unless
    stripped.

    Removing them is preferable to setting the `SuppressResourceDataStreams` flag
    bit: the corpus census found that bit set in ZERO of 506 retail assets, so it is
    untested territory, whereas an empty list is what a clip with no events looks
    like. Authoring our own events later means filling this list rather than
    inheriting it.

    Safe with respect to string aliasing because `freshen` has already given the
    clone's own definitions new ids, so nothing outside the subtree refs into it.
    """
    removed = 0
    for lst in list(state_el.iter("additional_data_streams")):
        for child in list(lst):
            lst.remove(child)
            removed += 1
    return removed


# The event curve is a STEP, and needs no retail template to clone. Measured
# across every armed, in-game-verified entry: the curve always spans the whole
# clip (x 0.0 -> 1.0), y is 16384 before the trigger and 16448 from it onward,
# and both subsequent_curve_param fields are 16384 - identical for audio and
# VFX, which differ only in curve_type, location, and where the step sits
# Mirrors motiongraph_generator's _Y_OFF/_Y_ON/_SCP; ElementTree here instead
# of string emission because this operates on an already-parsed graph tree
_EVENT_Y_OFF, _EVENT_Y_ON, _EVENT_SCP = 16384, 16448, 16384


def _event_curve_point(x, y):
    p = ET.Element("curvedatapoint")
    p.set("x", repr(x))
    p.set("y", str(y))
    p.set("sub_curve_type", "SubCurveType.CONSTANT")
    p.set("subsequent_curve_param", str(_EVENT_SCP))
    p.set("subsequent_curve_param_b", str(_EVENT_SCP))
    return p


def append_datastreams(state_el, events, bone_ref):
    """Arm `events` (parse_events()'s shape) on a State. Returns the count.

    Call AFTER strip_datastreams, on the same state - this fills the empty
    list strip_datastreams leaves rather than fighting inherited content.
    `bone_ref` is the state's own sync_prop_through_variable id: every armed,
    in-game-verified entry points bone_i_d at that id, not at an actual bone.
    """
    if not events:
        return 0
    lst = state_el.find(".//additional_data_streams")
    if lst is None:
        raise ValueError("state has no additional_data_streams to fill")
    holder = ET.SubElement(lst, "data_stream_resource_data")
    holder.set("pool_type", "3")
    for e in events:
        entry = ET.SubElement(holder, "datastreamresourcedata")
        entry.set("curve_type", str(CURVE_TYPE_FOR[e["type"]]))
        ET.SubElement(entry, "ds_name").text = e["name"]
        ET.SubElement(entry, "type").text = e["type"]
        ET.SubElement(entry, "bone_i_d").set("ref", str(bone_ref))
        # empty stays None (self-closing <location />, retail's own shape for a
        # VFX event) rather than an explicit "" text node
        ET.SubElement(entry, "location").text = e["location"] or None
        curve = ET.SubElement(entry, "curve")
        curve.set("count", "3")
        points = ET.SubElement(curve, "points")
        points.set("pool_type", "3")
        points.append(_event_curve_point(0.0, _EVENT_Y_OFF))
        points.append(_event_curve_point(float(e["at"]), _EVENT_Y_ON))
        points.append(_event_curve_point(1.0, _EVENT_Y_ON))
    return len(events)


def _repoint_state_outputs(clone, new_id, next_id):
    """Point every StateOutput in a cloned branch at `new_id`."""
    for m in clone.iter():
        lm = m.find("./lua_method")
        if lm is None or (lm.text or "").strip() != "MotionGraph.StateOutput":
            continue
        p0, mv = m.find("./ptr_0"), m.find("./motiongraph_vars")
        # ptr_0 (+16) and motiongraph_vars (+24) are always the same target on
        # action nodes, so both must carry the SAME link (spec 3.4)
        if p0 is not None:
            p0.attrib.pop("raw", None)
            p0.set("ref", new_id)
        if mv is not None:
            mv.set("ref", new_id)
    return next_id


def _check_specs_qualified(specs):
    """Every spec names a target asset before it reaches the graph.

    An unqualified spec carries a bare clip ("MyClip") and no loc symbol. Writing
    one would produce a <mani> reference the graph never resolves and a choice row
    labelled "[None]" - both silent in game. This is a programming error in the
    build, not something a user typed, so it fails loudly here.
    """
    bad = [s.clip for s in specs if not s.is_qualified]
    if bad:
        raise ValueError(
            f"{len(bad)} spec(s) were never bound to a target asset: {bad[:5]}. "
            f"Call ClipSpec.qualify(prefix) with the prefix from resolve_prefix() "
            f"before appending.")


def _check_post_conditions(nodes, branch_counts_before, enum_holder,
                           n_enum_before, choices_root, soe, lua, n_specs):
    """Everything that indexes the dropdown positionally must agree.

    These files reference each other by POSITION, so a count mismatch is not a
    crash, it is a dropdown entry that plays something other than its label. Every
    check here corresponds to a defect that shipped and was found in game rather
    than at build time, so none of them are hypothetical.
    """
    n_enum = len(list(enum_holder))
    n_choices = int(choices_root.get("count"))
    n_states = len(soe.findall("./statereference"))

    if n_enum != n_enum_before + n_specs:
        raise ValueError(
            f"enum names went {n_enum_before} -> {n_enum}, expected "
            f"+{n_specs}; the appends did not land in the name list")
    if n_enum != n_choices:
        raise ValueError(
            f"enum names ({n_enum}) != choice rows ({n_choices}); the dropdown "
            f"and the name list would disagree")

    for i, (node, before) in enumerate(zip(nodes, branch_counts_before)):
        now = len(node.find("./children").findall("./mrfchild"))
        if now != before + n_specs:
            raise ValueError(
                f"enum node {i} branches went {before} -> {now}, expected "
                f"+{n_specs}")
        if now != n_choices:
            raise ValueError(
                f"enum node {i} has {now} branches but there are {n_choices} "
                f"choice rows; the modes would disagree about what index means")

    n_slots = _count_lua_result_slots(lua)
    if n_slots != n_choices:
        raise ValueError(
            f"lua_results declares {n_slots} ResultParams slots but there are "
            f"{n_choices} choice rows / enum branches")

    logging.info(f"append_clips OK: {n_choices} rows / {n_enum} names / "
                 f"{[len(n.find('./children').findall('./mrfchild')) for n in nodes]} "
                 f"branches / {n_slots} lua slots / {n_states} states")
