"""
ovl_tool_cmd.py

Command-line OVL tool using the same OvlFile API as ovl_tool_gui.

Subcommands:
  new             - create a new OVL from a folder
  extract         - extract files from an OVL
  inject          - inject/replace files into an OVL
  buildprop       - build a scenery prop OVL from scratch out of an animspec
  apply-animspec  - add a GENERATED graph to an already-packed OVL
  animspec        - APPEND animation choices to an OVL's existing donor graph

Examples:
  ovl_tool_cmd.py extract -i path/to/main.ovl
  ovl_tool_cmd.py new -i this/folder/ -g "Planet Zoo" -o Main.ovl
  ovl_tool_cmd.py inject -f test/test.lua  -g "Jurassic World Evolution 3" --in-place path/to/main.ovl
  ovl_tool_cmd.py buildprop MyProp -g "Planet Coaster 2" --art this/folder/ --dest out/
  ovl_tool_cmd.py apply-animspec path/to/asset.ovl -s anim_choices.json -g "Planet Coaster 2"
  ovl_tool_cmd.py animspec -s anim_choices.json -g "Planet Coaster 2" --in-place path/to/asset.ovl

"""
from __future__ import annotations

from utils import config
from utils.logs import logging_setup # type: ignore
import logging
logging_setup("ovl_tool_cmd")

import argparse
import io
import os
import re
import sys
import tempfile
from typing import Iterable, List, Optional

# -----------------------------------------------------------------------------
# Bootstrapping: repo root, shared formats, logging shim
# -----------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import contextlib
from modules.formats.shared import DummyReporter 

class BuildReporter(DummyReporter):
    def __init__(self):
        self.warnings = []
        self.errors = []
        self.error_files = []

    def show_warning(self, msg: str):
        self.warnings.append(msg)

    def show_error(self, exception: Exception):
        self.errors.append(exception)

    def iter_progress(self, iterable, message, cond=True):
        for item in iterable:
            yield item

    @contextlib.contextmanager
    def report_error_files(self, operation):
        yield self.error_files
  

from generated.formats.ovl import games, OvlFile
from generated.formats.ovl_base.enums.Compression import Compression
from utils.config import Config

# In the GUI, logging.success is provided by their logging wrapper; here we alias
if not hasattr(logging, "success"):
    def success(msg, *args, **kwargs):
        logging.getLogger("cobra-tools").info(msg, *args, **kwargs)
    logging.success = success  # type: ignore[attr-defined]

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def ensure_exists(path: str, kind: str = "file") -> None:
    if kind == "file" and not os.path.isfile(path):
        die(f"{kind.capitalize()} does not exist: {path}")
    if kind == "dir" and not os.path.isdir(path):
        die(f"{kind.capitalize()} does not exist: {path}")


def find_common_root(files: Iterable[str]) -> str:
    paths = [os.path.abspath(p) for p in files]
    if not paths:
        return ""
    if len(paths) == 1:
        return os.path.dirname(paths[0])
    return os.path.commonpath(paths)


def resolve_game_label(label: Optional[str]) -> Optional[str]:
    """
    Convert a user-facing game label to the same value the GUI uses.

    The combo box in the GUI is built from [g.value for g in games], and
    game_changed() sets ovl_data.game to that value, so we mirror that.
    """
    if not label:
        return None
    for g in games:
        if label == getattr(g, "value", None) or label == getattr(g, "name", None):
            return g.value
    return label


def game_choices() -> List[str]:
    try:
        return [g.value for g in games]
    except Exception:
        return []


def compression_choices() -> List[str]:
    return [c.name for c in Compression]


# -----------------------------------------------------------------------------
# Command line operations 
# -----------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace) -> None:
    """
    File > New (from folder): create an OVL from a directory and save it.

    Mirrors MainWindow.create_ovl + save in ovl_tool_gui:
        self.ovl_data.clear()
        self.game_changed()
        self.ovl_data.create(ovl_dir)
        self.ovl_data.save(filepath, commands={"update_aux": cfg["update_aux"]})
    """
    in_dir = os.path.abspath(args.input)
    out_ovl = os.path.abspath(args.output)

    ensure_exists(in_dir, "dir")
    if os.path.exists(out_ovl) and not args.force:
        die(f"Output file already exists: {out_ovl} (use --force to overwrite)")

    game = resolve_game_label(args.game)
    if not game:
        die("You must specify --game for 'new'.")

    ovl = OvlFile()
    config = Config(REPO_ROOT)
    config.load()
    ovl.cfg = config
    ovl.game = game  # same as game_changed() ultimately does
    ovl.load_hash_table()

    if args.compression:
        try:
            ovl.user_version.compression = Compression[args.compression]
        except KeyError:
            die(f"Unknown compression '{args.compression}'. Valid: {', '.join(compression_choices())}")

    logging.info("Creating OVL from %s", in_dir)
    logging.info("Game: %s", ovl.game)

    try:
        ovl.clear()    
        ovl.create(in_dir)
    except Exception as e:
        die(f"OvlFile.create failed: {e!r}")

    commands = {"update_aux": args.update_aux}
    try:
        ovl.save(out_ovl, commands=commands)
    except Exception as e:
        die(f"OvlFile.save failed: {e!r}")

    logging.success("Created OVL: %s", out_ovl)


def cmd_extract(args: argparse.Namespace) -> None:
    """
    Extract from an OVL.

    GUI equivalents:
      - MainWindow._extract_all -> ovl.extract(out_dir, only_types=only_types)
      - drag_files -> ovl.extract(temp_dir, only_names=file_names)
    """
    ovl_path = os.path.abspath(args.ovl)
    ensure_exists(ovl_path, "file")

    if args.output:
        out_dir = os.path.abspath(args.output)
    else:
        # Default: <same_dir>/<ovl_basename_without_ext>
        ovl_dir = os.path.dirname(ovl_path)
        ovl_name = os.path.splitext(os.path.basename(ovl_path))[0]
        out_dir = os.path.join(ovl_dir, ovl_name)

    os.makedirs(out_dir, exist_ok=True)

    ovl = OvlFile()

    # Optional override: if user passes -g, we force that game
    game = resolve_game_label(args.game)
    commands = {}
    if game:
        commands["game"] = game
        logging.info("Using game preset: %s", game)
    else:
        logging.info("No game preset supplied; OvlFile will auto-detect.")
    logging.info("Loading archive %s", ovl_path)

    try:
        ovl.load(ovl_path, commands)
        logging.info("Detected game from archive: %s", getattr(ovl, "game", "<unknown>"))
    except Exception as e:
        die(f"OvlFile.load failed: {e!r}")

    only_types = args.type or None
    only_names = args.name or None

    logging.info("Extracting to %s", out_dir)
    if only_types:
        logging.info("Only types: %s", ", ".join(only_types))
    if only_names:
        logging.info("Only names: %s", ", ".join(only_names))

    kwargs = {}
    if only_types:
        kwargs["only_types"] = only_types
    if only_names:
        kwargs["only_names"] = only_names

    try:
        ovl.extract(out_dir, **kwargs)
    except Exception as e:
        die(f"OvlFile.extract failed: {e!r}")

    logging.success("Extracted OVL to %s", out_dir)


def cmd_buildprop(args: argparse.Namespace) -> None:
    """Build a prop asset from its animspec; see modules/prop_builder.py."""
    from modules.prop_builder import PropBuildError, build_from_animspec
    game = resolve_game_label(args.game)
    try:
        report = build_from_animspec(
            cobra_dir=os.path.dirname(os.path.abspath(__file__)),
            asset=args.asset, art_dir=args.art, dest_dir=args.dest,
            vars_ref=args.vars_ref, blend_time=args.blend_time, game=game,
            log=lambda m: logging.info("%s", m))
    except PropBuildError as e:
        die(str(e))
    # print, not log: this report IS the command's result, and the log stream
    # goes to a file, so logging it would hide the one thing the user needs
    # die() already writes user-facing text directly for the same reason
    print(f"\nBuilt {report['out_ovl']}")
    print(f"  graph:     {report['graph_entry']}")
    print(f"  clips:     {', '.join(report['clip_refs'])}")
    print(f"  materials: {', '.join(report['fgm']) or '(none)'}")
    print("\nYour mod must supply these for the asset to work:")
    print(f"  loc text for: {', '.join(report['loc_symbols'])}")
    # The graph half of an effect is armed here; the prefab half is the
    # caller's. Say so explicitly - a missing child or emitter is silent in
    # game, with no error to trace back to this build
    if report["vfx_children"]:
        print(f"  prefab children for the armed VFX: "
              f"{', '.join(report['vfx_children'])}")
    if report["vfx_particles"]:
        print("  particle per prefab child: " + ", ".join(
            f"{child}={particle}"
            for child, particle in report["vfx_particles"].items()))
    if report["audio_locations"]:
        print(f"  audio emitter locations: "
              f"{', '.join(report['audio_locations'])}")


def cmd_apply_animspec(args: argparse.Namespace) -> None:
    """Add a generated motiongraph/enumnamer/choice-table to an OVL that was
    already packed the ordinary way; see modules/prop_builder.apply_animspec.

    The scriptable counterpart to the GUI's Util > Apply Animation Spec: New
    from Folder (or any other packing route) builds the container, this adds
    the generated half, in place. NOT the same command as 'animspec' above -
    that one APPENDS a choice to an asset's existing, donor-derived graph;
    this one GENERATES the graph itself, the same as buildprop but against a
    container that already exists instead of one built from scratch.
    """
    from modules.prop_builder import PropBuildError, apply_animspec
    game = resolve_game_label(args.game)
    ovl_path = os.path.abspath(args.ovl)
    ensure_exists(ovl_path, "file")
    spec_path = os.path.abspath(args.spec)
    ensure_exists(spec_path, "file")
    try:
        report = apply_animspec(
            cobra_dir=os.path.dirname(os.path.abspath(__file__)),
            ovl_path=ovl_path, spec_path=spec_path, asset=args.asset,
            blend_time=args.blend_time, game=game,
            log=lambda m: logging.info("%s", m))
    except PropBuildError as e:
        die(str(e))
    print(f"\nUpdated {report['out_ovl']}")
    print(f"  graph:     {report['graph_entry']}")
    print(f"  clips:     {', '.join(report['clip_refs'])}")
    print(f"  materials: {', '.join(report['fgm']) or '(none)'}")
    print("\nYour mod must supply these for the asset to work:")
    print(f"  loc text for: {', '.join(report['loc_symbols'])}")
    if report["vfx_children"]:
        print(f"  prefab children for the armed VFX: "
              f"{', '.join(report['vfx_children'])}")
    if report["vfx_particles"]:
        print("  particle per prefab child: " + ", ".join(
            f"{child}={particle}"
            for child, particle in report["vfx_particles"].items()))
    if report["audio_locations"]:
        print(f"  audio emitter locations: "
              f"{', '.join(report['audio_locations'])}")


def cmd_animspec(args: argparse.Namespace) -> None:
    """Append animation choices to an animated scenery OVL from a spec.

    This is the consumer side of File > Export > Cobra Animation Spec. Without it the
    exporter wrote a file nothing in the tools could act on, so the whole feature
    needed an external build script to be of any use.

    Three files inside the OVL have to stay in step, because they reference each
    other POSITIONALLY: the .sceneryanimchoices rows, the .enumnamer names, and the
    branches of every LoopAnimSelection node in the .motiongraph. append_clips does
    all three and asserts they agree, since a mismatch is not a crash - it is a
    dropdown entry that plays the wrong clip.

    APPEND-ONLY, and not by preference: a placed prop stores the choice INDEX, so
    inserting or reordering silently repoints every instance already placed in a park.
    """
    import xml.etree.ElementTree as ET

    from modules.motiongraph_author import (append_clips, load_spec,
                                            resolve_enum_holder, ClipSpec,
                                            FLAGS_LOOPING, FLAGS_ONE_SHOT,
                                            choice_duration, durations_from_manis,
                                            resolve_prefix)

    ovl_src = os.path.abspath(args.ovl)
    ensure_exists(ovl_src, "file")
    spec_path = os.path.abspath(args.spec)
    ensure_exists(spec_path, "file")
    if not args.in_place and not args.output:
        die("animspec needs -o/--output or --in-place.")
    out_path = ovl_src if args.in_place else os.path.abspath(args.output)
    loc_dir = os.path.abspath(args.loc_dir) if args.loc_dir else os.path.dirname(spec_path)

    game = resolve_game_label(args.game)
    try:
        rows = load_spec(spec_path, game=game)
    except ValueError as e:
        die(f"bad spec: {e}")
    logging.info("Spec %s: %d clip(s)", spec_path, len(rows))

    ovl = OvlFile()
    ovl.game = game
    ovl.load_hash_table()
    try:
        ovl.clear()
        ovl.load(ovl_src, {"game": game} if game else {})
    except Exception as e:
        die(f"OvlFile.load failed: {e!r}")

    def one(suffix):
        hits = [n for n in ovl.loaders if n.endswith(suffix)]
        if not hits:
            die(f"{ovl_src} has no {suffix} - is this an animated scenery asset?")
        if len(hits) > 1:
            die(f"{ovl_src} has {len(hits)} {suffix} entries ({hits}); ambiguous.")
        return hits[0]

    graph_name = one(".motiongraph")
    choices_name = one(".sceneryanimchoices")
    enum_name = one(".enumnamer")
    manis_name = next((n for n in ovl.loaders if n.endswith(".manis")), None)

    with tempfile.TemporaryDirectory() as work:
        def out(n):
            return os.path.join(work, n)

        paths = {n: ovl.loaders[n].extract(out)[0]
                 for n in (graph_name, choices_name, enum_name)}

        # Durations come from the .manis, never from the spec. The spec's value is
        # only used to detect drift, because hand-authored durations were wrong on
        # every entry when they were trusted
        clip_dur = {}
        if manis_name:
            from generated.formats.manis import ManisFile
            mf = ManisFile()
            mf.load(ovl.loaders[manis_name].extract(out)[0])
            clip_dur = durations_from_manis(mf)

        # The prefix comes from the TARGET, never from the spec. Whatever the
        # author typed was discarded by parse_spec, so a stale or mistyped prefix
        # cannot reach the asset - it is not merely unnecessary, it is a chance to
        # be wrong, and a wrong one plays silently in game
        try:
            prefix = resolve_prefix(ovl)
        except ValueError as e:
            die(str(e))
        if prefix is None:
            die(f"{ovl_src} has no prefixed clips, so there is no asset prefix to "
                f"append against. Export the animations into the asset first.")
        # The loc symbol is a FILENAME, so it takes the asset's real name rather
        # than the clip prefix: the prefix comes from .mani entry names, which the
        # OVL lowercases, and deriving the symbol from it silently turned the
        # shipped "..._MyProp_MyClip" into "..._myprop_MyClip"
        asset_name = os.path.splitext(os.path.basename(ovl_src))[0]
        logging.info("Asset clip prefix: %r (from the target); loc symbols use %r",
                     prefix, asset_name)

        specs, drift = [], []
        for r in rows:
            # r["clip"] is bare; qualify it against the target to look it up
            qualified = f"{prefix}${r['clip']}"
            real = clip_dur.get(qualified.lower())
            if real is None:
                die(f"clip {qualified} is not in {manis_name or 'the asset'} - "
                    f"export the animations first")
            if r["duration"] is not None and abs(r["duration"] - real) > 0.001:
                drift.append((qualified, r["duration"], real))
            specs.append(ClipSpec.from_clip(
                r["clip"], r["label_text"], choice_duration(real),
                loop=r["loop"], prefix=prefix, enum_name=r["enum_name"],
                asset_name=asset_name, events=r.get("events")))
        if drift:
            for clip, s, a in drift:
                logging.error("  %-28s spec %.4fs   asset %.4fs", clip, s, a)
            die("SPEC/ASSET MISMATCH - the spec and the built asset disagree about "
                "clip length. Re-export whichever is stale; refusing to guess.")

        graph = ET.parse(paths[graph_name])
        choices = ET.parse(paths[choices_name])
        enum = ET.parse(paths[enum_name])
        # Appended clips never INHERIT the donor's events (append_clips strips
        # its clones - firing the donor state's sounds on the new clip's
        # timing would be wrong) - they only get events the spec itself arms
        # Say so, or a spec with no events on an asset whose existing clips
        # DO have some reads as breakage rather than as what was asked for
        n_events = sum(len(list(el))
                       for el in graph.getroot().iter("additional_data_streams"))
        n_spec_events = sum(len(r.get("events") or ()) for r in rows)
        if n_events and not n_spec_events:
            logging.info(
                "This asset's existing clips carry %d audio/VFX event(s); the "
                "appended clip(s) have none in this spec, so they will play "
                "silently (existing entries keep their sound).", n_events)
        try:
            added = append_clips(graph.getroot(),
                                 resolve_enum_holder(enum.getroot()),
                                 choices.getroot(), specs)
        except ValueError as e:
            die(f"append_clips refused: {e}")

        for tree, p in ((graph, paths[graph_name]), (choices, paths[choices_name]),
                        (enum, paths[enum_name])):
            tree.write(p, encoding="utf-8", xml_declaration=False)

        os.makedirs(loc_dir, exist_ok=True)
        for a in added:
            loc = os.path.join(loc_dir, f"{a['label_symbol']}.txt")
            # parse_spec already constrains label_symbol to [A-Za-z0-9_], so this
            # cannot trigger from the CLI. Kept as defence in depth because the
            # consequence is writing an arbitrary file with spec-supplied contents,
            # and append_clips is a library entry point other callers may reach
            # without going through parse_spec
            if os.path.dirname(os.path.abspath(loc)) != os.path.abspath(loc_dir):
                die(f"refusing to write localisation file outside {loc_dir}: "
                    f"label_symbol {a['label_symbol']!r} escapes the directory")
            with open(loc, "w", encoding="utf-8") as f:
                f.write(a["label_text"])
            logging.info("  + %-26s index=%s  loc=%s",
                         a["clip"], a["index"], os.path.basename(loc))

        ovl.add_files([paths[n] for n in (graph_name, choices_name, enum_name)], work)
        try:
            ovl.save(out_path, {"update_aux": False})
        except Exception as e:
            die(f"OvlFile.save failed: {e!r}")

    logging.info("Appended %d choice(s) -> %s", len(added), out_path)
    logging.info("Localisation .txt files written to %s", loc_dir)


def _display_case_in_files(ovl, name):
    """Spellings of `name` that appear INSIDE motiongraphs, with their real case.

    Entry names are lowercased, so a user who types the lowercase form has nothing
    to derive the display case from - and renaming with only the lowercase spelling
    renames the entries while leaving every in-file reference stale.
    """
    out = set()
    pat = re.compile(re.escape(name), re.I)
    # NO blanket try/except around this. An earlier version wrapped the whole body
    # and returned an empty set on ANY error - which is exactly what happened when
    # `io` was not imported: a NameError was swallowed, the lookup silently found
    # nothing, and a rename that should have worked produced a broken asset that
    # looked fine. Per-file failures are tolerated and REPORTED; nothing else is
    with tempfile.TemporaryDirectory() as w:
        for n in list(ovl.loaders):
            if not n.endswith(".motiongraph"):
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    p = ovl.loaders[n].extract(lambda q: os.path.join(w, q))[0]
                txt = open(p, encoding="utf-8", errors="replace").read()
            except Exception as e:
                logging.warning("could not read %s to look up name casing: %r", n, e)
                continue
            for m in pat.finditer(txt):
                if m.group(0) != name:
                    out.add(m.group(0))
    return sorted(out)


def _prefix_from_graph_refs(ovl):
    """The asset prefix, read off the graph's own <mani> clip references.

    resolve_prefix() reads it from .mani ENTRY names, which is right in general -
    but useless immediately after --replace-clips, because the prefixed entries
    were just deleted and the replacements are bare. The graph still says what it
    expects, so ask it instead.

    Returns None when the graph names no qualified clip, or names several
    different prefixes (a multi-prop asset, where guessing would be wrong).
    """
    import xml.etree.ElementTree as _ET
    prefixes = set()
    with tempfile.TemporaryDirectory() as w:
        for n in list(ovl.loaders):
            if not n.endswith(".motiongraph"):
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    p = ovl.loaders[n].extract(lambda q: os.path.join(w, q))[0]
                root = _ET.parse(p).getroot()
            except Exception as e:
                logging.warning("could not read %s to find the clip prefix: %r", n, e)
                continue
            for el in root.iter("mani"):
                ref = (el.text or "").strip()
                if "$" in ref:
                    prefixes.add(ref.split("$", 1)[0])
    if len(prefixes) != 1:
        if prefixes:
            logging.warning("the graph references %d different clip prefixes (%s); "
                            "not guessing which the injected clips belong to",
                            len(prefixes), sorted(prefixes))
        return None
    return prefixes.pop()


def _repair_vars_ref(ovl):
    """Point the graph's vars reference back at the .motiongraphvars entry.

    Only touches graphs whose reference no longer resolves, so an asset that is
    already consistent is left exactly as it is.
    """
    import xml.etree.ElementTree as _ET
    entries = {n.lower() for n in ovl.loaders}
    fixed = []
    with tempfile.TemporaryDirectory() as w:
        for n in list(ovl.loaders):
            if not n.endswith(".motiongraph"):
                continue
            vs = [x for x in ovl.loaders if x.endswith(".motiongraphvars")]
            if len(vs) != 1:
                continue          # multi-prop: which vars belongs to which graph?
            stem = vs[0].rsplit(".", 1)[0]
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    p = ovl.loaders[n].extract(lambda q: os.path.join(w, q))[0]
                tree = _ET.parse(p)
                root = tree.getroot()
            except Exception as e:
                logging.warning("could not check the vars reference in %s: %r", n, e)
                continue
            abs_ = [e for e in root.iter("ab") if e.text and e.text.strip()]
            if len(abs_) != 2:
                continue
            cur = abs_[1].text.strip()
            if f"{cur.lower()}.motiongraphvars" in entries:
                continue          # already resolves - leave it alone
            abs_[1].text = stem
            tree.write(p, encoding="utf-8", xml_declaration=False)
            try:
                ovl.add_files([p], os.path.dirname(p))
                fixed.append((n, cur, stem))
            except Exception as e:
                die(f"could not write the repaired vars reference into {n}: {e!r}")
    for n, cur, stem in fixed:
        logging.info("repaired the vars reference in %s: %r -> %r (the rename "
                     "moved the entry but could not follow the reference)",
                     n, cur, stem)
    return fixed


def _check_clip_refs_resolve(ovl, label=""):
    """Every <mani> reference in every motiongraph resolves to a .mani entry.

    A rename can leave these dangling - it renames entry names and file contents by
    the same tuples, and those live in different cases - and the failure is silent:
    the asset loads, the prop places, and the animation simply never plays. Better
    to fail the command than hand back a broken asset.

    Returns the list of dangling refs.
    """
    import xml.etree.ElementTree as _ET
    entries = {n.lower() for n in ovl.loaders}
    dangling = []
    # Deliberately NOT wrapped in a blanket try/except - see _display_case_in_files
    # A post-condition that silently passes when it cannot run is worse than none,
    # because it converts "unverified" into "verified" without anyone noticing
    with tempfile.TemporaryDirectory() as w:
        for n in list(ovl.loaders):
            if not n.endswith(".motiongraph"):
                continue
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    p = ovl.loaders[n].extract(lambda q: os.path.join(w, q))[0]
                root = _ET.parse(p).getroot()
            except Exception as e:
                # Cannot verify this graph, so do not claim the asset is coherent
                die(f"could not read {n} to verify the rename left the clip "
                    f"references intact: {e!r}. Nothing was written.")
            for el in root.iter("mani"):
                ref = (el.text or "").strip()
                if ref and f"{ref.lower()}.mani" not in entries:
                    dangling.append(ref)
            # The VARS reference too. Checking only clips missed the failure that
            # actually happens: a rename renames the .motiongraphvars ENTRY (its
            # name contains the asset name) but not the reference to it, because
            # MotiongraphLoader.accept_string skips strings without '$'. So
            # "OldAssetVars" is left pointing at an entry now called
            # "newassetvars" - broken on essentially every renamed asset, while
            # the clip refs stay consistent and this check said everything was fine
            abs_ = [e for e in root.iter("ab") if e.text and e.text.strip()]
            if len(abs_) == 2:
                vref = abs_[1].text.strip()
                if f"{vref.lower()}.motiongraphvars" not in entries:
                    dangling.append(vref)
    return sorted(set(dangling))


def cmd_cleanasset(args: argparse.Namespace) -> None:
    """Remove the donor's residue from an asset built by renaming a retail one.

    A rename retargets everything whose name CONTAINS the donor asset name. What it
    cannot touch is residue that is named for something else or is not a name at
    all, and all of it ships to players:

      1. audio/VFX events on the donor's states - they reference emitter children
         of the DONOR's prefab, which your asset does not have
      2. the .motiongraphvars reference and entry - retail assets ship these
         under names unrelated to the asset, so the rename passes straight
         over them
      3. loc symbols on the donor's dropdown rows - these still resolve, but only
         by borrowing retail's translations, so the asset silently depends on
         retail strings it does not own

    ONLY for an asset you are creating. Never run it on an edit-in-place: existing
    states' events are load-bearing there, and other assets' loc symbols resolve
    against retail loc files on purpose.
    """
    import xml.etree.ElementTree as ET
    from modules.motiongraph_author import (strip_datastreams, loc_symbol,
                                            resolve_enum_holder, LOC_SYMBOL_ROOT,
                                            durations_from_manis, choice_duration,
                                            resolve_prefix)

    ovl_src = os.path.abspath(args.ovl)
    ensure_exists(ovl_src, "file")
    if not args.in_place and not args.output:
        die("cleanasset needs -o/--output or --in-place.")
    loc_dir = os.path.abspath(args.loc_dir)

    game = resolve_game_label(args.game)
    ovl = OvlFile()
    ovl.game = game
    ovl.load_hash_table()
    try:
        ovl.clear()
        ovl.load(ovl_src, {"game": game} if game else {})
    except Exception as e:
        die(f"OvlFile.load failed: {e!r}")

    def one(suffix, required=True):
        hits = [n for n in ovl.loaders if n.endswith(suffix)]
        if not hits and required:
            die(f"{ovl_src} has no {suffix} - is this an animated scenery asset?")
        if len(hits) > 1:
            die(f"{ovl_src} has {len(hits)} {suffix} entries ({hits}); ambiguous. "
                f"A multi-prop asset needs cleaning per prop.")
        return hits[0] if hits else None

    graph_name = one(".motiongraph")
    choices_name = one(".sceneryanimchoices", required=False)
    enum_name = one(".enumnamer", required=False)
    vars_name = one(".motiongraphvars", required=False)

    # Default to the OVL FILENAME, not the graph entry stem. Both identify the
    # asset, but entry names are lowercased by the OVL while the filename keeps
    # display case - and this name becomes a loc symbol, which is a FILENAME
    # Taking the entry stem produced "InfoPanel_AnimationType_myprop_Default"
    # where the shipped, in-game-verified symbol is "..._MyProp_Default", and
    # worse, animspec derives its display case from the filename - so the two
    # commands would write MIXED casing for the same asset
    asset = args.name or os.path.splitext(os.path.basename(ovl_src))[0]
    graph_stem = graph_name.rsplit(".", 1)[0]
    # NOT required to match the graph entry. An earlier version died when they
    # differed, which rejected 49 of the 63 retail assets that have a graph -
    # because the graph is named for the PROP and the file for the ASSET, and they
    # coincide in only 22% of cases (AQ_Doors ships aq_doors01.motiongraph). The
    # loc symbol name is a free choice; the graph entry is what a prefab's
    # MotionGraphName must resolve to. Different things, as name-flow documents
    if asset.lower() != graph_stem.lower():
        logging.info("Cleaning %s: loc symbols will use %r; the graph is %r "
                     "(these differ in most retail assets, which is normal)",
                     os.path.basename(ovl_src), asset, graph_name)
    else:
        logging.info("Cleaning %s against asset name %r",
                     os.path.basename(ovl_src), asset)

    with tempfile.TemporaryDirectory() as work:
        def out(n):
            return os.path.join(work, n)

        paths = {graph_name: ovl.loaders[graph_name].extract(out)[0]}
        for n in (choices_name, enum_name):
            if n:
                paths[n] = ovl.loaders[n].extract(out)[0]

        gtree = ET.parse(paths[graph_name])
        groot = gtree.getroot()
        vars_rename = None

        # --- 1. donor audio/VFX events ---------------------------------------
        n_ev = strip_datastreams(groot)
        logging.info("  stripped %d audio/VFX event element(s) from the graph "
                     "(each may hold several named events)", n_ev)

        # --- 2. the motiongraphvars reference and entry -----------------------
        # The invariant is that the graph's reference RESOLVES to the vars entry -
        # not that either is named anything in particular. Both can be wrong
        # independently, and each is silent:
        #
        #   broken link   ovl.rename renames the ENTRY but NOT this reference,
        #                 because MotiongraphLoader.accept_string only rewrites
        #                 strings containing '@' or '$' (to avoid mangling sound
        #                 event names). So renaming OldAsset -> NewAsset leaves
        #                 <ab>OldAssetVars</ab> pointing at an entry that no
        #                 longer exists
        #   foreign name  entry and reference agree, but carry a DONOR's name
        #                 The asset works and ships someone else's name
        #
        # The reference lives in <ab>, verified across several retail assets
        n_ref = 0
        if vars_name:
            vars_stem = vars_name.rsplit(".", 1)[0]
            # Measured across every retail asset with one graph and one vars
            # (20 of 20): the graph carries exactly TWO <ab> under
            # m_g_two/ptr/nestedzstr/bc - "TriggerSequence" at index 0 and the
            # vars reference at index 1, without exception. Matching by VALUE
            # would fail in the case that matters most, because after a rename
            # the reference still holds the OLD name
            abs_ = [el for el in groot.iter("ab") if el.text and el.text.strip()]
            if len(abs_) != 2:
                logging.warning("  expected 2 <ab> elements (TriggerSequence + the "
                                "vars reference), found %d - leaving the vars "
                                "wiring alone", len(abs_))
            else:
                ref_el = abs_[1]
                ref = ref_el.text.strip()
                foreign = asset.lower() not in vars_stem.lower()
                # a foreign entry gets renamed to <asset>Vars, which is
                # retail's dominant convention
                target_stem = f"{asset}Vars" if foreign else vars_stem
                if ref.lower() != target_stem.lower():
                    ref_el.text = target_stem
                    n_ref = 1
                    logging.info("  vars reference %r -> %r%s", ref, target_stem,
                                 "  (was pointing at a resource that does not exist)"
                                 if ref.lower() != vars_stem.lower() else "")
                else:
                    logging.info("  vars reference %r already resolves to %s",
                                 ref, vars_name)
                if foreign:
                    vars_rename = (vars_stem, target_stem.lower())
                else:
                    vars_rename = None

        # --- 3/4. loc symbols AND durations on the donor's rows ---------------
        # These live in the same loop because they are the same bug shape: a row
        # copied verbatim from the donor by rename, never revisited by anything
        # that has the data to fix it. animspec already computes a real duration
        # for every row IT appends, via durations_from_manis + choice_duration -
        # but that only ever touched new rows. The donor's original rows (every
        # index below whatever animspec later added) kept the donor's numbers
        # forever, which is why an early build shipped Static/Idle01/Idle02/LookPetrify
        # at 1.0/7.3/8.0/3.3s while its own clips run 0.46/3.71/3.71/1.83s
        #
        # durations_from_manis keys on the QUALIFIED clip name (Asset$Clip), so
        # this only fixes rows whose enum name matches one of the asset's own
        # clips - a "Default"/idle-pose row with no exported clip behind it is
        # left untouched, same as before this existed
        manis_name = next((n for n in ovl.loaders if n.endswith(".manis")), None)
        clip_dur = {}
        if manis_name:
            from generated.formats.manis import ManisFile
            try:
                mf = ManisFile()
                mf.load(ovl.loaders[manis_name].extract(out)[0])
                clip_dur = durations_from_manis(mf)
            except Exception as e:
                # TY_PushPuppet's .manis hits a pre-existing ManisFile parser
                # limitation (BufferError deep in a compressed-header read) that
                # nothing exercised before this existed: cleanasset now loads
                # EVERY asset's .manis unconditionally, where previously only
                # animspec did, and only when a human ran it against one
                # specific asset. A retail asset failing to parse is not a
                # reason to abort cleaning it - loc symbols and vars wiring are
                # independent of this and still need fixing. Duration recompute
                # is skipped for this asset only
                logging.warning("  cannot read %s for duration recompute: %r "
                                "- durations left as-is", manis_name, e)
        dur_prefix = None
        if clip_dur:
            try:
                dur_prefix = resolve_prefix(ovl)
            except ValueError as e:
                logging.warning("  cannot recompute durations: %s", e)

        n_sym = 0
        n_dur = 0
        os.makedirs(loc_dir, exist_ok=True)
        if choices_name and enum_name:
            ctree = ET.parse(paths[choices_name])
            croot = ctree.getroot()
            holder = resolve_enum_holder(ET.parse(paths[enum_name]).getroot())
            enums = [(e.text or "").strip() for e in holder]
            choice_els = list(croot.iter("animchoice"))
            if len(choice_els) != len(enums):
                die(f"{len(choice_els)} choice rows vs {len(enums)} enum names - "
                    f"refusing to rewrite symbols positionally.")
            mine = f"{LOC_SYMBOL_ROOT}{asset}_"
            for i, ac in enumerate(choice_els):
                el = ac.find("label")
                raw = (el.text or "").strip() if el is not None and el.text else ""
                if not (raw.startswith("[") and raw.endswith("]")):
                    logging.info("  row %d label %r is not a [symbol] - left alone",
                                 i, raw)
                elif not raw[1:-1].lower().startswith(mine.lower()):
                    new_sym = loc_symbol(asset, enums[i])
                    el.text = f"[{new_sym}]"
                    p = os.path.join(loc_dir, f"{new_sym}.txt")
                    if os.path.dirname(os.path.abspath(p)) != loc_dir:
                        die(f"loc symbol {new_sym!r} escapes {loc_dir}")
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(enums[i])
                    logging.info("  loc symbol [%s] -> [%s]", raw[1:-1], new_sym)
                    n_sym += 1

                if dur_prefix is not None:
                    qualified = f"{dur_prefix}${enums[i]}"
                    real = clip_dur.get(qualified.lower())
                    if real is not None:
                        new_dur = choice_duration(real)
                        old_s = ac.get("duration")
                        old = float(old_s) if old_s is not None else None
                        if old is None or abs(old - new_dur) > 0.001:
                            ac.set("duration", repr(new_dur))
                            logging.info("  row %d (%s) duration %s -> %.4fs "
                                         "(computed from %s)",
                                         i, enums[i], old_s, new_dur, manis_name)
                            n_dur += 1
            ctree.write(paths[choices_name], encoding="utf-8", xml_declaration=False)
            logging.info("  %d donor loc symbol(s) rewritten, .txt shipped for each; "
                         "%d row duration(s) recomputed from the asset's own clips",
                         n_sym, n_dur)

        gtree.write(paths[graph_name], encoding="utf-8", xml_declaration=False)

        inject = [paths[graph_name]] + ([paths[choices_name]] if choices_name else [])
        try:
            ovl.add_files(inject, work)
        except Exception as e:
            die(f"re-injecting the cleaned files failed: {e!r}")

        # rename the vars ENTRY last, so the reference edited above matches it
        if vars_rename:
            old_stem, new_stem = vars_rename
            try:
                ovl.rename([(old_stem, new_stem)])
                logging.info("  entry %s -> %s.motiongraphvars", vars_name, new_stem)
            except Exception as e:
                die(f"renaming the vars entry failed: {e!r}")

        out_ovl = ovl_src if args.in_place else os.path.abspath(args.output)
        try:
            ovl.save(out_ovl, commands={"update_aux": False})
        except Exception as e:
            die(f"OvlFile.save failed: {e!r}")

    logging.success("Cleaned %s: %d event element(s), %d vars ref(s), %d loc symbol(s)",
                    os.path.basename(out_ovl), n_ev, n_ref, n_sym)


def cmd_syncprefab(args: argparse.Namespace) -> None:
    """Point a prefab .lua at the asset it is supposed to drive.

    A prefab names its resources by BASENAME: MotionGraphName = "MyProp"
    resolves to myprop.motiongraph. If they disagree the prop still places and
    simply never animates - no error, in the tool or in game - so this is the
    single easiest way to lose an afternoon.

    Nothing else can check it: the prefab is authored source that ends up in the
    MOD's ovl, while the graph lives in the ASSET's ovl, so no other step sees both.

    Reads the asset for the entries that actually exist and rewrites the prefab's
    names to match. Case is preserved when the existing value is already correct
    case-insensitively - entry names are lowercased by the OVL while prefabs use
    display case, and lookup does not care, so churning "MyProp" into
    "myprop" would be noise.
    """
    lua_path = os.path.abspath(args.prefab)
    ensure_exists(lua_path, "file")
    asset = os.path.abspath(args.asset)
    ensure_exists(asset, "file")

    game = resolve_game_label(args.game)
    ovl = OvlFile()
    ovl.game = game
    ovl.load_hash_table()
    try:
        ovl.clear()
        ovl.load(asset, {"game": game} if game else {})
    except Exception as e:
        die(f"OvlFile.load failed: {e!r}")

    # key -> the extension whose entry it must resolve to
    WANT = {"MotionGraphName": ".motiongraph",
            "AnimChoiceSettingsResource": ".sceneryanimchoices",
            "ModelName": ".mdl2"}
    stems = {}
    for key, ext in WANT.items():
        hits = sorted(n.rsplit(".", 1)[0] for n in ovl.loaders if n.endswith(ext))
        if len(hits) == 1:
            stems[key] = hits[0]
        elif len(hits) > 1:
            # Multi-prop assets are real - retail ships assets with one graph per
            # prop - and picking one arbitrarily would be a coin flip
            logging.warning("%s: asset has %d %s entries (%s); leaving %s alone - "
                            "a multi-prop asset needs a name per prop.",
                            os.path.basename(asset), len(hits), ext, hits[:3], key)

    if not stems:
        die(f"{asset} has none of {', '.join(WANT.values())} - is this the right "
            f"asset ovl?")

    with open(lua_path, encoding="utf-8") as f:
        text = f.read()
    changed, already = [], []
    for key, stem in stems.items():
        def _sub(m, k=key, s=stem):
            cur = m.group(1)
            if cur.lower() == s.lower():
                already.append((k, cur))
                return m.group(0)          # correct already - do not churn case
            changed.append((k, cur, s))
            return f'{k} = "{s}"'
        text = re.sub(rf'{key}\s*=\s*"([^"]*)"', _sub, text)

    if args.check:
        for k, cur, s in changed:
            logging.error("  %s = %r should be %r", k, cur, s)
        if changed:
            die(f"{len(changed)} prefab name(s) do not match {os.path.basename(asset)}. "
                f"Re-run without --check to fix them.")
        logging.success("%s: all names match %s", os.path.basename(lua_path),
                        os.path.basename(asset))
        return

    out = os.path.abspath(args.output) if args.output else lua_path
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    for k, cur, s in changed:
        logging.info("  %s %r -> %r", k, cur, s)
    for k, cur in already:
        logging.info("  %s %r already correct", k, cur)
    logging.success("Synced %s against %s (%d changed)",
                    os.path.basename(out), os.path.basename(asset), len(changed))


def cmd_rename(args: argparse.Namespace) -> None:
    """Retarget a whole asset: entry names AND the references inside files.

    This is the load-bearing step in building custom animated scenery, and it was
    previously GUI-only. A .motiongraph cannot survive extract-to-XML and repack -
    cobra's writer loses roughly a third of the pointer relocations - so the working
    approach is to take a retail animated asset and RENAME it. Renaming edits
    strings in the existing pools and re-points fragments, so nothing is rebuilt and
    the graph is preserved exactly.

    Both halves are required and neither is sufficient alone:
        rename_contents   strings INSIDE files - clip refs in the graph, the
                          MotionGraphName in a prefab .lua, material names
        rename            the entry names themselves
    Renaming only the entries leaves every reference pointing at the old name;
    renaming only the contents leaves the entries misnamed. Both are applied here,
    in both the given case and lowercase, because the OVL lowercases entry names
    while references keep display case.

    Substring-based, which is the one thing to watch: a sub-resource whose name does
    not contain the old asset name is NOT caught. That is not hypothetical - retail
    assets ship sub-resources named for a different asset entirely, and those
    survive the rename untouched.
    """
    ovl_src = os.path.abspath(args.ovl)
    ensure_exists(ovl_src, "file")
    if not args.in_place and not args.output:
        die("rename needs -o/--output or --in-place.")
    old, new = args.old, args.new
    if old == new:
        die(f"--from and --to are both {old!r}; nothing to do.")

    game = resolve_game_label(args.game)
    ovl = OvlFile()
    ovl.game = game
    ovl.load_hash_table()
    try:
        ovl.clear()
        ovl.load(ovl_src, {"game": game} if game else {})
    except Exception as e:
        die(f"OvlFile.load failed: {e!r}")

    before = sorted(ovl.loaders)
    hits = [n for n in before if old.lower() in n.lower()]
    if not hits:
        die(f"no entry in {ovl_src} contains {old!r}. Check the name - renaming "
            f"against a string the asset does not use silently does nothing.")
    logging.info("%d of %d entries carry %r", len(hits), len(before), old)

    # Entry names are LOWERCASED by the OVL; references inside files keep display
    # case. One tuple can only match one of those, so a rename given the wrong case
    # renames the entries and leaves every reference pointing at the old name - a
    # silently BROKEN asset. Cover both by deriving the missing variant, and by
    # looking up the display case in the files when only lowercase was supplied,
    # since it cannot be derived from a lowercase string
    tups = [(old, new)]
    if old.lower() != old:
        tups.append((old.lower(), new.lower()))
    else:
        found = _display_case_in_files(ovl, old)
        for disp in found:
            # map the real cased spelling onto `new` with the same shape
            tups.append((disp, new.upper() if disp.isupper() else new))
        if found:
            logging.info("also renaming the display-case spelling(s) found in "
                         "file contents: %s", found)

    try:
        ovl.rename_contents(tups, None)
        ovl.rename(tups)
    except Exception as e:
        die(f"rename failed: {e!r}")

    after = sorted(ovl.loaders)
    still = [n for n in after if old.lower() in n.lower()]
    logging.info("renamed %d entrie(s)", len(hits) - len(still))
    if still:
        # Not fatal: a sub-resource can legitimately be named for something else
        # Say so, because these are exactly what gets forgotten
        logging.warning("%d entrie(s) still contain %r and were NOT renamed - a "
                        "sub-resource named for something other than the asset. "
                        "Check whether they belong to this asset: %s",
                        len(still), old, still[:5])

    # The .motiongraphvars reference is renamed here rather than left to the
    # post-condition, because rename BREAKS it on essentially every asset and
    # refusing would make the command unusable. The entry's name contains the asset
    # name so it gets renamed; the reference does not contain '$', so
    # MotiongraphLoader.accept_string skips it and it keeps pointing at the old
    # name. Repair it the same way cleanasset does
    _repair_vars_ref(ovl)

    # POST-CONDITION. Verify the rename left the asset coherent before writing it
    # out: every clip the graph references, AND the vars reference, must still
    # resolve. This is what turns a silently broken asset into a failed command
    dangling = _check_clip_refs_resolve(ovl)
    if dangling:
        die(f"rename left {len(dangling)} clip reference(s) pointing at entries "
            f"that no longer exist: {dangling[:4]}"
            f"{' ...' if len(dangling) > 4 else ''}. Nothing was written.\n"
            f"  The graph and the entry names disagree, which usually means the "
            f"case was wrong: OVL entry names are lowercased, but references "
            f"inside files keep display case. Re-run with --from spelled as it "
            f"appears in the graph, e.g. {dangling[0].split('$')[0]!r}.")

    out_ovl = ovl_src if args.in_place else os.path.abspath(args.output)
    try:
        ovl.save(out_ovl, commands={"update_aux": False})
    except Exception as e:
        die(f"OvlFile.save failed: {e!r}")
    logging.success("Renamed %s -> %s in %s", old, new, out_ovl)


def cmd_inject(args: argparse.Namespace) -> None:
    """
    Inject files into an OVL, using OvlFile.add_files(files, common_root_dir)
    (same pattern as MainWindow.inject_files in the GUI).
    """
    ovl_src = os.path.abspath(args.ovl)
    ensure_exists(ovl_src, "file")

    game = resolve_game_label(args.game)
    commands = {}
    if game:
        commands["game"] = game
        logging.info("Using game preset: %s", game)
    else:
        logging.info("No game preset supplied; OvlFile will auto-detect.")

    ovl = OvlFile()
    ovl.game = game
    ovl.load_hash_table()

    logging.info("Loading archive %s", ovl_src)

    try:
        ovl.clear()
        ovl.load(ovl_src, commands)
    except Exception as e:
        die(f"OvlFile.load failed: {e!r}")

    try:
        logging.info("Detected game from archive: %s", getattr(ovl, "game", "<unknown>"))
    except Exception:
        pass


    # Collect files to inject
    files_to_inject: List[str] = []

    if args.input:
        in_dir = os.path.abspath(args.input)
        ensure_exists(in_dir, "dir")
        for root, _, files in os.walk(in_dir):
            for name in files:
                files_to_inject.append(os.path.join(root, name))

    if args.file:
        for p in args.file:
            files_to_inject.append(os.path.abspath(p))

    if not files_to_inject:
        die("No files to inject (use --input folder and/or --file path).")

    files_to_inject = sorted(set(files_to_inject))

    # Choose a common root like the GUI does for relative names
    if args.input:
        common_root = os.path.abspath(args.input)
    else:
        common_root = find_common_root(files_to_inject)
        if not common_root:
            die("Could not determine common root directory for injected files.")

    logging.info("Injecting %d files (root: %s)", len(files_to_inject), common_root)

    # --replace-clips: drop the asset's existing animations first, so it ends up
    # with ONLY the injected ones. Opt-in rather than the default, because inject
    # is a general command and silently special-casing one extension would be
    # surprising - and because an asset can legitimately hold several .manis
    # (retail SC_Villager_Animatronic and TY_Animatronics ship three each)
    if getattr(args, "replace_clips", False):
        manis_in = [p for p in files_to_inject if p.lower().endswith(".manis")]
        if not manis_in:
            die("--replace-clips needs at least one .manis among the injected "
                "files; nothing would replace the animations being removed.")
        from modules.motiongraph_author import replace_clips, apply_clip_prefix
        try:
            removed, _ = replace_clips(ovl, manis_in)
        except Exception as e:
            die(f"--replace-clips failed: {e!r}")
        logging.info("Replaced animations: removed %d existing entrie(s), "
                     "injected %d .manis", len(removed), len(manis_in))

        # Blender names an Action for the CLIP ALONE ("MyClip") and export_manis
        # writes that verbatim, so a freshly exported .manis has BARE clips while
        # the graph references <Prefix>$Clip. Qualify them here or every reference
        # dangles and the prop animates nothing - silently
        #
        # The prefix comes from the GRAPH, not from the .mani entries: those were
        # just replaced, so there is nothing prefixed left to read it off
        bare = [n for n in ovl.loaders if n.endswith(".mani") and "$" not in n]
        if bare:
            prefix = _prefix_from_graph_refs(ovl)
            if prefix is None:
                die(f"{len(bare)} injected clip(s) have no asset prefix and the "
                    f"graph names none to adopt: {sorted(bare)[:4]}. The graph "
                    f"references clips as <Prefix>$Clip, so bare clips would "
                    f"resolve to nothing.")
            try:
                pairs = apply_clip_prefix(ovl, prefix)
            except Exception as e:
                die(f"could not qualify the injected clips: {e!r}")
            logging.info("Qualified %d bare clip(s) with the prefix %r read from "
                         "the graph", len(pairs), prefix)
        files_to_inject = [p for p in files_to_inject if p not in manis_in]
        if not files_to_inject:
            logging.info("Nothing further to inject.")

    try:
        if files_to_inject:
            ovl.add_files(files_to_inject, common_root)
    except Exception as e:
        die(f"OvlFile.add_files failed: {e!r}")

    # Decide output path
    if args.in_place:
        out_ovl = ovl_src
    else:
        if not args.output:
            die("You must specify --output when not using --in-place.")
        out_ovl = os.path.abspath(args.output)

    commands_save = {"update_aux": args.update_aux}

    logging.info("Saving archive to %s", out_ovl)
    try:
        ovl.save(out_ovl, commands=commands_save)
    except Exception as e:
        die(f"OvlFile.save failed: {e!r}")

    logging.success("Injected files into %s", out_ovl)


# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Command-line OVL tool using cobra-tools' OvlFile."
    )

    sub = parser.add_subparsers(dest="command", required=True)

    game_vals = game_choices()
    comp_vals = compression_choices()

    # new
    p_new = sub.add_parser(
        "new",
        help="Create a new OVL from a folder (File > New from folder).",
    )
    p_new.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_new.add_argument(
        "-i", "--input",
        help="Input folder containing files to pack into the OVL.",
        required=True,
    )
    p_new.add_argument(
        "-o", "--output",
        help="Output .ovl file path.",
        required=True,
    )
    p_new.add_argument(
        "-c", "--compression",
        help="Compression method (matches GUI 'Compression').",
        choices=comp_vals if comp_vals else None,
    )
    p_new.add_argument(
        "--update-aux",
        action="store_true",
        help="Set commands['update_aux']=True when saving.",
    )
    p_new.add_argument(
        "-f", "--force",
        action="store_true",
        help="Overwrite output file if it already exists.",
    )
    p_new.set_defaults(func=cmd_new)

    # extract
    p_ext = sub.add_parser(
        "extract",
        help="Extract files from an OVL.",
    )
    p_ext.add_argument(
        "ovl",
        help="Path to the .ovl file to extract.",
    )
    p_ext.add_argument(
        "-o", "--output",
        help=(
            "Output folder (created if missing). "
            "If omitted, a folder named after the OVL file will be created "
            "next to the OVL (e.g. Main.ovl -> Main/)."
        )
    ),
    p_ext.add_argument(
        "-g", "--game",
        help="Game identifier (optional; if omitted, OvlFile may auto-detect).",
        choices=game_vals if game_vals else None,
    )
    p_ext.add_argument(
        "--type",
        action="append",
        default=[],
        help="Restrict extraction to specific file types/extensions (can repeat).",
    )
    p_ext.add_argument(
        "--name",
        action="append",
        default=[],
        help="Restrict extraction to specific internal entry names (can repeat).",
    )
    p_ext.set_defaults(func=cmd_extract)

    # inject
    # cleanasset
    p_cln = sub.add_parser(
        "cleanasset",
        help="Strip a donor's residue from an asset created by renaming a retail one.",
    )
    p_cln.add_argument("ovl", help="Path to the asset .ovl to clean.")
    p_cln.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_cln.add_argument(
        "--loc-dir", required=True,
        help="Folder to write the replacement localisation .txt files into. Pack "
             "these into your mod alongside the asset.",
    )
    p_cln.add_argument(
        "--name",
        help="Asset name for the new loc symbols. Defaults to the .motiongraph "
             "entry stem, which is what a prefab's MotionGraphName resolves to.",
    )
    p_cln.add_argument(
        "--in-place", action="store_true",
        help="Modify the OVL in place (overwrite the input file).",
    )
    p_cln.add_argument(
        "-o", "--output",
        help="Output .ovl file path (required unless --in-place).",
    )
    p_cln.set_defaults(func=cmd_cleanasset)

    # syncprefab
    p_syn = sub.add_parser(
        "syncprefab",
        help="Point a prefab .lua at the asset it drives (or --check it).",
    )
    p_syn.add_argument("prefab", help="Path to the prefab .lua source file.")
    p_syn.add_argument(
        "-a", "--asset", required=True,
        help="The asset .ovl whose entries the prefab must name.",
    )
    p_syn.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_syn.add_argument(
        "--check", action="store_true",
        help="Report mismatches and exit non-zero WITHOUT editing. For a build "
             "gate, where silently rewriting authored source would be wrong.",
    )
    p_syn.add_argument(
        "-o", "--output",
        help="Write the result here instead of editing the prefab in place.",
    )
    p_syn.set_defaults(func=cmd_syncprefab)

    # rename
    p_ren = sub.add_parser(
        "rename",
        help="Retarget an asset: rename entries AND the references inside files.",
    )
    p_ren.add_argument("ovl", help="Path to the .ovl file to retarget.")
    p_ren.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_ren.add_argument(
        "--from", dest="old", required=True,
        help="The name to replace, e.g. the donor asset's name.",
    )
    p_ren.add_argument(
        "--to", dest="new", required=True,
        help="Your asset name, e.g. MyProp. Applied in the given case and in "
             "lowercase, since the OVL lowercases entry names while references "
             "inside files keep display case.",
    )
    p_ren.add_argument(
        "--in-place", action="store_true",
        help="Modify the OVL in place (overwrite the input file).",
    )
    p_ren.add_argument(
        "-o", "--output",
        help="Output .ovl file path (required unless --in-place).",
    )
    p_ren.set_defaults(func=cmd_rename)

    p_bp = sub.add_parser(
        "buildprop",
        help="Build an animated scenery prop asset from scratch: a CREATED "
             "container with injected art and a GENERATED motiongraph, "
             "enumnamer and choice table. Nothing is cloned from an existing "
             "asset - to reuse content from one, inject into it directly "
             "with the 'inject' or 'rename' command instead.",
    )
    p_bp.add_argument("asset", help="Name of the new asset, e.g. MyProp.")
    p_bp.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_bp.add_argument("--art", required=True,
                      help="Folder with the exported .ms2/.manis, any .fgm/"
                           ".tex materials, and <asset>.animspec.json. A "
                           ".motiongraphvars here is used if present; if not, "
                           "one is generated for you. Materials are optional, "
                           "same as any other cobra mesh - an unbound "
                           "material is caught at build time, not required "
                           "up front.")
    p_bp.add_argument("--dest", required=True,
                      help="Output folder for <asset>.ovl and sidecars - "
                           "created if missing, never wiped; an existing "
                           "<asset>.ovl or loc .txt there is overwritten "
                           "(the GUI asks first, the CLI does not).")
    p_bp.add_argument("--vars-ref", default=None,
                      help="Name of the .motiongraphvars entry the new graph "
                           "references. Optional: defaults to the one --art "
                           "supplies, or <asset>Vars generated for you if "
                           "--art supplies none.")
    p_bp.add_argument("--blend-time", type=float, default=None,
                      help="State blend seconds; overrides the animspec's "
                           "value (default: spec value, else 0.15).")
    p_bp.set_defaults(func=cmd_buildprop)

    p_aa = sub.add_parser(
        "apply-animspec",
        help="Add a generated motiongraph/enumnamer/choice table to an OVL "
             "that is already packed - the CLI counterpart to the GUI's "
             "Util > Apply Animation Spec. Not the same as 'animspec' below, "
             "which appends to an existing DONOR graph instead of "
             "generating one.",
    )
    p_aa.add_argument("ovl", help="Path to the already-packed .ovl to update, in place.")
    p_aa.add_argument("-s", "--spec", required=True,
                      help="Path to the <asset>.animspec.json exported from Blender.")
    p_aa.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_aa.add_argument("--asset", default=None,
                      help="Asset name the graph is generated for; defaults "
                           "to the OVL's own filename (New from Folder "
                           "already names it this way).")
    p_aa.add_argument("--blend-time", type=float, default=None,
                      help="State blend seconds; overrides the animspec's "
                           "value (default: spec value, else 0.15).")
    p_aa.set_defaults(func=cmd_apply_animspec)

    p_inj = sub.add_parser(
        "inject",
        help="Inject/replace files into an OVL.",
    )
    p_inj.add_argument(
        "ovl",
        help="Path to the .ovl file to modify.",
    )
    p_inj.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_inj.add_argument(
        "-i", "--input",
        help="Folder whose contents will be injected (recursively).",
    )
    p_inj.add_argument(
        "-f", "--file",
        action="append",
        default=[],
        help="Individual file path(s) to inject. Can be repeated.",
    )
    p_inj.add_argument(
        "--in-place",
        action="store_true",
        help="Modify the OVL in place (overwrite the input file).",
    )
    p_inj.add_argument(
        "-o", "--output",
        help="Output .ovl file path (required unless --in-place).",
    )
    p_inj.add_argument(
        "--update-aux",
        action="store_true",
        help="Set commands['update_aux']=True when saving.",
    )
    p_inj.add_argument(
        "--replace-clips",
        action="store_true",
        help="Remove the OVL's existing .manis/.mani entries before injecting, so "
             "the asset keeps ONLY the injected animations. Use when building a new "
             "model from a donor: the donor's clips animate a skeleton your model "
             "does not have. Without this, an injected .manis replaces the old one "
             "only if it happens to carry the same entry name (an opaque hash like "
             "animation.manisetb896236e.manis); under any other name both sets "
             "survive, silently.",
    )
    p_inj.set_defaults(func=cmd_inject)

    # animspec
    p_spec = sub.add_parser(
        "animspec",
        help="Append animation choices to an animated scenery OVL from a spec "
             "written by Blender's File > Export > Cobra Animation Spec.",
    )
    p_spec.add_argument(
        "ovl",
        help="Path to the animated scenery .ovl to modify. It must already contain "
             "a .motiongraph, a .sceneryanimchoices and an .enumnamer - the choices "
             "are APPENDED to those, not created from nothing.",
    )
    p_spec.add_argument(
        "-s", "--spec",
        required=True,
        help="Animation choice spec (.json).",
    )
    p_spec.add_argument(
        "-g", "--game",
        help="Game identifier (matches GUI 'Game' dropdown).",
        choices=game_vals if game_vals else None,
        required=True,
    )
    p_spec.add_argument(
        "--loc-dir",
        help="Where to write the <label_symbol>.txt localisation files. Defaults "
             "to the spec's own folder. The game resolves the [symbol] stored in "
             "the choice row against these.",
    )
    p_spec.add_argument(
        "--in-place",
        action="store_true",
        help="Modify the OVL in place (overwrite the input file).",
    )
    p_spec.add_argument(
        "-o", "--output",
        help="Output .ovl file path (required unless --in-place).",
    )
    p_spec.set_defaults(func=cmd_animspec)

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
