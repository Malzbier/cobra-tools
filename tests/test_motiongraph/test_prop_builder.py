"""prop_builder assembles a shippable prop, and every failure it does not catch
is SILENT in game: a dropdown entry that plays nothing, a material that renders
unlit white, a graph pointing at a variable set that is not there. The refusals
ARE the contract, so they are what is pinned here, together with the byte-exact
loc text the caller's mod ships and the "a failed build leaves nothing behind"
promise the write tail makes.

read_animspec and the PropBuildError basics are pinned in test_spec.py; this
covers everything around them. No game install and no retail OVL is needed: the
container is a real OvlFile carrying the generated XML, and the art that cannot
be generated (.manis, .ms2) is stubbed at the loader boundary, which is the
only surface prop_builder touches it through.

The loc-text pair carries its own class, because those files have to come out
identical on Windows, macOS and Linux: pinned encoding, no BOM, no newline
translation, and symbols that cannot collide on a case-insensitive filesystem.
"""
import json
import os
import tempfile

import pytest

from generated.formats.ovl import OvlFile
from modules import prop_builder
from modules.motiongraph_author import parse_events
from modules.prop_builder import (PropBuildError, animspec_path,
								  apply_animspec, apply_animspec_to, build,
								  build_from_animspec, existing_loc_texts,
								  existing_outputs, planned_outputs,
								  write_loc_texts)
from utils import config

# (enum, clip, weight, label, events) - choice 0 is the clipless Auto choice,
# exactly what read_animspec synthesizes
CHOICES = [("Default", None, None, "Auto", ()),
		   ("Static", "ClipA", 1, "Rest", ()),
		   ("Play", "ClipB", 3, "Playing", ())]

CLIPS = {"MyProp$ClipA": 1.0, "MyProp$ClipB": 2.0}


def quiet(msg):
	"""build()'s log hook, silenced."""


def write_spec(path, **overrides):
	payload = {
		"spec_version": 2,
		"clips": [
			{"clip": "ClipA", "enum_name": "Static", "label_text": "Rest",
			 "loop": True, "weight": 1},
			{"clip": "ClipB", "enum_name": "Play", "label_text": "Playing",
			 "loop": True, "weight": 3},
		],
	}
	payload.update(overrides)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh)
	return path


def loc_report(*pairs):
	"""The two report keys the loc functions read, as (symbol, text) pairs."""
	return {"loc_symbols": [s for s, _ in pairs],
			"labels": [(s, t) for s, t in pairs]}


class StubLoader:
	"""The minimum OvlFile asks of an entry it did not create itself:
	send_files reads name/ext/children, validate_loaders calls validate() and
	reads ovs_name, and prop_builder reads it back through extract()."""

	def __init__(self, name, payload=b""):
		self.name = name
		self.ext = os.path.splitext(name)[1]
		self.ovs_name = "STATIC"
		self.children = ()
		self.payload = payload

	def validate(self):
		pass

	def extract(self, out_dir_func):
		path = out_dir_func(self.name)
		with open(path, "wb") as fh:
			fh.write(self.payload)
		# real loaders chatter on stdout; _extract_one must swallow it
		print(f"extracting {self.name}")
		return (path,)


class Ms2Stub(StubLoader):
	"""An .ms2 entry is only ever read through its buffer datas, and only for
	the ASCII runs that are material names."""

	def __init__(self, name, blob):
		super().__init__(name)
		self.blob = blob

	def get_ms2_buffer_datas(self):
		return (self.blob, b"", b"")


def manis_stub(name, clips):
	"""A .manis entry whose 'binary' is the {clip: duration} map FakeManis
	reads back, so a test declares clips as data instead of shipping a
	fixture."""
	return StubLoader(name, json.dumps(clips).encode("utf-8"))


class FakeManiInfo:

	def __init__(self, name, duration):
		self.name = name
		self.duration = duration


class FakeManis:
	"""ManisFile stand-in. It is bound into prop_builder's own namespace at
	import, so replacing it there exercises the whole clip/duration half of
	the assembly without a .manis binary anywhere."""

	def __init__(self):
		self.mani_infos = []

	def load(self, path):
		with open(path, encoding="utf-8") as fh:
			self.mani_infos = [FakeManiInfo(n, d)
							   for n, d in json.load(fh).items()]


class FakeOvl:
	"""An OvlFile as far as build() and apply_animspec_to() reach into one: a
	loaders dict plus a record of what was added, removed, renamed and saved."""

	def __init__(self, names=(), filepath=""):
		self.loaders = {n: StubLoader(n) for n in names}
		self.filepath = filepath
		self.added = []
		self.removed = []
		self.saved = []

	def add_files(self, paths, common_root_dir=None):
		self.added.append(sorted(os.path.basename(p) for p in paths))

	def remove(self, names):
		self.removed.append(sorted(names))
		for n in names:
			del self.loaders[n]

	def rename(self, pairs):
		for old, new in pairs:
			self.loaders[f"{new}.mani"] = self.loaders.pop(f"{old}.mani")

	def save(self, path, commands=None):
		self.saved.append(path)
		with open(path, "wb") as fh:
			fh.write(b"OVL")


@pytest.fixture
def fake_manis(monkeypatch):
	monkeypatch.setattr(prop_builder, "ManisFile", FakeManis)


def packed(*loaders):
	"""A real OvlFile - hash table and all - carrying `loaders` as if they had
	been packed into it. Everything prop_builder generates is added to this for
	real; only the art it cannot generate is stubbed."""
	o = OvlFile()
	o.reporter = prop_builder._RaisingReporter()
	o.game = "Planet Coaster 2"
	o.load_hash_table()
	for lo in loaders:
		o.loaders[lo.name] = lo
	return o


def generate_into(ovl, asset="MyProp", choices=None, vars_ref="MyPropVars",
				  blend_time=0.1, game="Planet Coaster 2", src_hint="art",
				  out_ovl="out.ovl"):
	return prop_builder._generate_into(
		ovl, asset, CHOICES if choices is None else choices, vars_ref,
		blend_time, game, src_hint=src_hint, out_ovl=out_ovl, log=quiet)


def art_dir_with(tmp_path, *names):
	art = tmp_path / "art"
	art.mkdir(exist_ok=True)
	for n in names:
		(art / n).write_bytes(b"")
	return str(art)


def build_args(tmp_path, **overrides):
	args = dict(cobra_dir=str(tmp_path), asset="MyProp",
				art_dir=str(tmp_path / "art"), dest_dir=str(tmp_path / "out"),
				choices=CHOICES, blend_time=0.1, log=quiet)
	args.update(overrides)
	return args


class _Seam:
	"""build()'s two pieces of infrastructure, replaced: the OvlFile it makes
	for itself and the generation step. What is left is the validation prologue
	and the write tail, which is what the tests using this are about."""

	def __init__(self):
		self.ovl = FakeOvl()
		self.calls = []
		self.error = None
		self.report = loc_report(("InfoPanel_AnimationType_MyProp_Default", "Auto"),
								 ("InfoPanel_AnimationType_MyProp_Static", "Rest"))

	def generate_into(self, ovl, asset, choices, vars_ref, blend_time, game,
					  src_hint, out_ovl, log=print):
		self.calls.append({"asset": asset, "choices": choices,
						   "vars_ref": vars_ref, "blend_time": blend_time,
						   "game": game, "src_hint": src_hint,
						   "out_ovl": out_ovl})
		if self.error:
			raise self.error
		return dict(self.report, out_ovl=out_ovl)


@pytest.fixture
def seam(monkeypatch):
	s = _Seam()
	monkeypatch.setattr(prop_builder, "_fresh", lambda cfg, game, path=None: s.ovl)
	monkeypatch.setattr(prop_builder, "_generate_into", s.generate_into)
	return s


class TestAnimspecPath:
	"""One convention for where a spec lives; two callers must agree on it."""

	def test_asset_is_lowercased(self):
		assert animspec_path("art", "MyProp") == os.path.join(
			"art", "myprop.animspec.json")

	def test_already_lowercase_is_unchanged(self):
		assert animspec_path("art", "myprop") == os.path.join(
			"art", "myprop.animspec.json")


class TestSoleVarsRef:

	def test_none_packed_falls_back_to_the_retail_convention(self):
		assert prop_builder._sole_vars_ref(
			FakeOvl(["myprop.ms2"]), "p.ovl", "MyProp") == "MyPropVars"

	def test_a_packed_one_wins_over_the_default(self):
		# shared variable sets under other names exist, so the file always wins
		assert prop_builder._sole_vars_ref(
			FakeOvl(["shared.motiongraphvars"]), "p.ovl", "MyProp") == "shared"

	def test_several_have_no_answer_and_say_so(self):
		o = FakeOvl(["a.motiongraphvars", "b.motiongraphvars"])
		with pytest.raises(PropBuildError) as e:
			prop_builder._sole_vars_ref(o, os.path.join("d", "p.ovl"), "MyProp")
		assert "several" in str(e.value)
		# the message names the file, not the whole path the caller passed
		assert "p.ovl" in str(e.value) and os.path.join("d", "p") not in str(e.value)


class TestGraphEntry:

	def test_finds_the_generated_graph(self):
		assert prop_builder._graph_entry(
			FakeOvl(["myprop.motiongraph", "myprop.ms2"])) == "myprop.motiongraph"

	def test_absence_is_diagnosed_not_a_bare_stopiteration(self):
		# add_files logs a failed create() without raising, so the graph going
		# missing surfaces here and has to explain itself
		with pytest.raises(PropBuildError, match="no .motiongraph entry"):
			prop_builder._graph_entry(FakeOvl(["myprop.ms2"]))


class TestExtractOne:

	def test_returns_the_first_path_extract_wrote(self, tmp_path):
		o = FakeOvl()
		o.loaders["a.motiongraph"] = StubLoader("a.motiongraph", b"<x/>")
		path = prop_builder._extract_one(o, "a.motiongraph", str(tmp_path))
		assert path == os.path.join(str(tmp_path), "a.motiongraph")
		with open(path, "rb") as fh:
			assert fh.read() == b"<x/>"

	def test_loader_chatter_never_reaches_stdout(self, tmp_path, capsys):
		o = FakeOvl()
		o.loaders["a.motiongraph"] = StubLoader("a.motiongraph", b"<x/>")
		prop_builder._extract_one(o, "a.motiongraph", str(tmp_path))
		assert capsys.readouterr().out == ""


class TestRaisingSignal:

	def test_details_are_appended_to_the_message(self):
		with pytest.raises(PropBuildError) as e:
			prop_builder._RaisingSignal().emit(("summary", "a.ms2\nb.ms2"))
		assert str(e.value) == "summary\na.ms2\nb.ms2"

	def test_no_details_leaves_a_bare_message(self):
		with pytest.raises(PropBuildError) as e:
			prop_builder._RaisingSignal().emit(("summary", ""))
		assert str(e.value) == "summary"

	def test_connect_is_a_no_op(self):
		assert prop_builder._RaisingSignal().connect(print) is None


class TestRaisingReporter:
	"""The module's error spine: add_files collects per-file create() failures
	and a bare DummyReporter drops them, which is how an asset ships with an
	entry silently missing."""

	def test_collected_failures_become_a_named_error(self):
		reporter = prop_builder._RaisingReporter()
		with pytest.raises(PropBuildError) as e:
			with reporter.report_error_files("Adding") as error_files:
				error_files.append("broken.ms2")
		assert "Adding 1 files failed" in str(e.value)
		assert "broken.ms2" in str(e.value)

	def test_a_clean_run_raises_nothing(self):
		reporter = prop_builder._RaisingReporter()
		with reporter.report_error_files("Adding") as error_files:
			assert error_files == []

	def test_only_the_warning_channel_is_rewired(self):
		# the other signals stay DummySignals; a progress tick is not a failure
		reporter = prop_builder._RaisingReporter()
		assert isinstance(reporter.warning_msg, prop_builder._RaisingSignal)
		assert reporter.progress_percentage.emit(50) is None
		assert reporter.files_list.emit([]) is None


class TestFresh:

	def test_a_new_container_carries_the_raising_reporter(self, tmp_path):
		cfg = config.Config(str(tmp_path))
		cfg.load()
		o = prop_builder._fresh(cfg, "Planet Coaster 2")
		assert isinstance(o.reporter, prop_builder._RaisingReporter)
		assert o.game == "Planet Coaster 2"
		assert o.cfg is cfg
		assert o.loaders == {}

	def test_a_path_is_loaded_with_the_game_forced(self, monkeypatch):
		class Recorder:
			def __init__(self):
				self.loaded = []

			def load_hash_table(self):
				pass

			def load(self, path, commands=None):
				self.loaded.append((path, commands))

		monkeypatch.setattr(prop_builder, "OvlFile", Recorder)
		o = prop_builder._fresh({}, "Planet Coaster 2", "x.ovl")
		assert o.loaded == [("x.ovl", {"game": "Planet Coaster 2"})]


class TestSpecRefusalsCarryTheirPath:
	"""parse_spec's rules themselves are pinned in test_spec.py; what the
	reader adds on top is naming WHICH spec was refused, and a mod folder holds
	one per asset."""

	def test_a_validation_refusal_names_the_file(self, tmp_path):
		path = write_spec(str(tmp_path / "myprop.animspec.json"),
						  clips=[{"clip": "ClipA", "enum_name": "../evil",
								  "label_text": "x", "loop": True}])
		with pytest.raises(PropBuildError) as e:
			planned_outputs("MyProp", str(tmp_path), path)
		assert str(e.value).startswith(f"{path}: ")
		assert "localisation" in str(e.value)


class TestPlannedOutputs:
	"""Every output listed on the same terms BEFORE anything is written - the
	OVL used to overwrite silently while its loc text asked."""

	def test_the_ovl_comes_first_and_is_not_text(self, tmp_path):
		spec = write_spec(str(tmp_path / "myprop.animspec.json"))
		planned = planned_outputs("MyProp", str(tmp_path), spec)
		assert planned[0] == (os.path.join(str(tmp_path), "MyProp.ovl"), None)

	def test_one_loc_file_per_dropdown_entry_including_auto(self, tmp_path):
		spec = write_spec(str(tmp_path / "myprop.animspec.json"))
		planned = planned_outputs("MyProp", str(tmp_path), spec)
		assert planned[1:] == [
			(os.path.join(str(tmp_path), "InfoPanel_AnimationType_MyProp_Default.txt"), "Auto"),
			(os.path.join(str(tmp_path), "InfoPanel_AnimationType_MyProp_Static.txt"), "Rest"),
			(os.path.join(str(tmp_path), "InfoPanel_AnimationType_MyProp_Play.txt"), "Playing"),
		]


class TestExistingOutputs:

	def test_nothing_on_disk_is_nothing_to_ask_about(self, tmp_path):
		planned = [(str(tmp_path / "MyProp.ovl"), None),
				   (str(tmp_path / "A.txt"), "new")]
		assert existing_outputs(planned) == []

	def test_a_text_output_shows_its_before_and_after(self, tmp_path):
		(tmp_path / "A.txt").write_text("old", encoding="utf-8")
		got = existing_outputs([(str(tmp_path / "A.txt"), "new")])
		assert got == [(str(tmp_path / "A.txt"), "old", "new")]

	def test_the_ovl_is_listed_with_nothing_to_show(self, tmp_path):
		(tmp_path / "MyProp.ovl").write_bytes(b"\x00\x01")
		got = existing_outputs([(str(tmp_path / "MyProp.ovl"), None)])
		assert got == [(str(tmp_path / "MyProp.ovl"), None, None)]

	def test_an_unreadable_text_output_still_gets_asked_about(self, tmp_path):
		# the caller must still get to decide about it; it just has no diff
		(tmp_path / "A.txt").write_bytes(b"\xff\xfe not utf-8")
		got = existing_outputs([(str(tmp_path / "A.txt"), "new")])
		assert got == [(str(tmp_path / "A.txt"), None, "new")]


class TestExistingLocTexts:

	def test_only_files_that_are_already_there_are_listed(self, tmp_path):
		(tmp_path / "Kept.txt").write_text("old wording", encoding="utf-8")
		report = loc_report(("Kept", "new wording"), ("Absent", "brand new"))
		assert existing_loc_texts(report, str(tmp_path)) == [
			("Kept", "old wording", "new wording")]

	def test_identical_wording_is_still_listed(self, tmp_path):
		# the question is which files may be replaced, not which differ
		(tmp_path / "Same.txt").write_text("same", encoding="utf-8")
		assert existing_loc_texts(loc_report(("Same", "same")), str(tmp_path)) == [
			("Same", "same", "same")]

	def test_an_unreadable_loc_file_degrades_instead_of_raising(self, tmp_path):
		(tmp_path / "Sym.txt").write_bytes(b"\xff\xfe not utf-8")
		assert existing_loc_texts(loc_report(("Sym", "new")), str(tmp_path)) == [
			("Sym", None, "new")]


class TestWriteLocTexts:

	def test_the_file_is_exactly_the_label_and_nothing_else(self, tmp_path):
		written = write_loc_texts(loc_report(("Good", "Wall of Fire")),
								  str(tmp_path), log=quiet)
		assert written == [os.path.join(str(tmp_path), "Good.txt")]
		with open(written[0], "rb") as fh:
			# no BOM, no trailing newline: the whole content is what the player sees
			assert fh.read() == b"Wall of Fire"

	def test_a_newline_in_a_label_is_not_translated(self, tmp_path):
		# newline="" on the open, so Windows does not turn one label into two
		write_loc_texts(loc_report(("Good", "two\nlines")), str(tmp_path), log=quiet)
		with open(os.path.join(str(tmp_path), "Good.txt"), "rb") as fh:
			assert fh.read() == b"two\nlines"

	def test_the_destination_is_created(self, tmp_path):
		dest = str(tmp_path / "deep" / "out")
		write_loc_texts(loc_report(("Good", "x")), dest, log=quiet)
		assert os.path.isfile(os.path.join(dest, "Good.txt"))

	def test_an_existing_file_is_rewritten(self, tmp_path):
		(tmp_path / "Good.txt").write_text("stale", encoding="utf-8")
		write_loc_texts(loc_report(("Good", "fresh")), str(tmp_path), log=quiet)
		assert (tmp_path / "Good.txt").read_text(encoding="utf-8") == "fresh"

	def test_a_skipped_symbol_is_left_exactly_as_it_was(self, tmp_path):
		(tmp_path / "Kept.txt").write_text("translated", encoding="utf-8")
		logged = []
		written = write_loc_texts(loc_report(("Kept", "retyped"), ("Fresh", "new")),
								  str(tmp_path), skip=["Kept"], log=logged.append)
		assert written == [os.path.join(str(tmp_path), "Fresh.txt")]
		assert (tmp_path / "Kept.txt").read_text(encoding="utf-8") == "translated"
		assert "kept 1 as they were" in logged[0]

	def test_nothing_kept_says_nothing_about_keeping(self, tmp_path):
		logged = []
		write_loc_texts(loc_report(("Good", "x")), str(tmp_path), log=logged.append)
		assert logged == [f"wrote 1 loc text files to {tmp_path}"]

	def test_a_traversal_symbol_is_refused(self, tmp_path):
		# a symbol becomes a FILENAME here; this is the security boundary, and
		# it is re-checked rather than trusting a report from anywhere
		dest = tmp_path / "out"
		with pytest.raises(PropBuildError, match="refusing to use it"):
			write_loc_texts(loc_report(("../evil", "pwned")), str(dest), log=quiet)
		assert not os.path.exists(str(tmp_path / "evil.txt"))

	@pytest.mark.parametrize("symbol", ["a b", "a.b", "a/b", "a\\b", "", "a-b", "ä"])
	def test_anything_but_a_bare_name_is_refused(self, tmp_path, symbol):
		with pytest.raises(PropBuildError, match="not a bare name"):
			write_loc_texts(loc_report((symbol, "x")), str(tmp_path), log=quiet)

	def test_a_refused_symbol_writes_nothing_at_all(self, tmp_path):
		report = loc_report(("Good", "kept"), ("../evil", "pwned"))
		with pytest.raises(PropBuildError):
			write_loc_texts(report, str(tmp_path), log=quiet)
		assert not os.path.exists(os.path.join(str(tmp_path), "Good.txt"))


class TestLocTextsAreCrossPlatform:
	"""The same spec must produce the same files on Windows, macOS and Linux.

	The write side already did: utf-8 is pinned rather than following the
	locale, newline="" suppresses CRLF translation, no BOM is emitted, and
	_SAFE_LOC_SYMBOL excludes path separators and every Windows reserved
	character. What follows covers the parts that did not.
	"""

	def test_symbols_differing_only_in_case_are_refused(self, tmp_path):
		# one file on Windows and macOS, two on Linux, and on the former the
		# second label silently destroys the first
		report = loc_report(("Sym_Static", "first"), ("sym_static", "second"))
		with pytest.raises(PropBuildError, match="differ only in case"):
			write_loc_texts(report, str(tmp_path), log=quiet)

	def test_nothing_is_written_when_a_case_clash_is_refused(self, tmp_path):
		report = loc_report(("Sym_Static", "first"), ("sym_static", "second"))
		with pytest.raises(PropBuildError):
			write_loc_texts(report, str(tmp_path), log=quiet)
		assert os.listdir(str(tmp_path)) == []

	def test_the_same_symbol_twice_is_not_a_case_clash(self, tmp_path):
		# an exact repeat is the caller's business, not a platform hazard
		report = loc_report(("Same", "a"), ("Same", "b"))
		written = write_loc_texts(report, str(tmp_path), log=quiet)
		assert len(written) == 2

	def test_output_bytes_do_not_depend_on_the_platform(self, tmp_path):
		# a label carrying a newline and non-ASCII: utf-8 with no BOM, and \n
		# left alone rather than turned into \r\n on Windows
		write_loc_texts(loc_report(("Sym", "Fahrgeschäft\nZwei")),
						str(tmp_path), log=quiet)
		raw = (tmp_path / "Sym.txt").read_bytes()
		assert raw == "Fahrgeschäft\nZwei".encode("utf-8")
		assert not raw.startswith(b"\xef\xbb\xbf")

	def test_a_bom_left_by_an_editor_does_not_look_like_a_change(self, tmp_path):
		# Notepad writes a BOM by default; reading it as plain utf-8 would
		# report the file as differing from a label it actually matches
		(tmp_path / "Sym.txt").write_bytes(b"\xef\xbb\xbfRest")
		assert existing_loc_texts(loc_report(("Sym", "Rest")), str(tmp_path)) == [
			("Sym", "Rest", "Rest")]

	def test_crlf_is_reported_as_it_sits_on_disk(self, tmp_path):
		# the read must not fold \r\n to \n: the write would not produce those
		# bytes, so folding reports "unchanged" for a file that would change
		(tmp_path / "Sym.txt").write_bytes(b"one\r\ntwo")
		got = existing_loc_texts(loc_report(("Sym", "one\ntwo")), str(tmp_path))
		assert got == [("Sym", "one\r\ntwo", "one\ntwo")]


class TestBuildFromAnimspec:
	"""The glue two callers share - the CLI subcommand and the GUI dialog - so
	one caller's error handling cannot drift from the other's."""

	@pytest.fixture
	def calls(self, monkeypatch):
		recorded = []

		def recorder(**kwargs):
			recorded.append(kwargs)
			return {"out_ovl": "x.ovl"}

		monkeypatch.setattr(prop_builder, "build", recorder)
		return recorded

	def test_a_missing_spec_names_the_export_that_makes_one(self, tmp_path):
		art = art_dir_with(tmp_path)
		with pytest.raises(PropBuildError) as e:
			build_from_animspec(str(tmp_path), "MyProp", art, str(tmp_path))
		assert "no animspec at" in str(e.value)
		assert "Cobra Animation Spec" in str(e.value)

	def test_an_explicit_blend_time_wins_over_the_spec(self, tmp_path, calls):
		art = art_dir_with(tmp_path)
		write_spec(animspec_path(art, "MyProp"), blend_time=0.3)
		build_from_animspec(str(tmp_path), "MyProp", art, str(tmp_path),
							blend_time=0.5)
		assert calls[0]["blend_time"] == 0.5

	def test_the_spec_supplies_it_when_the_caller_does_not(self, tmp_path, calls):
		art = art_dir_with(tmp_path)
		write_spec(animspec_path(art, "MyProp"), blend_time=0.3)
		build_from_animspec(str(tmp_path), "MyProp", art, str(tmp_path))
		assert calls[0]["blend_time"] == 0.3

	def test_neither_falls_back_to_the_measured_default(self, tmp_path, calls):
		art = art_dir_with(tmp_path)
		write_spec(animspec_path(art, "MyProp"))
		build_from_animspec(str(tmp_path), "MyProp", art, str(tmp_path))
		assert calls[0]["blend_time"] == 0.15

	def test_the_read_rows_reach_build_as_choices(self, tmp_path, calls):
		art = art_dir_with(tmp_path)
		write_spec(animspec_path(art, "MyProp"))
		build_from_animspec(str(tmp_path), "MyProp", art, str(tmp_path),
							vars_ref="Shared", skip=("x",))
		assert calls[0]["choices"][0] == ("Default", None, None, "Auto", ())
		assert calls[0]["vars_ref"] == "Shared"
		assert calls[0]["skip"] == ("x",)

	def test_a_user_input_refusal_is_not_a_traceback(self, tmp_path, monkeypatch):
		art = art_dir_with(tmp_path)
		write_spec(animspec_path(art, "MyProp"))

		def refuse(**kwargs):
			raise ValueError("choice 2 has no clip")

		monkeypatch.setattr(prop_builder, "build", refuse)
		with pytest.raises(PropBuildError, match="choice 2 has no clip"):
			build_from_animspec(str(tmp_path), "MyProp", art, str(tmp_path))


class TestBuildValidatesItsArt:
	"""The prologue, before anything is packed: it is the only place the art
	folder is judged, and each refusal names what to do about it."""

	def test_an_empty_art_folder_is_refused(self, tmp_path):
		art_dir_with(tmp_path)
		with pytest.raises(PropBuildError, match="no art"):
			build(**build_args(tmp_path))

	def test_a_tex_with_no_source_png_is_refused(self, tmp_path):
		# a .tex is CREATED from its PNGs; add_files would drop it with a log
		# and the mesh would ship with dangling material references
		art_dir_with(tmp_path, "myprop.ms2", "myprop.pbasecolourtexture.tex")
		with pytest.raises(PropBuildError) as e:
			build(**build_args(tmp_path))
		assert "no source .png next to it" in str(e.value)
		assert "myprop.pbasecolourtexture.tex" in str(e.value)

	def test_a_tex_with_its_png_passes(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2", "myprop.pbasecolourtexture.tex",
					 "myprop.pbasecolourtexture_[0].png")
		build(**build_args(tmp_path))
		assert seam.calls

	def test_art_named_for_another_asset_is_refused(self, tmp_path):
		art_dir_with(tmp_path, "somethingelse.ms2")
		with pytest.raises(PropBuildError) as e:
			build(**build_args(tmp_path))
		assert "named for a different asset" in str(e.value)
		assert "somethingelse.ms2" in str(e.value)

	def test_several_vars_files_have_no_answer(self, tmp_path):
		art_dir_with(tmp_path, "myprop.ms2", "a.motiongraphvars", "b.motiongraphvars")
		with pytest.raises(PropBuildError, match="several .motiongraphvars"):
			build(**build_args(tmp_path))

	def test_a_supplied_vars_file_names_the_reference(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2", "Shared.motiongraphvars")
		build(**build_args(tmp_path))
		assert seam.calls[0]["vars_ref"] == "Shared"

	def test_no_vars_file_falls_back_to_the_retail_convention(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		build(**build_args(tmp_path))
		assert seam.calls[0]["vars_ref"] == "MyPropVars"

	def test_an_explicit_vars_ref_skips_the_whole_derivation(self, tmp_path, seam):
		# two files would otherwise be unanswerable; naming one settles it
		art_dir_with(tmp_path, "myprop.ms2", "a.motiongraphvars", "b.motiongraphvars")
		build(**build_args(tmp_path, vars_ref="Chosen"))
		assert seam.calls[0]["vars_ref"] == "Chosen"

	def test_every_art_extension_is_collected(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2", "myprop.manis", "myprop_mat.fgm",
					 "notart.txt")
		build(**build_args(tmp_path))
		assert seam.ovl.added == [["myprop.manis", "myprop.ms2", "myprop_mat.fgm"]]

	def test_art_in_a_bracketed_folder_is_found(self, tmp_path, seam):
		art = tmp_path / "art [v2]"
		art.mkdir()
		(art / "myprop.ms2").write_bytes(b"")
		build(**build_args(tmp_path, art_dir=str(art)))
		assert seam.calls

	def test_a_clip_outside_the_prefix_is_a_propbuilderror(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		seam.ovl.loaders["donor$idle.mani"] = StubLoader("donor$idle.mani")
		with pytest.raises(PropBuildError):
			build(**build_args(tmp_path))


class TestBuildWritesOnlyItsOwnOutputs:
	"""The tail used to rmtree dest_dir, which deleted whatever else the user
	had picked. Now every output is written on the same terms and `skip` is
	honoured for all of them."""

	def test_the_ovl_and_its_loc_texts_land_together(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		out = str(tmp_path / "out")
		report = build(**build_args(tmp_path))
		assert seam.ovl.saved == [os.path.join(out, "MyProp.ovl")]
		assert report["out_ovl"] == os.path.join(out, "MyProp.ovl")
		assert os.path.isfile(os.path.join(
			out, "InfoPanel_AnimationType_MyProp_Default.txt"))

	def test_a_skipped_ovl_is_not_overwritten(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		out = tmp_path / "out"
		out.mkdir()
		(out / "MyProp.ovl").write_bytes(b"KEEP")
		logged = []
		build(**build_args(tmp_path, skip=[str(out / "MyProp.ovl")], log=logged.append))
		assert seam.ovl.saved == []
		assert (out / "MyProp.ovl").read_bytes() == b"KEEP"
		assert any("kept existing" in m for m in logged)

	def test_a_skipped_loc_text_is_not_overwritten(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		out = tmp_path / "out"
		out.mkdir()
		kept = out / "InfoPanel_AnimationType_MyProp_Default.txt"
		kept.write_text("translated", encoding="utf-8")
		build(**build_args(tmp_path, skip=[str(kept)]))
		assert kept.read_text(encoding="utf-8") == "translated"
		# the other one is still written; skip is per file
		assert os.path.isfile(os.path.join(
			str(out), "InfoPanel_AnimationType_MyProp_Static.txt"))

	def test_a_failed_build_leaves_nothing_behind(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		seam.error = PropBuildError("graph references clips the manis does not provide")
		with pytest.raises(PropBuildError):
			build(**build_args(tmp_path))
		assert os.listdir(str(tmp_path / "out")) == []

	def test_the_generation_step_gets_the_arguments_it_was_given(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		build(**build_args(tmp_path, blend_time=0.25))
		call = seam.calls[0]
		assert call["asset"] == "MyProp"
		assert call["blend_time"] == 0.25
		assert call["src_hint"] == str(tmp_path / "art")
		assert call["choices"] is CHOICES

	@pytest.mark.skipif(os.path.normcase("A") != "a",
						reason="only a case-insensitive filesystem can express this")
	def test_skip_matches_the_same_file_under_another_casing(self, tmp_path, seam):
		art_dir_with(tmp_path, "myprop.ms2")
		out = tmp_path / "out"
		out.mkdir()
		(out / "MyProp.ovl").write_bytes(b"KEEP")
		build(**build_args(tmp_path, skip=[str(out / "MYPROP.OVL")]))
		assert (out / "MyProp.ovl").read_bytes() == b"KEEP"


class TestApplyAnimspecTo:
	"""The in-memory route the GUI needs: the generated entries land in the
	open container and saving stays the caller's call."""

	@pytest.fixture
	def spec(self, tmp_path):
		return write_spec(str(tmp_path / "myprop.animspec.json"))

	@pytest.fixture
	def gen(self, monkeypatch):
		calls = []

		def recorder(ovl, asset, choices, vars_ref, blend_time, game,
					 src_hint, out_ovl, log=print):
			calls.append({"asset": asset, "vars_ref": vars_ref,
						  "blend_time": blend_time, "src_hint": src_hint,
						  "out_ovl": out_ovl})
			return {"out_ovl": out_ovl}

		monkeypatch.setattr(prop_builder, "_generate_into", recorder)
		return calls

	def test_an_explicit_asset_name_wins(self, spec, gen):
		apply_animspec_to(FakeOvl(filepath="x.ovl"), spec, asset="Chosen", log=quiet)
		assert gen[0]["asset"] == "Chosen"

	def test_the_ovl_path_names_the_asset(self, spec, gen):
		apply_animspec_to(FakeOvl(), spec, ovl_path=os.path.join("d", "MyProp.ovl"),
						  log=quiet)
		assert gen[0]["asset"] == "MyProp"
		assert gen[0]["src_hint"] == "MyProp.ovl"

	def test_an_already_open_file_names_itself(self, spec, gen):
		apply_animspec_to(FakeOvl(filepath=os.path.join("d", "Opened.ovl")), spec,
						  log=quiet)
		assert gen[0]["asset"] == "Opened"

	def test_an_unnamed_container_cannot_be_guessed_at(self, tmp_path, gen):
		with pytest.raises(PropBuildError, match="cannot tell what this asset is called"):
			apply_animspec_to(FakeOvl(), str(tmp_path / "nope.json"), log=quiet)

	def test_stale_generated_entries_are_replaced_not_duplicated(self, spec, gen):
		# re-export-and-reapply is the iteration loop; without this it is a
		# duplicate-entry error
		ovl = FakeOvl(["myprop.motiongraph", "loopanimselection.enumnamer",
					   "myprop.sceneryanimchoices", "myprop.ms2"])
		logged = []
		apply_animspec_to(ovl, spec, asset="MyProp", log=logged.append)
		assert ovl.removed == [["loopanimselection.enumnamer",
								"myprop.motiongraph", "myprop.sceneryanimchoices"]]
		assert "replacing existing generated entries" in logged[0]

	def test_nothing_stale_is_not_announced(self, spec, gen):
		ovl = FakeOvl(["myprop.ms2"])
		logged = []
		apply_animspec_to(ovl, spec, asset="MyProp", log=logged.append)
		assert ovl.removed == []
		assert not any("replacing" in m for m in logged)

	def test_a_second_independent_object_in_the_container_is_not_touched(self, spec, gen):
		# AQ_Doors: Hatch 1 and Hatch 2 are two separately placeable retail
		# objects sharing one OVL, verified in game - applying to one must
		# never delete or duplicate-detect against the other's entries
		ovl = FakeOvl(["aq_doors02.motiongraph", "aq_door02state.enumnamer"])
		with pytest.raises(PropBuildError, match="aq_door02state.enumnamer"):
			apply_animspec_to(ovl, spec, asset="aq_doors01", log=quiet)
		assert ovl.removed == []

	def test_a_synchronised_sibling_part_is_not_touched(self, spec, gen):
		# MY_WaterWheel: the wheel and its post are ONE object, verified in
		# game as unable to be separated or animated apart - regenerating the
		# wheel's graph must not silently orphan the post's
		ovl = FakeOvl(["my_waterwheel_large_post_01.motiongraph",
					   "my_waterwheel_large_post_01.sceneryanimchoices"])
		with pytest.raises(PropBuildError,
						   match="my_waterwheel_large_post_01.motiongraph"):
			apply_animspec_to(ovl, spec, asset="my_waterwheel_large_01", log=quiet)
		assert ovl.removed == []

	def test_the_error_names_what_it_will_not_touch(self, spec, gen):
		ovl = FakeOvl(["other.motiongraph"])
		with pytest.raises(PropBuildError, match=r"\['other\.motiongraph'\]"):
			apply_animspec_to(ovl, spec, asset="MyProp", log=quiet)

	def test_only_the_exact_names_this_call_owns_are_replaced(self, spec, gen):
		# same asset, re-applied - the one case that must still succeed
		ovl = FakeOvl(["myprop.motiongraph", "loopanimselection.enumnamer",
					   "myprop.sceneryanimchoices"])
		apply_animspec_to(ovl, spec, asset="MyProp", log=quiet)
		assert ovl.removed == [["loopanimselection.enumnamer",
								"myprop.motiongraph", "myprop.sceneryanimchoices"]]

	def test_bare_clips_are_qualified_before_the_graph_is_generated(self, spec, gen):
		ovl = FakeOvl(["idle.mani"])
		logged = []
		apply_animspec_to(ovl, spec, asset="MyProp", log=logged.append)
		assert "MyProp$idle.mani" in ovl.loaders
		assert "idle -> MyProp$idle" in logged[0]

	def test_nothing_to_qualify_says_so(self, spec, gen):
		logged = []
		apply_animspec_to(FakeOvl(), spec, asset="MyProp", log=logged.append)
		assert "(none needed)" in logged[0]

	def test_the_vars_reference_is_read_off_the_container(self, spec, gen):
		apply_animspec_to(FakeOvl(["shared.motiongraphvars"]), spec, asset="MyProp",
						  log=quiet)
		assert gen[0]["vars_ref"] == "shared"

	def test_blend_time_precedence(self, tmp_path, gen):
		spec = write_spec(str(tmp_path / "s.json"), blend_time=0.3)
		apply_animspec_to(FakeOvl(), spec, asset="A", blend_time=0.5, log=quiet)
		apply_animspec_to(FakeOvl(), spec, asset="A", log=quiet)
		apply_animspec_to(FakeOvl(), write_spec(str(tmp_path / "t.json")),
						  asset="A", log=quiet)
		assert [c["blend_time"] for c in gen] == [0.5, 0.3, 0.15]

	def test_a_generator_refusal_is_not_a_traceback(self, spec, monkeypatch):
		def refuse(*args, **kwargs):
			raise ValueError("choice 0 must be the Auto/random choice")

		monkeypatch.setattr(prop_builder, "_generate_into", refuse)
		with pytest.raises(PropBuildError, match="Auto/random"):
			apply_animspec_to(FakeOvl(), spec, asset="MyProp", log=quiet)

	def test_a_clip_outside_the_prefix_is_a_propbuilderror(self, spec, gen):
		with pytest.raises(PropBuildError):
			apply_animspec_to(FakeOvl(["donor$idle.mani"]), spec, asset="MyProp",
							  log=quiet)

	def test_a_clip_collision_is_a_propbuilderror_too(self, spec, gen):
		# apply_clip_prefix itself raises ValueError on a rename collision,
		# a different site from check_prefix_consistent above - both need the
		# same translation to this function's PropBuildError contract
		ovl = FakeOvl(["idle.mani", "MyProp$idle.mani"])
		with pytest.raises(PropBuildError, match="collide"):
			apply_animspec_to(ovl, spec, asset="MyProp", log=quiet)


class TestApplyAnimspec:
	"""The on-disk wrapper. Its one job beyond apply_animspec_to is that a
	failure must not touch the file."""

	@pytest.fixture
	def spec(self, tmp_path):
		return write_spec(str(tmp_path / "myprop.animspec.json"))

	def test_a_missing_ovl_is_diagnosed(self, tmp_path, spec):
		with pytest.raises(PropBuildError, match="no such ovl"):
			apply_animspec(str(tmp_path), str(tmp_path / "nope.ovl"), spec)

	def test_the_container_is_saved_back_over_itself(self, tmp_path, spec, monkeypatch):
		ovl_path = tmp_path / "MyProp.ovl"
		ovl_path.write_bytes(b"ORIGINAL")
		fake = FakeOvl()
		monkeypatch.setattr(prop_builder, "_fresh", lambda cfg, game, path=None: fake)
		monkeypatch.setattr(prop_builder, "_generate_into",
							lambda *a, **kw: {"out_ovl": kw["out_ovl"]})
		report = apply_animspec(str(tmp_path), str(ovl_path), spec, log=quiet)
		assert fake.saved == [str(ovl_path)]
		assert report["out_ovl"] == str(ovl_path)

	def test_a_failed_apply_leaves_the_file_byte_identical(self, tmp_path, spec,
														   monkeypatch):
		ovl_path = tmp_path / "MyProp.ovl"
		ovl_path.write_bytes(b"ORIGINAL")
		fake = FakeOvl()
		monkeypatch.setattr(prop_builder, "_fresh", lambda cfg, game, path=None: fake)

		def refuse(*args, **kwargs):
			raise ValueError("graph references clips the manis does not provide")

		monkeypatch.setattr(prop_builder, "_generate_into", refuse)
		with pytest.raises(PropBuildError):
			apply_animspec(str(tmp_path), str(ovl_path), spec, log=quiet)
		assert ovl_path.read_bytes() == b"ORIGINAL"
		assert fake.saved == []


class TestGenerateInto:
	"""Steps 2-5 against a real OvlFile: the graph and enumnamer are generated,
	created and read back for real, and only the art that cannot be generated
	is stubbed."""

	def test_the_report_names_everything_the_caller_needs(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS),
				   Ms2Stub("myprop.ms2", b"MyProp_Mat\x00MyProp.ms2\x00"),
				   StubLoader("myprop_mat.fgm"))
		report = generate_into(o, out_ovl="MyProp.ovl")
		assert set(report) == {"out_ovl", "graph_entry", "clip_refs", "labels",
							   "loc_symbols", "fgm", "vfx_children",
							   "audio_locations", "vfx_particles"}
		assert report["out_ovl"] == "MyProp.ovl"
		assert report["graph_entry"] == "myprop.motiongraph"
		assert report["clip_refs"] == ["MyProp$ClipA", "MyProp$ClipB"]
		assert report["labels"] == [("Default", "Auto"), ("Static", "Rest"),
									("Play", "Playing")]
		assert report["loc_symbols"] == ["InfoPanel_AnimationType_MyProp_Default",
										 "InfoPanel_AnimationType_MyProp_Static",
										 "InfoPanel_AnimationType_MyProp_Play"]
		assert report["fgm"] == ["myprop_mat"]
		assert report["vfx_children"] == []
		assert report["audio_locations"] == []
		assert report["vfx_particles"] == {}

	def test_the_generated_entries_are_really_in_the_container(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS))
		generate_into(o)
		assert "myprop.motiongraph" in o.loaders
		assert "loopanimselection.enumnamer" in o.loaders
		assert "myprop.sceneryanimchoices" in o.loaders

	def test_a_vars_entry_is_generated_when_the_art_supplies_none(self, fake_manis):
		logged = []
		o = packed(manis_stub("myprop.manis", CLIPS))
		prop_builder._generate_into(o, "MyProp", CHOICES, "MyPropVars", 0.1,
									"Planet Coaster 2", src_hint="art",
									out_ovl="o.ovl", log=logged.append)
		assert "mypropvars.motiongraphvars" in o.loaders
		assert "generated vars entry: mypropvars.motiongraphvars" in logged

	def test_a_packed_vars_entry_is_left_alone(self, fake_manis):
		# its contents are fixed for this family, but a shared set under
		# another name is the user's, so it is never regenerated over
		logged = []
		o = packed(manis_stub("myprop.manis", CLIPS),
				   StubLoader("shared.motiongraphvars"))
		prop_builder._generate_into(o, "MyProp", CHOICES, "Shared", 0.1,
									"Planet Coaster 2", src_hint="art",
									out_ovl="o.ovl", log=logged.append)
		assert not any("generated vars entry" in m for m in logged)

	def test_choice_durations_are_the_clip_plus_the_blend_round_trip(self, fake_manis):
		# a sequencer block covers blend + clip + blend, and only the clip fits
		# in a bare-duration block
		o = packed(manis_stub("myprop.manis", CLIPS))
		generate_into(o, blend_time=0.25)
		body = self.choices_body(o)
		assert 'index="1" duration="1.5"' in body
		assert 'index="2" duration="2.5"' in body

	def test_the_auto_choice_follows_the_longest_clip(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS))
		generate_into(o, blend_time=0.0)
		assert 'index="0" duration="2.0"' in self.choices_body(o)

	def test_choice_rows_may_omit_their_events(self, fake_manis):
		# a row carries an optional 5th member; a fixed-width unpack would break
		o = packed(manis_stub("myprop.manis", CLIPS))
		report = generate_into(o, choices=[c[:4] for c in CHOICES])
		assert report["vfx_children"] == []
		assert report["vfx_particles"] == {}

	def test_armed_events_become_the_prefab_contract(self, fake_manis):
		events = parse_events([
			{"name": "VFX_Fire", "type": "VFXEnable", "at": 0.2,
			 "particle": "sce_vi_bellows_blow"},
			{"name": "VFX_Fire", "type": "VFXDisable", "at": 0.9},
			{"name": "Snap", "type": "AudioEvent", "at": 0.5,
			 "location": "Base_Default"},
		], "ClipA")
		choices = [CHOICES[0], (CHOICES[1][:4] + (tuple(events),)), CHOICES[2]]
		o = packed(manis_stub("myprop.manis", CLIPS))
		report = generate_into(o, choices=choices)
		assert report["vfx_children"] == ["VFX_Fire"]
		assert report["audio_locations"] == ["Base_Default"]
		# an enable/disable pair only needs the particle on one of them
		assert report["vfx_particles"] == {"VFX_Fire": "sce_vi_bellows_blow"}

	def test_a_clip_the_manis_does_not_provide_is_refused(self, fake_manis):
		o = packed(manis_stub("myprop.manis", {"MyProp$ClipA": 1.0}))
		with pytest.raises(PropBuildError) as e:
			generate_into(o)
		assert "the manis does not provide" in str(e.value)
		assert "MyProp$ClipB" in str(e.value)

	def test_clip_matching_ignores_case(self, fake_manis):
		# the graph keeps authored casing, the manis lowercases
		o = packed(manis_stub("myprop.manis", {"myprop$clipa": 1.0,
											   "myprop$clipb": 2.0}))
		assert generate_into(o)["clip_refs"] == ["MyProp$ClipA", "MyProp$ClipB"]

	def test_a_vars_ref_pointing_at_nothing_is_refused(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS),
				   StubLoader("othervars.motiongraphvars"))
		with pytest.raises(PropBuildError) as e:
			generate_into(o, vars_ref="MyPropVars")
		assert "would point at nothing" in str(e.value)

	def test_an_unbound_material_is_refused(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS),
				   Ms2Stub("myprop.ms2", b"MyProp_Mat\x00"))
		with pytest.raises(PropBuildError) as e:
			generate_into(o)
		assert "render unlit white" in str(e.value)
		assert "myprop_mat" in str(e.value)

	def test_scene_nodes_are_not_mistaken_for_materials(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS),
				   Ms2Stub("myprop.ms2", b"MyProp_hitcheck\x00MyProp_joint\x00"
										 b"MyProp.mdl2\x00other_mat\x00"))
		assert generate_into(o)["fgm"] == []

	def test_generator_drift_in_the_clip_references_is_caught(self, fake_manis,
															  monkeypatch):
		# the guard exists for drift between generate() and the choice list,
		# which nothing else can produce - so the graph text is substituted at
		# the point the check reads it back
		monkeypatch.setattr(prop_builder, "_extract_one",
							self.drifted(".motiongraph", "<mani>MyProp$Ghost</mani>"))
		with pytest.raises(PropBuildError, match="expected exactly"):
			generate_into(packed(manis_stub("myprop.manis", CLIPS)))

	def test_a_choice_table_that_does_not_read_back_is_caught(self, fake_manis,
															  monkeypatch):
		monkeypatch.setattr(prop_builder, "_extract_one",
							self.drifted(".sceneryanimchoices", "<label>[One]</label>"))
		with pytest.raises(PropBuildError, match="read back 1"):
			generate_into(packed(manis_stub("myprop.manis", CLIPS)))

	def test_a_container_with_no_manis_is_diagnosed(self, fake_manis):
		with pytest.raises(PropBuildError):
			generate_into(packed())

	def test_durations_are_read_from_every_manis(self, fake_manis):
		o = packed(manis_stub("a.manis", {"MyProp$ClipA": 1.0}),
				   manis_stub("b.manis", {"MyProp$ClipB": 2.0}))
		assert generate_into(o)["clip_refs"] == ["MyProp$ClipA", "MyProp$ClipB"]

	def test_a_missing_blend_time_is_refused(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS))
		with pytest.raises(PropBuildError, match="blend_time"):
			generate_into(o, blend_time=None)

	def test_a_negative_blend_time_is_refused(self, fake_manis):
		o = packed(manis_stub("myprop.manis", CLIPS))
		with pytest.raises(PropBuildError, match="blend_time"):
			generate_into(o, blend_time=-0.5)

	# --- helpers ---------------------------------------------------------
	@staticmethod
	def choices_body(ovl):
		work = tempfile.mkdtemp()
		path = prop_builder._extract_one(ovl, "myprop.sceneryanimchoices", work)
		with open(path, encoding="utf-8") as fh:
			return fh.read()

	@staticmethod
	def drifted(ext, text):
		real = prop_builder._extract_one

		def substitute(o, entry, work):
			path = real(o, entry, work)
			if entry.endswith(ext):
				with open(path, "w", encoding="utf-8") as fh:
					fh.write(text)
			return path

		return substitute
