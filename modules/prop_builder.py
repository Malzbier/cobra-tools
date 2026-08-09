"""Assemble an animated scenery prop asset: a fresh container, CREATED - not
cloned - with generated motiongraph/enumnamer/choice table.

    from modules.prop_builder import build, read_animspec
    report = build(cobra_dir=..., asset="MyProp", art_dir=..., dest_dir=...,
                   choices=[...], blend_time=0.15)

Nothing here starts from an existing asset. Every entry - the mesh, the
materials, the motiongraph, the enumnamer, the choice table, the vars entry -
is either supplied by the caller as a file in art_dir or generated from the
choice spec (modules.motiongraph_generator). Reusing content from an existing
asset (materials, a mesh, anything else) is a DIFFERENT, explicit operation -
inject files into a chosen OVL, or rename one - and belongs outside this
function, on whichever container the caller actually wants to modify. This
function only ever produces a new one.

Materials are optional and decoupled from the mesh, matching how the rest of
the toolchain already treats them: exporting a mesh writes only material
NAMES into it (plugin/modules_export/material.py), and a matching .fgm is a
separate, independent export action per material. A mesh with no matching
.fgm in art_dir is not an error here; it becomes an unbound-material failure
below, the same way it would show up unlit in game.

The build fails loudly on the failure modes that are SILENT in game:
  * a clip the graph references but the manis does not provide
  * a material name with no .fgm to bind to (renders unlit white)
  * bare clip names (a dropdown entry that plays nothing)
  * a vars_ref naming an entry neither supplied nor generated

Deliberately NOT here: loc .txt emission, prefab lua, registration, deploy -
those belong to the caller's mod project. The report returns the label texts
so the caller can write them wherever its loc pipeline lives.

Choice rows are (enum_name, clip, weight, label); choice 0 must be the
Auto/random choice (clip None). The first clip-bearing choice is the REST
state. Choice-table durations are read from the built asset's .manis - never
typed - and padded by 2x blend_time so a sequencer block covers the blend
round trip (motion is blend + clip + blend; only the clip fits in a
bare-duration block).
"""
import contextlib
import glob
import io
import json
import os
import re
import shutil
import tempfile

from generated.formats.manis import ManisFile
from generated.formats.ovl import OvlFile
from modules.formats.shared import DummyReporter
from modules.motiongraph_author import (apply_clip_prefix,
                                        check_prefix_consistent, loc_symbol,
                                        parse_spec)
from modules.motiongraph_generator import generate, motiongraphvars_xml
from utils import config


class PropBuildError(Exception):
    pass


# A loc symbol becomes a filename in write_loc_texts; same charset the spec
# layer enforces on the enum names these are built from
_SAFE_LOC_SYMBOL = re.compile(r"\A[A-Za-z0-9_]+\Z")


class _RaisingSignal:
    """A DummySignal that actually does something with what it is given.

    Matches DummySignal's interface (emit/connect) so it drops into the same
    slot the GUI's Reporter class fills with a real pyqtSignal - neither
    subclass touches report_error_files itself; DummyReporter's own version
    already collects failures and calls warning_msg.emit((summary, details))
    when any exist. The GUI makes that visible by wiring a Qt signal to a
    dialog; this makes it visible by raising.
    """

    def emit(self, val):
        msg, details = val
        raise PropBuildError(f"{msg}\n{details}" if details else msg)

    def connect(self, func):
        pass


class _RaisingReporter(DummyReporter):
    """The OvlFile reporter for every build() step: turns the per-file create()
    failures that add_files already collects (but a bare DummyReporter
    discards, since its warning_msg is a no-op) into a real PropBuildError,
    naming the files that failed instead of shipping an asset with a
    silently missing entry."""

    def __init__(self):
        self.warning_msg = _RaisingSignal()


def read_animspec(path, auto_label_default="Auto", game=None):
    """(choices, blend_time_or_None) from an exported animation spec.

    ONE reader: validation - including the charset rules that stop an enum
    name from becoming a path traversal when it turns into a loc filename -
    lives in motiongraph_author.parse_spec, shared with the animspec
    subcommand. This function only adapts validated rows to generator
    choices, enforcing the anim_choices-family conventions: choice 0 is
    ALWAYS the synthesized Auto/random choice - authors list real clips
    only - weight defaults to 1 with null opting a clip out of the random
    pool, and the first listed clip is the rest state.

    `game` is forwarded to parse_spec for its event-catalogue checks
    (audio event names against the measured hash set, particle names
    against the measured .particleeffect list); omit to skip them.
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        raise PropBuildError(f"spec file not found: {path}")
    except json.JSONDecodeError as e:
        raise PropBuildError(f"spec file is not valid JSON ({path}): {e}")
    try:
        rows = parse_spec(payload, game)
    except ValueError as e:
        raise PropBuildError(f"{path}: {e}")
    # Motiongraphs come in structurally different FAMILIES (doors, platforms,
    # sequence-chain animatronics, ...); a clip list can only describe the
    # selectable-looping-prop family, so the spec carries the family as data
    # and anything else is refused rather than mis-generated. Additional
    # families mean additional emitters behind the same field, not a second
    # pipeline
    family = payload.get("family", "anim_choices")
    if family != "anim_choices":
        raise PropBuildError(
            f"{path}: family {family!r} is not implemented - only "
            f"'anim_choices' (a dropdown of selectable looping clips) can be "
            f"generated so far")
    # The generator emits every state with looping flags; a one-shot dropdown
    # entry is only supported by the append path (the animspec subcommand), so
    # refuse it here rather than silently emitting a looping entry
    non_loop = [r["enum_name"] for r in rows if not r["loop"]]
    if non_loop:
        raise PropBuildError(
            f"{path}: loop=false rows {non_loop} - one-shot entries are not "
            f"yet supported by graph generation (the animspec append "
            f"subcommand does support them)")
    choices = [("Default", None, None,
                payload.get("auto_label", auto_label_default), ())]
    for r in rows:
        choices.append((r["enum_name"], r["clip"], r["weight"],
                        r["label_text"], tuple(r["events"])))
    return choices, payload.get("blend_time")


def build_from_animspec(cobra_dir, asset, art_dir, dest_dir, vars_ref=None,
                        blend_time=None, game="Planet Coaster 2", skip=(),
                        log=print):
    """read_animspec() + build(), the exact glue ovl_tool_cmd's buildprop
    subcommand already does inline - factored out so the GUI's "Create
    animated prop..." dialog calls the SAME two-step flow rather than
    reimplementing it, and so a change to one caller's error handling
    cannot drift from the other's. Returns build()'s report dict; raises
    PropBuildError for every failure mode (read_animspec's ValueErrors are
    already PropBuildError; build()'s own ValueErrors - the generator's
    refusals, an unverified game, a malformed choice list - are user input
    problems, not tracebacks, so they are wrapped the same way here).

    `<art_dir>/<asset lower>.animspec.json` is the ONE convention for
    where the spec lives, matching File > Export > Cobra Animation Spec's
    default location next to the rest of the exported art.
    """
    spec_path = animspec_path(art_dir, asset)
    if not os.path.isfile(spec_path):
        raise PropBuildError(
            f"no animspec at {spec_path} - export it from Blender "
            f"(File > Export > Cobra Animation Spec)")
    choices, spec_blend = read_animspec(spec_path, game=game)
    blend = blend_time if blend_time is not None else \
        (spec_blend if spec_blend is not None else 0.15)
    try:
        return build(
            cobra_dir=cobra_dir, asset=asset, art_dir=art_dir, dest_dir=dest_dir,
            choices=choices, vars_ref=vars_ref, blend_time=blend, game=game,
            skip=skip, log=log)
    except ValueError as e:
        raise PropBuildError(str(e))


def animspec_path(art_dir, asset):
    """<art_dir>/<asset lower>.animspec.json - the ONE convention for where a
    spec lives, matching File > Export > Cobra Animation Spec's default."""
    return os.path.join(art_dir, asset.lower() + ".animspec.json")


def planned_outputs(asset, dest_dir, spec_path, game=None):
    """(path, new_text) for everything a build of `spec_path` into dest_dir
    would write, without building anything: the OVL (new_text None, it is not
    text), then one loc file per dropdown entry.

    Lets the caller ask about ALL of them on the same terms before the build
    runs, rather than the OVL overwriting silently while its loc text asks.
    Symbols come from loc_symbol, the same function that generates the ones
    the graph references, so this cannot drift from what build() writes.
    """
    choices, _ = read_animspec(spec_path, game=game)
    out = [(os.path.join(dest_dir, asset + ".ovl"), None)]
    for c in choices:
        out.append((os.path.join(dest_dir, f"{loc_symbol(asset, c[0])}.txt"), c[3]))
    return out


def existing_outputs(planned):
    """(path, current_text, new_text) for each planned output already on disk.

    current_text is None for a file that is not text (the OVL) or cannot be
    read as UTF-8 - the caller still gets to decide about it, it just has
    nothing to show for the before/after.
    """
    out = []
    for path, new_text in planned:
        if not os.path.isfile(path):
            continue
        current = None
        if new_text is not None:
            try:
                with open(path, encoding="utf-8") as fh:
                    current = fh.read()
            except (UnicodeDecodeError, OSError):
                current = None
        out.append((path, current, new_text))
    return out


def existing_loc_texts(report, dest_dir):
    """(symbol, existing_text, new_text) for every loc file already in
    dest_dir that write_loc_texts would replace.

    Label text is prose, and prose gets polished or translated in the mod long
    after it was typed in Blender, so replacing a file that is already there
    is the user's decision. Everything the write would land on is listed,
    whether or not the wording differs - the question is which files may be
    replaced, and that is answered per file. Symbols with no file yet are not
    listed: there is nothing to lose and nothing to ask.
    """
    out = []
    for symbol, (_enum, text) in zip(report["loc_symbols"], report["labels"]):
        path = os.path.join(dest_dir, f"{symbol}.txt")
        if not os.path.isfile(path):
            continue
        # utf-8-sig and newline="" mirror write_loc_texts exactly: a file an
        # editor saved with a BOM would otherwise read back with a leading
        # ﻿ and look changed when it is not, and universal newlines would
        # fold a CRLF file to \n so it looks UNchanged when a rewrite would in
        # fact alter every line ending
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                current = fh.read()
        except (UnicodeDecodeError, OSError):
            # same degradation as existing_outputs: the caller still gets to
            # decide about a file it cannot preview
            current = None
        out.append((symbol, current, text))
    return out


def write_loc_texts(report, dest_dir, skip=(), log=print):
    """Write one <loc symbol>.txt per choice from `report` into dest_dir.

    `skip` names symbols to leave alone - what the caller's user chose not to
    overwrite from existing_loc_texts()'s list.

    The build already knows both halves - the symbol it generated from the
    asset and enum names, and the label_text the author wrote in the spec -
    so the report could name every file the caller had to create and then
    leave them to retype the contents. This writes them instead.

    Deliberately NOT packed anywhere: these belong to the caller's MOD ovl
    (the loc layer lives beside the rest of a mod's text, not in the asset),
    which nothing here can locate. The caller adds them to their own OVL the
    same way they add any other file - .txt is a first-class format
    (modules/formats/TXT.py), so no special handling is needed there.

    Plain UTF-8, no BOM and no trailing newline: the file's whole content is
    the string the player sees. Existing files are overwritten, since
    rewriting them after a label change is the point.

    Returns the paths written.
    """
    pairs = list(zip(report["loc_symbols"], report["labels"]))
    # Validate EVERY symbol before writing ANY file. Refusing mid-loop would
    # leave the earlier files on disk with no way for the caller to learn which
    # ones landed
    seen = {}
    for symbol, _label in pairs:
        # parse_spec already constrains enum names and the generator the asset
        # name, precisely because a symbol becomes a FILENAME here - re-check
        # rather than trust a report that could have come from anywhere
        if not _SAFE_LOC_SYMBOL.match(symbol):
            raise PropBuildError(
                f"loc symbol {symbol!r} is not a bare name - refusing to use it "
                f"as a filename")
        # Windows and macOS filesystems are case-insensitive by default, so two
        # symbols differing only in case are ONE file there and two on Linux -
        # the second write would silently destroy the first label
        clash = seen.get(symbol.lower())
        if clash is not None and clash != symbol:
            raise PropBuildError(
                f"loc symbols {clash!r} and {symbol!r} differ only in case, so "
                f"they are the same file on Windows and macOS and one label "
                f"would be lost - rename one of the animation choices")
        seen[symbol.lower()] = symbol

    os.makedirs(dest_dir, exist_ok=True)
    skip = set(skip)
    written, kept = [], []
    for symbol, (_enum, text) in pairs:
        if symbol in skip:
            kept.append(symbol)
            continue
        path = os.path.join(dest_dir, f"{symbol}.txt")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        written.append(path)
    log(f"wrote {len(written)} loc text files to {dest_dir}"
        + (f", kept {len(kept)} as they were" if kept else ""))
    return written


def apply_animspec(cobra_dir, ovl_path, spec_path, asset=None, blend_time=None,
                   game="Planet Coaster 2", log=print):
    """Generate this asset's graph from `spec_path` and inject it into the
    ALREADY PACKED ovl at `ovl_path`, in place. Returns build()'s report dict.

    This is the "I added my files the normal way" route: the user creates an
    OVL from a folder of Blender exports exactly as they always have, then
    asks for the generated half to be added to it. build() is the same
    sequence with the packing step in front, so both share _generate_into and
    cannot drift.

    `asset` defaults to the OVL's own base name, which "New from Folder"
    already takes from the folder name - so the ordinary path needs no name
    typed anywhere. Re-running on an OVL that already carries generated
    entries replaces them, which is what makes re-export-and-reapply an
    iteration loop rather than a duplicate-entry error.
    """
    if not os.path.isfile(ovl_path):
        raise PropBuildError(f"no such ovl: {ovl_path}")
    cfg = config.Config(cobra_dir)
    cfg.load()
    ovl = _fresh(cfg, game, ovl_path)
    report = apply_animspec_to(ovl, spec_path, asset=asset, ovl_path=ovl_path,
                               blend_time=blend_time, game=game, log=log)
    ovl.save(ovl_path, commands={"update_aux": False})
    return report


def apply_animspec_to(ovl, spec_path, asset=None, ovl_path=None,
                      blend_time=None, game="Planet Coaster 2", log=print):
    """apply_animspec against an OvlFile the caller already has open, WITHOUT
    writing anything: the generated entries land in `ovl` and saving is the
    caller's call.

    This is the shape the GUI needs - it holds the OVL the user is looking at,
    and every other mutating action there (inject, remove, rename) works the
    same way: change the open container, mark it dirty, let the user decide
    when to save. It also means a failed apply cannot leave a half-modified
    file on disk, since nothing was written.
    """
    ovl_path = ovl_path or getattr(ovl, "filepath", "") or ""
    asset = asset or os.path.splitext(os.path.basename(ovl_path))[0]
    if not asset:
        raise PropBuildError(
            "cannot tell what this asset is called - save the OVL under the "
            "asset's name first, or pass one explicitly")
    choices, spec_blend = read_animspec(spec_path, game=game)
    blend = blend_time if blend_time is not None else \
        (spec_blend if spec_blend is not None else 0.15)

    # Only remove entries THIS call is about to replace - never everything of
    # the same extension. A container can legitimately hold more than one
    # generated set: retail ships assets built from several independently
    # placeable parts (two separate doors sharing one OVL) as well as single
    # objects whose animation is split across synchronised parts (a water
    # wheel and its post, which cannot be separated or animated apart) - and
    # a blind sweep destroyed either shape, deleting a working object nothing
    # here was asked to touch.
    #
    # loopanimselection.enumnamer is NOT asset-qualified (unlike retail's own
    # convention, e.g. wheel_large_01_loopanimselection.enumnamer) - every
    # asset this tool generates for wants that exact name, so it can never be
    # safely attributed to one graph in a container that already holds
    # another. Any OTHER generated-looking entry left over refuses rather than
    # guesses: the alternative is a name collision on the next apply, or
    # silently orphaning a part this call was never told about.
    own = {f"{asset.lower()}.motiongraph", "loopanimselection.enumnamer",
           f"{asset.lower()}.sceneryanimchoices"}
    existing = [k for k in ovl.loaders
               if k.endswith((".motiongraph", ".enumnamer", ".sceneryanimchoices"))]
    ours = [k for k in existing if k in own]
    others = sorted(k for k in existing if k not in own)
    if others:
        raise PropBuildError(
            f"{asset!r} would replace generated entries, but this container "
            f"also carries {others}, which do not belong to {asset!r} and "
            f"would not be touched - remove them first if they are stale, or "
            f"apply against the asset they DO belong to")
    if ours:
        log(f"replacing existing generated entries: {sorted(ours)}")
        ovl.remove(ours)
    try:
        pairs = apply_clip_prefix(ovl, asset)
    except ValueError as e:
        # a clip collision is a user error, and this function's contract is
        # PropBuildError - the CLI and the GUI both catch only that
        raise PropBuildError(str(e)) from e
    log("qualified clips: " + (", ".join(f"{a} -> {b}" for a, b in pairs)
                               or "(none needed)"))
    try:
        check_prefix_consistent(ovl, asset)
        vars_ref = _sole_vars_ref(ovl, ovl_path or asset, asset)
        return _generate_into(ovl, asset, choices, vars_ref, blend, game,
                              src_hint=os.path.basename(ovl_path) or asset,
                              out_ovl=ovl_path, log=log)
    except ValueError as e:
        raise PropBuildError(str(e)) from e


def _sole_vars_ref(o, ovl_path, asset):
    """The one .motiongraphvars entry in `o`, as a vars_ref name, or the name
    to generate one under when the OVL carries none.

    The graph names its variable set, so exactly one has to be identifiable.
    Deriving it from the packed file rather than asking removes a field the
    user would otherwise have to type; generating it when absent removes the
    file itself from their work, since for this family its contents are fixed
    (see motiongraph_generator.motiongraphvars_xml). Several with no way to
    pick between them stays a real user error worth naming.

    `<Asset>Vars` is retail's own per-asset convention (AQ_Doors01 ->
    AQ_Doors01Vars); shared sets under other names exist too, which is why a
    packed file always wins over the generated default.
    """
    found = [k.rsplit(".", 1)[0] for k in o.loaders
             if k.endswith(".motiongraphvars")]
    if len(found) > 1:
        raise PropBuildError(
            f"{os.path.basename(ovl_path)} has several .motiongraphvars "
            f"entries ({sorted(found)}) and nothing says which the graph "
            f"should reference - keep one.")
    return found[0] if found else f"{asset}Vars"


def _fresh(cfg, game, path=None):
    o = OvlFile()
    o.reporter = _RaisingReporter()
    o.cfg = cfg
    o.game = game
    o.load_hash_table()
    if path:
        o.load(path, commands={"game": game})
    return o


def _graph_entry(o):
    entry = next((k for k in o.loaders if k.endswith(".motiongraph")), None)
    if entry is None:
        # add_files logs per-file create() failures without raising, so a graph
        # that failed to create shows up here, not there - diagnose it as what
        # it is rather than dying with a bare StopIteration
        raise PropBuildError(
            "the OVL has no .motiongraph entry - creating the generated graph "
            "failed; check the log above for the create() error")
    return entry


def _extract_one(o, entry_name, work):
    with contextlib.redirect_stdout(io.StringIO()):
        return o.loaders[entry_name].extract(lambda n: os.path.join(work, n))[0]


def build(cobra_dir, asset, art_dir, dest_dir, choices, blend_time,
          vars_ref=None, game="Planet Coaster 2", skip=(), log=print):
    """Build `asset` into dest_dir from scratch. Returns a report dict; raises
    PropBuildError.

    cobra_dir  the cobra-tools checkout (for config)
    art_dir    directory holding the exported .ms2/.manis and any .fgm/.tex;
               a .motiongraphvars here is used if present, generated if not
    dest_dir   output directory for <asset>.ovl and its sidecars - created if
               missing; an existing file there is overwritten unless it is
               named in `skip`, never wiped outright
    vars_ref   name of the variable set the graph references; defaults to the
               one packed from art_dir, else <Asset>Vars generated alongside
    skip       full paths, from dest_dir, to leave untouched if already
               present - what the caller's user chose to keep from
               existing_outputs()'s list
    """
    cfg = config.Config(cobra_dir)
    cfg.load()

    # escape the DIRECTORY: glob reads [ ] ? in it as pattern syntax, so an art
    # folder named "My Prop [v2]" would match nothing and report "no art" while
    # the user is looking straight at the files
    art_glob = glob.escape(art_dir)
    art_files = sorted(glob.glob(os.path.join(art_glob, "*.ms2")) +
                       glob.glob(os.path.join(art_glob, "*.manis")) +
                       glob.glob(os.path.join(art_glob, "*.fgm")) +
                       glob.glob(os.path.join(art_glob, "*.tex")) +
                       glob.glob(os.path.join(art_glob, "*.motiongraphvars")))
    if not art_files:
        raise PropBuildError(f"no art (.ms2/.manis) in {art_dir}")
    # A .tex is CREATED from its source images (the PNGs its exporter wrote
    # next to it), not from the .tex bytes alone - and add_files logs a failed
    # create() without raising, so a raw extracted .tex would vanish silently
    # and the mesh would ship with dangling material references. Refuse loudly
    # instead
    for f in art_files:
        if f.lower().endswith(".tex"):
            base = os.path.splitext(os.path.splitext(f)[0])[0]
            if not glob.glob(base + "*.png") and not glob.glob(
                    os.path.splitext(f)[0] + "*.png"):
                raise PropBuildError(
                    f"{os.path.basename(f)} has no source .png next to it - a "
                    f".tex cannot be created from its own bytes. Export the "
                    f"texture properly (PNG alongside) or remove the .tex "
                    f"from {art_dir}.")
    stray = [os.path.basename(f) for f in art_files
             if f.lower().endswith(".ms2")
             and not os.path.basename(f).lower().startswith(asset.lower())]
    if stray:
        raise PropBuildError(
            f"art is named for a different asset: {stray}, expected {asset!r}")
    if vars_ref is None:
        supplied = [os.path.splitext(os.path.basename(f))[0] for f in art_files
                    if f.lower().endswith(".motiongraphvars")]
        if len(supplied) > 1:
            raise PropBuildError(
                f"{art_dir} has several .motiongraphvars files ({sorted(supplied)}) "
                f"and nothing says which the graph should reference - keep one, "
                f"or name it with vars_ref")
        # absent -> _generate_into emits one under this name
        vars_ref = supplied[0] if supplied else f"{asset}Vars"

    # NOT a wipe of dest_dir. This used to rmtree it, which silently deleted
    # whatever else the user had picked - a mod folder, say - and meant the
    # OVL overwrote without asking while the loc text beside it asked. The
    # build now only ever writes its own outputs, and every one of them is
    # overwritten on the same terms: see planned_outputs and `skip`
    os.makedirs(dest_dir, exist_ok=True)
    out_ovl = os.path.join(dest_dir, asset + ".ovl")

    # --- 1. create the container from nothing, qualify clip names -----------
    # Blender exports bare Action names ("MyClip") while the graph references
    # Asset$Clip ("MyProp$MyClip"); unqualified clips give a dropdown entry that
    # plays nothing, silently. apply_clip_prefix renames the .mani ENTRIES -
    # editing ManisInfo.name corrupts the file (names there are derived, not
    # authoritative)
    ovl = _fresh(cfg, game)
    ovl.add_files(art_files, art_dir)
    # both of these raise ValueError on what is a plain user error - a clip
    # name that collides, or art from another asset - and this function's
    # contract is PropBuildError, so translate rather than let a traceback out
    try:
        pairs = apply_clip_prefix(ovl, asset)
        log(f"art: {[os.path.basename(f) for f in art_files]}")
        log("qualified clips: " + (", ".join(f"{a} -> {b}" for a, b in pairs)
                                   or "(none needed)"))
        check_prefix_consistent(ovl, asset)
    except ValueError as e:
        raise PropBuildError(str(e)) from e

    report = _generate_into(ovl, asset, choices, vars_ref, blend_time,
                            game, src_hint=art_dir, out_ovl=out_ovl, log=log)
    # The writes, all at the end and all on the same terms: everything above
    # happened in memory, so a build that fails leaves nothing behind, and
    # anything the caller listed in `skip` is left exactly as it was
    # normcase as well as normpath: Windows and macOS resolve paths case
    # insensitively, so a skip entry differing only in case names the very file
    # the user asked to keep, and matching on normpath alone would overwrite it
    skipped = {os.path.normcase(os.path.normpath(p)) for p in skip}
    if os.path.normcase(os.path.normpath(out_ovl)) in skipped:
        log(f"kept existing {out_ovl} - not overwritten")
    else:
        ovl.save(out_ovl, commands={"update_aux": False})
        log(f"created: {out_ovl}")
    # The loc text is an output of this build like the OVL is, so it lands in
    # the same folder and obeys the same skip list
    # derived from the ORIGINAL skip paths, not the normcased set: a loc symbol
    # has to match the report's own casing, and normcase would have lowered it
    write_loc_texts(report, dest_dir, log=log,
                    skip={os.path.splitext(os.path.basename(p))[0]
                          for p in skip if p.lower().endswith(".txt")})
    return report


def _generate_into(ovl, asset, choices, vars_ref, blend_time, game,
                   src_hint, out_ovl, log=print):
    """Steps 2-5 of the assembly, against an already-packed OvlFile IN
    MEMORY: generate + inject the graph and enumnamer, verify the graph
    resolves by identity, verify material binding, author + verify the choice
    table. Returns the report dict.

    Nothing here writes to `out_ovl`; it is the caller's to save (build()
    does, the GUI leaves it dirty for the user). That is what lets a failed
    apply leave the file on disk untouched, and it is why the verification
    steps read back through the live loaders rather than reloading from
    disk - proven equivalent, since extract() works on entries that have
    never been saved.

    `src_hint` names where missing inputs should have come from in error
    messages (build: the art folder; apply: the OVL itself); `out_ovl` is
    only the path reported back to the caller."""
    # --- 2. generated graph + enumnamer --------------------------------------
    # blend_time is arithmetic on every choice duration and is interpolated
    # straight into the graph, so a None reaches the XML as the text "None" and
    # surfaces much later as an add-files failure naming nothing the user can
    # act on, while a negative silently makes a choice shorter than its clip
    if not isinstance(blend_time, (int, float)) or isinstance(blend_time, bool):
        raise PropBuildError(
            f"blend_time must be a number, not {blend_time!r}")
    if blend_time < 0:
        raise PropBuildError(
            f"blend_time {blend_time} is negative, which would make a choice "
            f"shorter than the clip it plays")
    graph_xml, enum_xml = generate(asset, choices, vars_ref, blend_time,
                                   game=game)
    work = tempfile.mkdtemp()
    entry = f"{asset.lower()}.motiongraph"
    gen_xml = os.path.join(work, entry)
    with open(gen_xml, "w", encoding="utf-8") as fh:
        fh.write(graph_xml)
    en_xml = os.path.join(work, "loopanimselection.enumnamer")
    with open(en_xml, "w", encoding="utf-8") as fh:
        fh.write(enum_xml)
    gen_files = [gen_xml, en_xml]
    # The vars resource is generated when the container has none, for the same
    # reason the graph is: its contents are fixed for this family and carry
    # nothing the author chose, so demanding the file only made them copy one
    # out of a retail asset under a borrowed name. A packed one is left alone
    if not any(k.endswith(".motiongraphvars") for k in ovl.loaders):
        vars_xml = os.path.join(work, f"{vars_ref.lower()}.motiongraphvars")
        with open(vars_xml, "w", encoding="utf-8") as fh:
            fh.write(motiongraphvars_xml(game))
        gen_files.append(vars_xml)
        log(f"generated vars entry: {os.path.basename(vars_xml)}")
    ovl.add_files(gen_files, work)
    shutil.rmtree(work, ignore_errors=True)

    # --- 3. the graph must actually resolve, by identity ---------------------
    entry = _graph_entry(ovl)
    w = tempfile.mkdtemp()
    txt = open(_extract_one(ovl, entry, w), encoding="utf-8", errors="replace").read()
    refs = sorted({m.group(1).strip()
                   for m in re.finditer(r"<mani>([^<]+)</mani>", txt)})
    have = set()
    for k in [x for x in ovl.loaders if x.endswith(".manis")]:
        m = ManisFile()
        m.load(_extract_one(ovl, k, w))
        have.update(mi.name for mi in m.mani_infos)
    have_vars = {k.rsplit(".", 1)[0].lower()
                for k in ovl.loaders if k.endswith(".motiongraphvars")}
    shutil.rmtree(w, ignore_errors=True)

    our_clips = sorted({c[1] for c in choices if c[1]})
    expected = {f"{asset}${c}" for c in our_clips}
    if set(refs) != expected:
        raise PropBuildError(
            f"graph references {set(refs)}, expected exactly {expected}")
    # case-insensitive: the graph keeps authored casing, the manis lowercases
    missing = {r for r in refs if r.lower() not in {h.lower() for h in have}}
    if have and missing:
        raise PropBuildError(
            f"graph references clips the manis does not provide: "
            f"{sorted(missing)}; manis has {sorted(have)}")
    if vars_ref.lower() not in have_vars:
        raise PropBuildError(
            f"the graph references vars entry {vars_ref!r}, but no "
            f"{vars_ref.lower()}.motiongraphvars is in {src_hint} - the graph "
            f"would point at nothing. Either supply that file or pick a "
            f"vars_ref matching one that exists.")

    # --- 4. every material the mesh names must have a .fgm ------------------
    # The ms2 stores only material NAMES; an unbound name renders unlit white
    # with no error anywhere. Optional by design - see the module docstring -
    # so a missing .fgm is caught HERE, not required up front. The
    # _hitcheck/_joint names the scan also finds are scene nodes, not
    # materials
    have_fgm = {k.rsplit(".", 1)[0].lower()
                for k in ovl.loaders if k.endswith(".fgm")}
    want_mat = set()
    for k in [x for x in ovl.loaders if x.endswith(".ms2")]:
        blob = b"".join(bytes(b) for b in (ovl.loaders[k].get_ms2_buffer_datas()
                                           or []) if b)
        for s in re.findall(rb"[ -~]{4,}", blob):
            t = s.decode().lower()
            if (t.startswith(asset.lower())
                    and not t.endswith((".ms2", ".mdl2", "_hitcheck", "_joint"))):
                want_mat.add(t)
    unbound = want_mat - have_fgm
    if unbound:
        raise PropBuildError(
            f"materials with no .fgm to bind to: {sorted(unbound)} - the prop "
            f"would render unlit white. Rename them to one of "
            f"{sorted(have_fgm)} or add the .fgm files to {src_hint}.")

    # --- 5. author the choice table from the same rows ----------------------
    # Durations come from the built asset's manis. The Auto choice has no clip
    # of its own; its duration follows the longest clip (retail convention)
    mw = tempfile.mkdtemp()
    # EVERY .manis, not just the first: step 3 above unions clip names across
    # all of them, so reading durations from one container would report a clip
    # as missing that the asset plainly has. Retail ships several per asset
    # (SC_Villager_Animatronic and TY_Animatronics carry three each)
    mks = [x for x in ovl.loaders if x.endswith(".manis")]
    if not mks:
        raise PropBuildError(
            f"{src_hint} has no .manis, so no clip has a duration - pack the "
            f"animations before adding an animation spec")
    dur = {}
    for mk in mks:
        mf = ManisFile()
        mf.load(_extract_one(ovl, mk, mw))
        dur.update({mi.name.split("$")[-1].lower(): mi.duration
                    for mi in mf.mani_infos})

    labels = []
    rows = []
    # index rather than unpack: a choice row carries an optional 5th member
    # (its events), and unpacking a fixed width breaks the moment it is present
    for i, c in enumerate(choices):
        enum, clip, label = c[0], c[1], c[3]
        play = clip or max(our_clips, key=lambda c: dur.get(c.lower(), 0))
        d = dur.get(play.lower())
        if d is None:
            raise PropBuildError(
                f"choice {enum!r} plays {play!r}, which the manis does not "
                f"have: {sorted(dur)}")
        labels.append((enum, label))
        rows.append(
            f'\t\t<animchoice index="{i}" duration="{d + 2 * blend_time}">\n'
            f'\t\t\t<label>[InfoPanel_AnimationType_{asset}_{enum}]</label>\n'
            f'\t\t</animchoice>')
    choices_xml = os.path.join(mw, f"{asset.lower()}.sceneryanimchoices")
    with open(choices_xml, "w", encoding="utf-8") as fh:
        fh.write(f'<SceneryAnimChoicesRoot count="{len(choices)}" pad="0" '
                 f'game="{game}">\n\t<choices pool_type="4">\n'
                 + "\n".join(rows) + "\n\t</choices>\n</SceneryAnimChoicesRoot>\n")
    ovl.add_files([choices_xml], mw)

    cw = tempfile.mkdtemp()
    body = open(_extract_one(ovl, f"{asset.lower()}.sceneryanimchoices", cw),
                encoding="utf-8").read()
    got = re.findall(r"<label>\[([^\]]+)\]</label>", body)
    shutil.rmtree(mw, ignore_errors=True)
    shutil.rmtree(cw, ignore_errors=True)
    if len(got) != len(choices):
        raise PropBuildError(f"wrote {len(choices)} choices, read back {len(got)}")

    # VFX events name a prefab CHILD; the graph half is emitted here but the
    # child itself is the caller's to declare, so report the contract rather
    # than guess at their prefab format. An armed VFX event whose child does
    # not exist is silent in game with no error anywhere
    vfx_children = sorted({e["name"] for c in choices for e in (c[4] if len(c) > 4 else ())
                           if e["type"] in ("VFXEnable", "VFXDisable",
                                            "ParticleEmissionRate")})
    audio_locations = sorted({e["location"] for c in choices
                              for e in (c[4] if len(c) > 4 else ())
                              if e["type"].startswith("Audio") and e["location"]})
    # `particle` never reaches the graph (ds_name only ever names the child),
    # so this is the one place it goes anywhere: telling the caller which
    # .particleeffect to assign to which prefab child. An enable/disable pair
    # on the same child only needs the particle on one of them to appear here
    vfx_particles = {e["name"]: e["particle"] for c in choices
                     for e in (c[4] if len(c) > 4 else ())
                     if e["type"] in ("VFXEnable", "VFXDisable",
                                       "ParticleEmissionRate") and e.get("particle")}
    return {
        "out_ovl": out_ovl,
        "graph_entry": entry,
        "clip_refs": refs,
        "labels": labels,          # (enum, label_text) for the caller's loc layer
        "loc_symbols": [f"InfoPanel_AnimationType_{asset}_{e}" for e, _ in labels],
        "fgm": sorted(have_fgm),
        # the prefab contract the caller must satisfy for the armed events to
        # do anything: a child entity per VFX name, an emitter per audio location
        "vfx_children": vfx_children,
        "audio_locations": audio_locations,
        "vfx_particles": vfx_particles,
    }
