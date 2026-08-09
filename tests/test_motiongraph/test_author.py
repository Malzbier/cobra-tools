"""motiongraph_author writes three files that index each other by POSITION, and
every way it can get that wrong is silent in game: a dropdown entry that plays
the donor's clip, a name list one shorter than the choice list, a cloned State
whose refs point at an id nobody defines. None of it raises at build time and
none of it is visible in the resulting OVL, so the contract is pinned here or it
is not pinned at all.

Entries are also APPEND-ONLY, because placed props store the choice INDEX. A
defect that reaches a shipped asset therefore cannot be undone by a later fix,
which is why the refusal table is tested as hard as the happy path.

Everything below runs on ElementTree literals plus a synthetic OvlFile - no game
install, no retail asset, no Blender.

Tests marked xfail(strict) assert the behaviour the module SHOULD have. They are
the recorded defects; none of them describes what the code does today.
"""
import json
import os

import pytest

import xml.etree.ElementTree as ET

from generated.formats.ovl import OvlFile
from modules.motiongraph_author import (
	FLAGS_LOOPING, FLAGS_ONE_SHOT, LOOP_ANIM_SELECTION, ClipSpec,
	_append_lua_result, _check_post_conditions, _check_specs_qualified,
	_count_lua_result_slots, _next_free_id, _repoint_state_outputs, append_clips,
	append_datastreams, apply_clip_prefix, check_prefix_consistent, choice_duration,
	duration_from_frames, durations_from_manis, find_enum_node, find_enum_nodes,
	freshen, load_spec, loc_symbol, parse_events, parse_spec, replace_clips,
	resolve_enum_holder, resolve_prefix, short_clip, strip_datastreams)


# --- fixtures: the smallest trees each function actually reads -----------------

def lua_text(n_slots, varname_first=False):
	"""A lua_results body whose LoopAnimSelection entry has `n_slots` slots.

	The first entry exists to give the rfind in _append_lua_result something
	wrong to land on; `varname_first` flips the LoopAnimSelection entry into the
	other field order retail also uses.
	"""
	slots = "".join(f" [{i + 1}] = {{  }}," for i in range(n_slots))
	rp = f"ResultParams = {{ {slots} }}"
	marker = f'VariableName = "{LOOP_ANIM_SELECTION}"'
	entry = f"{marker}, {rp}," if varname_first else f"{rp}, {marker},"
	return ("MotionGraphResults = {\n"
			' [1] = { ResultParams = {  [1] = {  }, }, VariableName = "OtherVar", },\n'
			f" [2] = {{ {entry} }},\n"
			"}\n")


def _state_xml(state_id, mani=True, sync=True, streams=True, inherited=0):
	inner = ['<data animation_flags="17">']
	if sync:
		inner.append(f'<sync_prop_through_variable id="{state_id + 1}" />')
	if streams:
		inner.append("<additional_data_streams>"
					 + "<data_stream_resource_data />" * inherited
					 + "</additional_data_streams>")
	if mani:
		inner.append("<mani>Donor$Idle</mani>")
	inner.append("</data>")
	# the state carries a ref to its OWN id - freshen has to keep that resolvable
	return (f'<statereference><state id="{state_id}">'
			f'<state_link ref="{state_id}" />'
			f'<activities><activityreference><activity>{"".join(inner)}'
			f"</activity></activityreference></activities></state></statereference>")


def _node_xml(node_id, n_branches, state_id, children=True, bound=True,
			  method="MotionGraph.VariableResultEnum"):
	branches = "".join(
		f'<mrfchild count_0="{i + 1}"><mrfmember1 id="{node_id + 1 + i}">'
		f"<lua_method>MotionGraph.StateOutput</lua_method>"
		f'<ptr_0 raw="00" ref="{state_id}" /><motiongraph_vars ref="{state_id}" />'
		f"</mrfmember1></mrfchild>" for i in range(n_branches))
	kids = f"<children>{branches}</children>" if children else ""
	target = (f"<motiongraph_vars_binding><target_name>{LOOP_ANIM_SELECTION}"
			  f"</target_name></motiongraph_vars_binding>") if bound else ""
	return (f'<mrfmember1 id="{node_id}"><lua_method>{method}</lua_method>'
			f"{target}{kids}</mrfmember1>")


def graph(n_nodes=2, n_branches=2, n_states=1, mani=True, sync=True, streams=True,
		  children=True, with_soe=True, with_lua=True, lua=None, inherited=0):
	"""A motiongraph with everything append_clips reads and nothing else."""
	states = "".join(_state_xml(100 + i * 10, mani, sync, streams, inherited)
					 for i in range(n_states))
	soe = (f"<state_output_entries><states>{states}</states></state_output_entries>"
		   if with_soe else "")
	nodes = "".join(_node_xml(20 + i * 10, n_branches, 100, children)
					for i in range(n_nodes))
	body = lua_text(n_branches) if lua is None else lua
	lua_el = f"<lua_results>{body}</lua_results>" if with_lua else ""
	return ET.fromstring(f"<motiongraph>{soe}<nodes>{nodes}</nodes>{lua_el}"
						 f"</motiongraph>")


def enum(n=2, tag="strings"):
	names = "".join(f"<pointer>Name{i}</pointer>" for i in range(n))
	return ET.fromstring(f"<enumnamer><{tag}>{names}</{tag}></enumnamer>")


def choices(n=2):
	rows = "".join(f'<sceneryanimchoice index="{i}" duration="1.0">'
				   f'<label id="{50 + i}">[Sym{i}]</label></sceneryanimchoice>'
				   for i in range(n))
	return ET.fromstring(f'<sceneryanimchoices count="{n}"><entries>{rows}</entries>'
						 f"</sceneryanimchoices>")


def clip_spec(name="MyClip", enum_name=None, loop=True, duration=2.5, events=(),
			  weight=1):
	enum_name = enum_name or name
	return ClipSpec(f"Prop${name}", enum_name, loc_symbol("Prop", enum_name),
					f"Label {enum_name}", duration, loop, events=list(events),
					weight=weight)


def audio_event(name="MyEvent", **over):
	row = {"name": name, "type": "AudioEvent", "at": 0.5}
	row.update(over)
	return parse_events([row], "c")


class StubOvl:
	"""The whole OvlFile surface replace_clips and resolve_prefix touch."""

	def __init__(self, names=()):
		self.loaders = {n: object() for n in names}
		self.removed = []
		self.added = []
		self.renamed = []

	def rename(self, name_tuples):
		self.renamed.extend(name_tuples)

	def remove(self, names):
		for n in names:
			self.loaders.pop(n)
		self.removed.append(list(names))

	def add_files(self, paths, common_root_dir=None):
		self.added.append((list(paths), common_root_dir))


def real_ovl(names):
	"""A synthetic OvlFile with real loaders, for the paths that call rename."""
	ovl = OvlFile()
	ovl.game = "Planet Coaster 2"
	ovl.load_hash_table()
	ovl.loaders = {}
	for name in names:
		ext = os.path.splitext(name)[1]
		loader = ovl.init_loader(name, ext, ovl.get_mime(ext, "version"))
		loader.set_ovs("STATIC")
		ovl.loaders[name] = loader
	return ovl


# --- name handling ------------------------------------------------------------

class TestNames:

	@pytest.mark.parametrize("clip,expected", [
		("MyClip", "MyClip"), ("Old$MyClip", "MyClip"), ("A$B$C", "B$C"), ("$", ""),
	])
	def test_short_clip_discards_the_qualifier(self, clip, expected):
		assert short_clip(clip) == expected

	def test_loc_symbol_takes_the_asset_name_not_the_prefix(self):
		# the two are deliberately different: the prefix comes from lowercased
		# entry names, the symbol is a filename
		assert loc_symbol("MyProp", "MyClip") == "InfoPanel_AnimationType_MyProp_MyClip"


class TestResolvePrefix:

	def test_no_prefixed_clips_is_the_create_flow(self):
		assert resolve_prefix(StubOvl(["bend.mani", "thing.manis"])) is None

	def test_one_prefix_wins(self):
		assert resolve_prefix(StubOvl(["p$a.mani", "p$b.mani", "x.tex"])) == "p"

	def test_manis_container_is_not_a_clip(self):
		# ".manis".endswith(".mani") is False, and it must stay false
		assert resolve_prefix(StubOvl(["q$anim.manis"])) is None

	def test_several_prefixes_must_be_chosen_explicitly(self):
		with pytest.raises(ValueError, match="2 different prefixes"):
			resolve_prefix(StubOvl(["p$a.mani", "q$b.mani"]))


class TestCheckPrefixConsistent:

	def test_all_under_the_prefix_passes(self):
		check_prefix_consistent(StubOvl(["P$a.mani", "p$b.mani", "x.tex"]), "p")

	def test_a_stray_clip_fails_the_build(self):
		with pytest.raises(ValueError, match="1 clip"):
			check_prefix_consistent(StubOvl(["p$a.mani", "b.mani"]), "p")

	def test_the_message_is_truncated_past_five(self):
		names = [f"x{i}.mani" for i in range(6)]
		with pytest.raises(ValueError, match=r"\.\.\."):
			check_prefix_consistent(StubOvl(names), "p")


class TestApplyClipPrefix:

	def test_only_bare_manis_are_renamed(self):
		ovl = real_ovl(["bend.mani", "p$done.mani", "thing.manis", "x.tex"])
		assert apply_clip_prefix(ovl, "p") == [("bend.mani", "p$bend.mani")]
		assert "p$bend.mani" in ovl.loaders

	def test_the_clips_filter_is_case_insensitive(self):
		ovl = StubOvl(["bend.mani", "wave.mani"])
		assert apply_clip_prefix(ovl, "p", clips=["BEND"]) == \
			[("bend.mani", "p$bend.mani")]

	def test_nothing_to_do_is_a_no_op(self):
		ovl = StubOvl(["p$a.mani"])
		assert apply_clip_prefix(ovl, "p") == []

	def test_a_coexisting_prefixed_copy_is_diagnosed(self):
		# the .manis landed ALONGSIDE instead of replacing; ovl.rename would only
		# say "new names collide" and name no clip
		ovl = StubOvl(["bend.mani", "p$bend.mani"])
		with pytest.raises(ValueError, match="landed ALONGSIDE"):
			apply_clip_prefix(ovl, "p")

	def test_the_clash_list_is_truncated_past_four(self):
		names = [f"c{i}.mani" for i in range(5)] + [f"p$c{i}.mani" for i in range(5)]
		with pytest.raises(ValueError, match=r"\.\.\."):
			apply_clip_prefix(StubOvl(names), "p")

	def test_a_clip_that_is_a_prefix_of_another_does_not_corrupt_it(self):
		# "bend" is a prefix of "bendy", but their FULL names are not
		# substrings of each other, so this batch is safe
		ovl = real_ovl(["bend.mani", "bendy.mani"])
		apply_clip_prefix(ovl, "p")
		assert set(ovl.loaders) == {"p$bend.mani", "p$bendy.mani"}

	def test_a_one_letter_clip_that_is_a_true_substring_refuses_rather_than_corrupt(self):
		# unlike "bend"/"bendy" above, "a.mani" genuinely IS a substring of
		# "aa.mani" - no choice of old/new value renders this pair safe as one
		# batch, so it must refuse rather than silently produce a wrong name
		ovl = real_ovl(["a.mani", "aa.mani"])
		with pytest.raises(ValueError, match="unsafe as a batch"):
			apply_clip_prefix(ovl, "p")
		# refusing means nothing was touched
		assert set(ovl.loaders) == {"a.mani", "aa.mani"}

	def test_unrelated_files_are_left_alone(self):
		ovl = real_ovl(["m.mani", "x.motiongraph"])
		apply_clip_prefix(ovl, "p")
		assert "x.motiongraph" in ovl.loaders
		assert set(ovl.loaders) == {"p$m.mani", "x.motiongraph"}


class TestReplaceClips:

	def test_container_goes_first_and_takes_its_clips(self):
		ovl = StubOvl(["anim.manis", "a.mani", "b.mani", "keep.tex"])
		removed, added = replace_clips(ovl, ["/out/new.manis"])
		assert removed == ["anim.manis", "a.mani", "b.mani"]
		# two calls, container first: passing both at once KeyErrors on the children
		assert ovl.removed == [["anim.manis"], ["a.mani", "b.mani"]]
		assert added == ["/out/new.manis"]
		assert ovl.added == [(["/out/new.manis"], os.path.dirname("/out/new.manis"))]

	def test_a_bare_string_path_is_accepted(self):
		ovl = StubOvl([])
		removed, added = replace_clips(ovl, "/out/new.manis")
		assert removed == []
		assert added == ["/out/new.manis"]

	def test_clips_without_a_container_are_still_swept(self):
		ovl = StubOvl(["a.mani"])
		removed, _ = replace_clips(ovl, [])
		assert removed == ["a.mani"]
		assert ovl.added == []


# --- spec validation ----------------------------------------------------------

class TestParseEvents:

	def test_absent_is_empty(self):
		assert parse_events(None, "c") == []

	def test_not_a_list_is_diagnosed(self):
		with pytest.raises(ValueError, match="must be a list"):
			parse_events({"name": "X"}, "c")

	def test_row_must_be_an_object(self):
		with pytest.raises(ValueError, match="must be an object"):
			parse_events(["X"], "c")

	@pytest.mark.parametrize("row", [
		{"type": "AudioEvent"}, {"name": "X"}, {"name": "", "type": "AudioEvent"},
		{"name": 7, "type": "AudioEvent"}, {"name": "X", "type": " "},
	])
	def test_name_and_type_are_required_non_empty_strings(self, row):
		with pytest.raises(ValueError, match="non-empty string"):
			parse_events([row], "c")

	def test_unknown_type_refused(self):
		with pytest.raises(ValueError, match="datastream type"):
			parse_events([{"name": "X", "type": "Explode"}], "c")

	def test_hostile_event_name_refused(self):
		with pytest.raises(ValueError, match="engine resource name"):
			parse_events([{"name": "X/y", "type": "AudioEvent"}], "c")

	def test_defaults(self):
		[e] = parse_events([{"name": "MyEvent", "type": "AudioEvent"}], "c")
		assert e == {"name": "MyEvent", "type": "AudioEvent", "at": 0.1,
					 "location": "Default", "particle": None}

	@pytest.mark.parametrize("bad", [-0.1, 1.5, "half", True])
	def test_trigger_outside_the_clip_refused(self, bad):
		with pytest.raises(ValueError):
			parse_events([{"name": "X", "type": "AudioEvent", "at": bad}], "c")

	@pytest.mark.parametrize("bad", [7, "a b"])
	def test_hostile_location_refused(self, bad):
		with pytest.raises(ValueError, match="location"):
			parse_events([{"name": "X", "type": "AudioEvent", "location": bad}], "c")

	def test_vfx_places_itself_by_naming_a_child(self):
		[e] = parse_events([{"name": "VFX_Fire", "type": "VFXEnable"}], "c")
		assert e["location"] == ""

	def test_vfx_with_a_location_refused(self):
		with pytest.raises(ValueError, match="must not set"):
			parse_events([{"name": "VFX_Fire", "type": "VFXEnable",
						   "location": "Default"}], "c")

	def test_an_audio_event_places_itself_by_location(self):
		[e] = parse_events([{"name": "X", "type": "AudioEvent",
							 "location": "Base_Default"}], "c")
		assert e["location"] == "Base_Default"

	def test_particle_only_applies_to_vfx(self):
		with pytest.raises(ValueError, match="only applies to VFX"):
			parse_events([{"name": "X", "type": "AudioEvent", "particle": "p"}], "c")

	@pytest.mark.parametrize("bad", [7, "../evil"])
	def test_hostile_particle_refused(self, bad):
		with pytest.raises(ValueError, match="particleeffect resource name"):
			parse_events([{"name": "VFX_Fire", "type": "VFXEnable",
						   "particle": bad}], "c")

	def test_no_game_skips_the_catalogues(self):
		# a validator that rejects valid input for lack of data is worse than one
		# that does not check
		[e, v] = parse_events([{"name": "Not_A_Real_Event", "type": "AudioEvent"},
							   {"name": "VFX_X", "type": "VFXEnable",
								"particle": "not_a_real_particle"}], "c")
		assert e["name"] == "Not_A_Real_Event"
		assert v["particle"] == "not_a_real_particle"

	def test_unmeasured_game_skips_the_catalogues(self):
		[e, v] = parse_events([{"name": "Not_A_Real_Event", "type": "AudioEvent"},
							   {"name": "VFX_X", "type": "VFXEnable",
								"particle": "not_a_real_particle"}], "c",
							  game="Jurassic World Evolution 2")
		assert e["name"] == "Not_A_Real_Event"
		assert v["particle"] == "not_a_real_particle"

	def test_real_event_and_particle_pass(self):
		[a, v] = parse_events([
			{"name": "Sny_AnimatedSny_Medusa_Snap", "type": "AudioEvent"},
			{"name": "VFX_X", "type": "VFXEnable",
			 "particle": "sce_vi_bellows_blow"}], "c", game="Planet Coaster 2")
		assert a["at"] == 0.1 and v["particle"] == "sce_vi_bellows_blow"

	def test_fake_event_refused(self):
		with pytest.raises(ValueError, match="does not hash"):
			parse_events([{"name": "Not_A_Real_Event", "type": "AudioEvent"}], "c",
						 game="Planet Coaster 2")

	def test_fake_particle_refused(self):
		with pytest.raises(ValueError, match="not a known"):
			parse_events([{"name": "VFX_X", "type": "VFXEnable",
						   "particle": "not_a_real_particle"}], "c",
						 game="Planet Coaster 2")


def payload(**over):
	body = {
		"spec_version": 2,
		"clips": [{"clip": "ClipA", "enum_name": "Static", "label_text": "Rest",
				   "loop": True}],
	}
	body.update(over)
	return body


def one_clip(**over):
	"""A payload whose single clip row carries `over`."""
	body = payload()
	body["clips"][0].update(over)
	return body


class TestParseSpec:

	def test_normalised_row(self):
		[row] = parse_spec(payload())
		assert row == {"clip": "ClipA", "enum_name": "Static", "label_text": "Rest",
					   "loop": True, "duration": None, "weight": 1, "events": []}

	def test_version_one_is_still_read(self):
		assert parse_spec(payload(spec_version=1))

	@pytest.mark.parametrize("bad", [None, 3, "2"])
	def test_unknown_version_refused(self, bad):
		with pytest.raises(ValueError, match="unsupported spec_version"):
			parse_spec(payload(spec_version=bad))

	def test_not_an_object_refused(self):
		with pytest.raises(ValueError, match="JSON object"):
			parse_spec([])

	def test_missing_clips_refused(self):
		body = payload()
		del body["clips"]
		with pytest.raises(ValueError, match="no 'clips' key"):
			parse_spec(body)

	def test_clips_must_be_a_list(self):
		with pytest.raises(ValueError, match="must be a list"):
			parse_spec(payload(clips={}))

	def test_empty_clips_refused(self):
		# a build that silently changes nothing is worse than one that stops
		with pytest.raises(ValueError, match="nothing to append"):
			parse_spec(payload(clips=[]))

	def test_clip_row_must_be_an_object(self):
		with pytest.raises(ValueError, match="must be an object"):
			parse_spec(payload(clips=["ClipA"]))

	def test_missing_keys_are_named(self):
		with pytest.raises(ValueError, match="missing label_text, loop"):
			parse_spec(payload(clips=[{"clip": "ClipA"}]))

	@pytest.mark.parametrize("field,bad", [
		("clip", ""), ("clip", 7), ("label_text", " "), ("label_text", None),
	])
	def test_blank_strings_refused(self, field, bad):
		with pytest.raises(ValueError, match="non-empty string"):
			parse_spec(one_clip(**{field: bad}))

	@pytest.mark.parametrize("bad", ["", 7])
	def test_blank_enum_name_refused(self, bad):
		with pytest.raises(ValueError, match="enum_name must be"):
			parse_spec(one_clip(enum_name=bad))

	@pytest.mark.parametrize("field,bad", [
		("clip", "My/Clip"), ("enum_name", "..\\..\\evil"),
	])
	def test_traversal_refused(self, field, bad):
		with pytest.raises(ValueError, match="localisation"):
			parse_spec(one_clip(**{field: bad}))

	def test_loop_must_be_a_bool(self):
		with pytest.raises(ValueError, match="true or false"):
			parse_spec(one_clip(loop=1))

	def test_weight_null_opts_out_of_the_pool(self):
		assert parse_spec(one_clip(weight=None))[0]["weight"] is None

	@pytest.mark.parametrize("bad", [-1, 0, True, "3"])
	def test_bad_weight_refused(self, bad):
		with pytest.raises(ValueError, match="weight"):
			parse_spec(one_clip(weight=bad))

	def test_duration_is_recorded_when_given(self):
		assert parse_spec(one_clip(duration=3.5))[0]["duration"] == 3.5

	@pytest.mark.parametrize("bad", [0, -1, True, "3"])
	def test_bad_duration_refused(self, bad):
		with pytest.raises(ValueError, match="duration"):
			parse_spec(one_clip(duration=bad))

	def test_a_clip_that_is_only_a_qualifier_refused(self):
		with pytest.raises(ValueError, match="no name after"):
			parse_spec(one_clip(clip="$"))

	def test_explicit_duplicate_enum_refused(self):
		body = payload()
		body["clips"].append({"clip": "ClipB", "enum_name": "Static",
							  "label_text": "B", "loop": True})
		with pytest.raises(ValueError, match="duplicates clips\\[0\\]"):
			parse_spec(body)

	def test_defaulted_duplicate_enum_says_how_to_fix_it(self):
		body = payload(clips=[
			{"clip": "ClipA", "label_text": "A", "loop": True},
			{"clip": "Other$ClipA", "label_text": "B", "loop": False}])
		with pytest.raises(ValueError, match="give one of"):
			parse_spec(body)

	@pytest.mark.parametrize("field,bad", [
		("blend_time", 0), ("blend_time", "fast"), ("blend_time", True),
		("family", 7), ("family", " "), ("auto_label", 7), ("auto_label", ""),
	])
	def test_bad_payload_fields_refused(self, field, bad):
		with pytest.raises(ValueError, match=field):
			parse_spec(payload(**{field: bad}))

	def test_good_payload_fields_pass(self):
		assert parse_spec(payload(blend_time=0.2, family="anim_choices",
								  auto_label="Auto"))

	def test_events_are_normalised_through(self):
		row = parse_spec(one_clip(events=[{"name": "VFX_X", "type": "VFXEnable"}]))[0]
		assert row["events"][0]["location"] == ""

	def test_a_defaulted_enum_name_is_charset_checked_too(self):
		# short_clip splits on the FIRST '$' only, so "A$B$C" defaults the enum
		# name to "B$C" - which loc_symbol turns into "..._B$C.txt". one_clip's
		# base payload always carries an enum_name, so the key is dropped here
		# to genuinely exercise the DEFAULTED path, not the explicit one
		body = one_clip(clip="A$B$C")
		del body["clips"][0]["enum_name"]
		with pytest.raises(ValueError, match="localisation"):
			parse_spec(body)


class TestLoadSpec:

	def write(self, tmp_path, text):
		path = os.path.join(tmp_path, "x.animspec.json")
		with open(path, "w", encoding="utf-8") as f:
			f.write(text)
		return path

	def test_round_trip(self, tmp_path):
		rows = load_spec(self.write(tmp_path, json.dumps(payload())))
		assert rows[0]["clip"] == "ClipA"

	def test_missing_file_diagnosed(self, tmp_path):
		with pytest.raises(ValueError, match="not found"):
			load_spec(os.path.join(tmp_path, "nope.json"))

	def test_malformed_json_diagnosed(self, tmp_path):
		with pytest.raises(ValueError, match="not valid JSON"):
			load_spec(self.write(tmp_path, "{not json"))

	def test_game_is_forwarded_to_the_catalogue_check(self, tmp_path):
		body = one_clip(events=[{"name": "Not_A_Real_Event", "type": "AudioEvent"}])
		with pytest.raises(ValueError, match="does not hash"):
			load_spec(self.write(tmp_path, json.dumps(body)), game="Planet Coaster 2")


# --- durations ----------------------------------------------------------------

class ManiInfo:

	def __init__(self, name, duration):
		self.name = name
		self.duration = duration


class TestDurations:

	def test_names_are_folded_to_lower_case(self):
		# the manis stores them folded; matching the raw string finds nothing,
		# which looks exactly like a missing clip
		manis = type("M", (), {"mani_infos": [ManiInfo("Prop$Bend", 2.0)]})()
		assert durations_from_manis(manis) == {"prop$bend": 2.0}

	def test_absent_attributes_do_not_explode(self):
		manis = type("M", (), {"mani_infos": [object()]})()
		assert durations_from_manis(manis) == {"": None}

	def test_choice_duration_is_exact_not_rounded(self):
		assert choice_duration(7.9667) == 7.9667

	def test_sub_second_clips_are_floored_at_one(self):
		# a sub-frame block is unusable on the sequencer timeline
		assert choice_duration(0.0333) == 1.0

	def test_no_duration_is_an_error(self):
		with pytest.raises(ValueError, match="no duration"):
			choice_duration(None)

	def test_frames_match_the_exporter(self):
		# 90 frames at 24 fps is 89/24, not 90/24 - one frame, two sources of truth
		assert duration_from_frames(90, 24) == pytest.approx(89 / 24)


# --- ClipSpec -----------------------------------------------------------------

class TestClipSpec:

	def test_flags_follow_loop(self):
		assert clip_spec(loop=True).animation_flags == FLAGS_LOOPING
		assert clip_spec(loop=False).animation_flags == FLAGS_ONE_SHOT

	def test_from_clip_discards_the_authors_prefix(self):
		s = ClipSpec.from_clip("Old$MyClip", "Label", 2.0, prefix="New")
		assert s.clip == "New$MyClip"
		assert s.label_symbol == "InfoPanel_AnimationType_New_MyClip"

	def test_from_clip_takes_the_asset_name_over_the_prefix(self):
		s = ClipSpec.from_clip("MyClip", "Label", 2.0, prefix="myprop",
							   asset_name="MyProp", enum_name="Alt", weight=3)
		assert s.label_symbol == "InfoPanel_AnimationType_MyProp_Alt"
		assert s.weight == 3

	def test_from_clip_without_a_prefix_refuses_to_guess(self):
		with pytest.raises(ValueError, match="must come from the target asset"):
			ClipSpec.from_clip("MyClip", "Label", 2.0)

	def test_from_clip_needs_a_name_after_the_qualifier(self):
		with pytest.raises(ValueError, match="no name after"):
			ClipSpec.from_clip("$", "Label", 2.0, prefix="p")

	def test_unqualified_states_intent_only(self):
		s = ClipSpec.unqualified("Old$MyClip", "Label", 2.0, loop=False,
								 events=[{"name": "X"}], weight=None)
		assert (s.clip, s.enum_name, s.label_symbol) == ("MyClip", "MyClip", None)
		assert s.is_qualified is False
		assert s.weight is None

	def test_unqualified_needs_a_name_after_the_qualifier(self):
		with pytest.raises(ValueError, match="no name after"):
			ClipSpec.unqualified("$", "Label", 2.0)

	def test_is_qualified_needs_both_halves(self):
		assert clip_spec().is_qualified is True
		assert ClipSpec("bare", "e", "sym", "l", 1.0).is_qualified is False
		assert ClipSpec("P$c", "e", None, "l", 1.0).is_qualified is False

	def test_qualify_binds_a_target(self):
		s = ClipSpec.unqualified("MyClip", "Label", 2.0).qualify("myprop", "MyProp")
		assert s.clip == "myprop$MyClip"
		assert s.label_symbol == "InfoPanel_AnimationType_MyProp_MyClip"
		assert s.is_qualified is True

	def test_qualify_defaults_the_asset_name_to_the_prefix(self):
		s = ClipSpec.unqualified("MyClip", "Label", 2.0).qualify("Prop")
		assert s.label_symbol == "InfoPanel_AnimationType_Prop_MyClip"

	def test_repr_names_the_clip_and_the_flags(self):
		assert repr(clip_spec()) == "ClipSpec('Prop$MyClip', dur=2.5, flags=17)"

	@pytest.mark.xfail(reason="qualify does not forward weight, so an opted-out row "
							  "silently rejoins the random pool at weight 1",
					   strict=True)
	def test_qualify_keeps_the_weight(self):
		s = ClipSpec.unqualified("MyClip", "Label", 2.0, weight=None).qualify("Prop")
		assert s.weight is None


# --- graph primitives ---------------------------------------------------------

class TestFindEnumNodes:
	"""Retail graphs have TWO; extending only the first leaves the timed mode on
	the donor's branch set, which reads in game as "the selection is ignored"."""

	def test_both_nodes_are_found(self):
		assert len(find_enum_nodes(graph())) == 2

	def test_a_node_bound_to_another_variable_is_not_the_dropdown(self):
		g = ET.fromstring(f"<motiongraph><nodes>"
						  f'<mrfmember1 id="1">'
						  f"<lua_method>MotionGraph.VariableResultEnum</lua_method>"
						  f"<target_name>SomethingElse</target_name></mrfmember1>"
						  f"</nodes></motiongraph>")
		assert find_enum_nodes(g) == []

	def test_elements_without_a_lua_method_are_skipped(self):
		g = ET.fromstring("<motiongraph><x /><y><lua_method /></y></motiongraph>")
		assert find_enum_nodes(g) == []

	def test_find_enum_node_returns_the_first(self):
		g = graph()
		assert find_enum_node(g) is find_enum_nodes(g)[0]

	def test_find_enum_node_on_a_graph_without_one(self):
		assert find_enum_node(ET.fromstring("<motiongraph />")) is None


class TestFreshen:

	def test_definitions_get_new_ids_and_inner_refs_follow(self):
		sub = ET.fromstring('<sub id="1"><a id="2" ref="1" /><b ref="99" />'
							"<c /></sub>")
		assert freshen(sub, 100) == 102
		assert sub.get("id") == "100"
		assert sub.find("./a").get("id") == "101"
		assert sub.find("./a").get("ref") == "100"
		# refs OUT of the subtree are genuinely shared and must stay shared
		assert sub.find("./b").get("ref") == "99"

	def test_next_free_id_ignores_non_numeric_ids(self):
		g = ET.fromstring('<g><a id="5" /><b id="notanumber" /><c id="" />'
						  "<d /></g>")
		assert _next_free_id(g) == 6


class TestLuaResults:

	def el(self, text):
		return ET.fromstring(f"<lua_results>{text}</lua_results>")

	def test_a_slot_is_appended_at_the_top_level(self):
		lua = self.el(lua_text(5))
		_append_lua_result(lua)
		assert _count_lua_result_slots(lua) == 6
		# nested inside slot 1 was the old defect; the new slot is a sibling
		assert "[6] = {  }," in lua.text

	def test_missing_marker_refused(self):
		with pytest.raises(ValueError, match="not found in lua_results"):
			_append_lua_result(self.el("nothing here"))

	def test_marker_without_result_params_refused(self):
		with pytest.raises(ValueError, match="no ResultParams"):
			_append_lua_result(self.el(f'VariableName = "{LOOP_ANIM_SELECTION}"'))

	def test_unbalanced_braces_refused(self):
		text = f'ResultParams = {{ [1] = {{  }}, VariableName = "{LOOP_ANIM_SELECTION}"'
		with pytest.raises(ValueError, match="unbalanced braces"):
			_append_lua_result(self.el(text))

	@pytest.mark.parametrize("text,expected", [
		("nothing here", None),
		("", None),
		(f'ResultParams = {{ [1] = {{  }}, VariableName = "{LOOP_ANIM_SELECTION}"',
		 None),
	])
	def test_counting_gives_up_rather_than_guessing(self, text, expected):
		assert _count_lua_result_slots(self.el(text)) is expected

	@pytest.mark.xfail(reason="rfind walks BACK from the marker, so a VariableName-"
							  "before-ResultParams entry widens the previous entry",
					   strict=True)
	def test_the_slot_lands_on_the_loop_anim_entry(self):
		lua = self.el(lua_text(2, varname_first=True))
		_append_lua_result(lua)
		tail = lua.text[lua.text.find(f'VariableName = "{LOOP_ANIM_SELECTION}"'):]
		assert "[3] = {  }," in tail

	@pytest.mark.xfail(reason="the count repeats the same backwards lookup, so it "
							  "reports the WRONG entry and agrees with the error",
					   strict=True)
	def test_counting_reads_the_loop_anim_entry(self):
		assert _count_lua_result_slots(self.el(lua_text(2, varname_first=True))) == 2


class TestResolveEnumHolder:
	"""Resolving this wrong is silent and expensive: every appended name became a
	sibling element and the OVL writer dropped it on repack."""

	def test_the_ptrs_shape(self):
		root = enum(2, tag="ptrs")
		assert resolve_enum_holder(root).tag == "ptrs"

	def test_the_strings_shape(self):
		assert resolve_enum_holder(enum(2)).tag == "strings"

	def test_the_holder_itself_is_accepted(self):
		holder = enum(2).find("./strings")
		assert resolve_enum_holder(holder) is holder

	def test_an_unrecognisable_element_is_diagnosed(self):
		with pytest.raises(ValueError, match="cannot locate"):
			resolve_enum_holder(ET.fromstring("<enumnamer />"))


class TestRepointStateOutputs:

	def test_both_links_move_and_the_raw_value_goes(self):
		# ptr_0 (+16) and motiongraph_vars (+24) are always the same target, so a
		# stale `raw` left behind would win over the ref
		clone = ET.fromstring(
			'<mrfchild><mrfmember1><lua_method>MotionGraph.StateOutput</lua_method>'
			'<ptr_0 raw="00" ref="1" /><motiongraph_vars ref="1" />'
			"</mrfmember1></mrfchild>")
		assert _repoint_state_outputs(clone, "77", 200) == 200
		assert clone.find(".//ptr_0").attrib == {"ref": "77"}
		assert clone.find(".//motiongraph_vars").get("ref") == "77"

	def test_other_nodes_are_left_alone(self):
		clone = ET.fromstring(
			"<mrfchild><a /><b><lua_method>MotionGraph.Other</lua_method>"
			'<ptr_0 ref="1" /></b><c><lua_method /></c></mrfchild>')
		_repoint_state_outputs(clone, "77", 200)
		assert clone.find(".//ptr_0").get("ref") == "1"

	def test_a_state_output_missing_either_link(self):
		clone = ET.fromstring(
			"<mrfchild><m><lua_method>MotionGraph.StateOutput</lua_method>"
			"</m></mrfchild>")
		_repoint_state_outputs(clone, "77", 200)
		assert clone.find(".//m").find("./ptr_0") is None


class TestStripDatastreams:
	"""A cloned State brings the donor's events with it, so the new clip would
	fire the donor's sounds on its own timing."""

	def test_inherited_streams_are_removed(self):
		state = ET.fromstring(_state_xml(1, inherited=3)).find("./state")
		assert strip_datastreams(state) == 3
		assert list(state.find(".//additional_data_streams")) == []

	def test_nothing_to_strip(self):
		state = ET.fromstring(_state_xml(1)).find("./state")
		assert strip_datastreams(state) == 0


class TestAppendDatastreams:

	def state(self):
		return ET.fromstring(_state_xml(1)).find("./state")

	def test_no_events_is_a_no_op(self):
		assert append_datastreams(self.state(), [], "2") == 0

	def test_a_state_without_the_list_is_diagnosed(self):
		state = ET.fromstring(_state_xml(1, streams=False)).find("./state")
		with pytest.raises(ValueError, match="no additional_data_streams"):
			append_datastreams(state, audio_event(), "2")

	def test_the_measured_step_curve(self):
		state = self.state()
		assert append_datastreams(state, audio_event(at=0.3), "2") == 1
		entry = state.find(".//datastreamresourcedata")
		assert entry.get("curve_type") == "1"
		points = list(entry.iter("curvedatapoint"))
		assert [p.get("x") for p in points] == ["0.0", "0.3", "1.0"]
		assert [p.get("y") for p in points] == ["16384", "16448", "16448"]
		assert all(p.get("subsequent_curve_param_b") == "16384" for p in points)
		assert entry.find("./bone_i_d").get("ref") == "2"
		assert entry.find("./location").text == "Default"

	def test_an_empty_location_stays_self_closing(self):
		state = self.state()
		events = parse_events([{"name": "VFX_X", "type": "VFXEnable"}], "c")
		append_datastreams(state, events, "2")
		# retail's own shape for a VFX event: <location />, not <location></location>
		assert state.find(".//location").text is None


# --- append_clips -------------------------------------------------------------

def append(specs, g=None, e=None, c=None, n=2):
	g = graph(n_branches=n) if g is None else g
	e = enum(n) if e is None else e
	c = choices(n) if c is None else c
	return append_clips(g, e, c, specs), g, e, c


class TestAppendClips:

	def test_one_clip_lands_in_all_three_trees(self):
		spec = clip_spec()
		added, g, e, c = append([spec])
		[got] = added
		assert got["clip"] == "Prop$MyClip"
		assert got["index"] == "2"
		assert got["branches"] == ["3", "3"]
		assert got["flags"] == FLAGS_LOOPING
		assert got["label_symbol"] == spec.label_symbol
		assert got["label_text"] == spec.label_text

		new_sr = g.findall("./state_output_entries/states/statereference")[-1]
		assert new_sr.find(".//mani").text == "Prop$MyClip"
		assert new_sr.find("./state").get("id") == got["state_id"]
		assert new_sr.find(".//data").get("animation_flags") == "17"

		names = [p.text for p in resolve_enum_holder(e)]
		assert names == ["Name0", "Name1", "MyClip"]

		row = [x for x in c.iter() if x.get("index") == "2"][0]
		assert row.get("duration") == "2.5"
		assert row.find("./label").text == f"[{spec.label_symbol}]"
		# the copied label kept the donor's share id, which would alias the string
		assert "id" not in row.find("./label").attrib
		assert c.get("count") == "3"
		assert _count_lua_result_slots(g.find("./lua_results")) == 3

	def test_the_donor_is_not_edited(self):
		_, g, _, _ = append([clip_spec(loop=False)])
		donor = g.find("./state_output_entries/states/statereference")
		assert donor.find(".//mani").text == "Donor$Idle"
		assert donor.find(".//data").get("animation_flags") == "17"

	def test_a_one_shot_clears_the_looping_bit(self):
		_, g, _, _ = append([clip_spec(loop=False)])
		new_sr = g.findall("./state_output_entries/states/statereference")[-1]
		assert new_sr.find(".//data").get("animation_flags") == str(FLAGS_ONE_SHOT)

	def test_every_enum_node_gains_a_branch_pointing_at_the_new_state(self):
		# extending only the first node leaves the trigger-sequenced mode on the
		# donor's branch set - the "timed mode ignores my selection" failure
		added, g, _, _ = append([clip_spec()])
		for node in find_enum_nodes(g):
			branch = node.find("./children").findall("./mrfchild")[-1]
			assert branch.get("count_0") == "3"
			assert branch.find(".//ptr_0").get("ref") == added[0]["state_id"]
			assert branch.find(".//ptr_0").get("raw") is None
			assert branch.find(".//motiongraph_vars").get("ref") == added[0]["state_id"]

	def test_two_specs_in_one_call_get_their_own_states(self):
		added, g, e, c = append([clip_spec("A"), clip_spec("B")])
		assert [a["index"] for a in added] == ["2", "3"]
		assert len({a["state_id"] for a in added}) == 2
		assert len(g.findall("./state_output_entries/states/statereference")) == 3
		assert [p.text for p in resolve_enum_holder(e)][-2:] == ["A", "B"]
		assert c.get("count") == "4"

	def test_the_enum_root_is_accepted_as_well_as_the_holder(self):
		root = enum(2)
		append([clip_spec()], e=root.find("./strings"))
		assert [p.text for p in root.find("./strings")][-1] == "MyClip"

	def test_inherited_datastreams_are_stripped_from_the_clone(self):
		g = graph(inherited=2)
		append([clip_spec()], g=g)
		new_sr = g.findall("./state_output_entries/states/statereference")[-1]
		assert list(new_sr.find(".//additional_data_streams")) == []
		donor = g.find("./state_output_entries/states/statereference")
		assert len(list(donor.find(".//additional_data_streams"))) == 2

	def test_events_are_armed_on_the_clones_own_sync_id(self):
		g = graph(inherited=1)
		append([clip_spec(events=audio_event())], g=g)
		new_sr = g.findall("./state_output_entries/states/statereference")[-1]
		entries = new_sr.findall(".//datastreamresourcedata")
		assert len(entries) == 1
		sync_id = new_sr.find(".//sync_prop_through_variable").get("id")
		assert entries[0].find("./bone_i_d").get("ref") == sync_id

	def test_the_new_state_id_is_free(self):
		added, g, _, _ = append([clip_spec()])
		ids = [el.get("id") for el in g.iter() if el.get("id")]
		assert ids.count(added[0]["state_id"]) == 1


class TestAppendClipsRefuses:
	"""Every entry here corresponds to a failure that is silent in game."""

	def test_an_unqualified_spec(self):
		spec = ClipSpec.unqualified("MyClip", "Label", 2.0)
		with pytest.raises(ValueError, match="never bound to a target asset"):
			append([spec])

	def test_a_name_the_asset_already_has(self):
		# append-only means a duplicate cannot be removed, and the result stays
		# internally consistent so no post-condition catches it
		with pytest.raises(ValueError, match="already present in this asset"):
			append([clip_spec(enum_name="Name1")])

	def test_a_graph_without_state_output_entries(self):
		with pytest.raises(ValueError, match="state_output_entries"):
			append([clip_spec()], g=graph(with_soe=False))

	def test_a_graph_without_the_dropdown_node(self):
		with pytest.raises(ValueError, match="no VariableResultEnum node"):
			append([clip_spec()], g=graph(n_nodes=0))

	@pytest.mark.parametrize("kwargs", [{"with_lua": False}, {"lua": ""}])
	def test_a_graph_without_lua_results(self, kwargs):
		with pytest.raises(ValueError, match="no lua_results"):
			append([clip_spec()], g=graph(**kwargs))

	def test_lua_results_without_the_marker(self):
		with pytest.raises(ValueError, match="not found in lua_results"):
			append([clip_spec()], g=graph(lua="ResultParams = { [1] = {  }, }"))

	def test_no_donor_state_to_clone(self):
		with pytest.raises(ValueError, match="no full State definition"):
			append([clip_spec()], g=graph(mani=False))

	def test_events_but_no_sync_variable_to_bind_them_to(self):
		with pytest.raises(ValueError, match="no defined sync_prop_through_variable"):
			append([clip_spec(events=audio_event())], g=graph(sync=False))

	def test_events_but_no_datastream_list(self):
		with pytest.raises(ValueError, match="no additional_data_streams"):
			append([clip_spec(events=audio_event())], g=graph(streams=False))

	def test_a_spec_with_no_duration(self):
		# choice_duration() already treats a missing duration as an error, and
		# `duration` is optional in the spec schema, so the value can arrive here
		# as None. The row it produces is what the sequencer reads for
		# playDurationSeconds, so a bad value is silent rather than fatal
		with pytest.raises(ValueError, match="duration"):
			append([clip_spec(duration=None)])

	@pytest.mark.xfail(reason="an empty enum holder raises a bare IndexError where "
							  "every sibling precondition raises a diagnosed error",
					   strict=True)
	def test_an_empty_enum_holder_is_diagnosed(self):
		with pytest.raises(ValueError):
			append([clip_spec()], e=enum(0), c=choices(0))

	@pytest.mark.xfail(reason="an enum node with no <children> raises a bare "
							  "AttributeError instead of naming the node",
					   strict=True)
	def test_an_enum_node_without_children_is_diagnosed(self):
		with pytest.raises(ValueError):
			append([clip_spec()], g=graph(children=False))

	def test_a_duplicate_within_one_specs_list(self):
		with pytest.raises(ValueError, match="duplicate"):
			append([clip_spec("A", enum_name="Same"),
					clip_spec("B", enum_name="Same")])

	@pytest.mark.xfail(reason="append_clips([]) reports success and changes nothing, "
							  "which is the failure mode parse_spec refuses",
					   strict=True)
	def test_an_empty_specs_list(self):
		with pytest.raises(ValueError):
			append([])


class TestAppendClipsAtomicity:

	def test_every_ref_in_the_appended_state_still_resolves(self):
		# a dangling ref is silent: the entry appears in the dropdown and plays
		# nothing, which is exactly constraint 2 in the module docstring
		_, g, _, _ = append([clip_spec()])
		ids = {el.get("id") for el in g.iter() if el.get("id") is not None}
		new_sr = g.findall("./state_output_entries/states/statereference")[-1]
		refs = {el.get("ref") for el in new_sr.iter() if el.get("ref") is not None}
		assert refs <= ids

	def test_a_later_failure_rolls_the_earlier_specs_back(self):
		# the donor-structural checks now run once, before the loop, so this
		# refuses before spec A is ever applied - not a general rollback, but
		# this exact shape (a later spec needing something the shared donor
		# lacks) is one of the cases that is now caught up front
		g, e, c = graph(sync=False), enum(2), choices(2)
		with pytest.raises(ValueError, match="sync_prop_through_variable"):
			append_clips(g, e, c, [clip_spec("A"),
								   clip_spec("B", events=audio_event())])
		assert len(g.findall("./state_output_entries/states/statereference")) == 1
		assert len(list(resolve_enum_holder(e))) == 2
		assert c.get("count") == "2"

	def test_a_bad_duration_on_a_later_spec_refuses_before_any_spec_lands(self):
		g, e, c = graph(), enum(2), choices(2)
		with pytest.raises(ValueError, match="B: clip has no duration"):
			append_clips(g, e, c, [clip_spec("A"),
								   clip_spec("B", duration=None)])
		assert len(g.findall("./state_output_entries/states/statereference")) == 1
		assert len(list(resolve_enum_holder(e))) == 2
		assert c.get("count") == "2"

	def test_a_missing_additional_data_streams_refuses_up_front_too(self):
		g, e, c = graph(streams=False), enum(2), choices(2)
		with pytest.raises(ValueError, match="additional_data_streams"):
			append_clips(g, e, c, [clip_spec("A", events=audio_event())])
		assert len(g.findall("./state_output_entries/states/statereference")) == 1

	def test_no_events_in_the_batch_skips_the_donor_event_checks(self):
		# sync=False would refuse if any spec had events - none do here, so
		# the donor-structural check for it must not even run
		g, e, c = graph(sync=False), enum(2), choices(2)
		append_clips(g, e, c, [clip_spec("A"), clip_spec("B")])
		assert len(g.findall("./state_output_entries/states/statereference")) == 3


class TestCheckSpecsQualified:

	def test_the_message_lists_at_most_five(self):
		specs = [ClipSpec.unqualified(f"C{i}", "L", 1.0) for i in range(6)]
		with pytest.raises(ValueError, match="6 spec"):
			_check_specs_qualified(specs)

	def test_qualified_specs_pass(self):
		_check_specs_qualified([clip_spec()])


class TestPostConditions:
	"""Each of these corresponds to a defect that shipped and was found in game
	rather than at build time."""

	def run(self, n_enum=3, n_choices=3, branches=(3, 3), before=(2, 2),
			n_enum_before=2, n_slots=3, n_specs=1):
		holder = ET.fromstring("<strings>" + "<pointer>x</pointer>" * n_enum
							   + "</strings>")
		rows = ET.fromstring(f'<choices count="{n_choices}" />')
		nodes = [ET.fromstring("<n><children>" + "<mrfchild />" * b + "</children></n>")
				 for b in branches]
		soe = ET.fromstring("<states />")
		lua = ET.fromstring(f"<lua_results>{lua_text(n_slots)}</lua_results>")
		return _check_post_conditions(nodes, list(before), holder, n_enum_before,
									  rows, soe, lua, n_specs)

	def test_a_consistent_result_passes(self):
		assert self.run() is None

	def test_names_that_did_not_land(self):
		with pytest.raises(ValueError, match="did not land in the name list"):
			self.run(n_specs=2)

	def test_names_disagreeing_with_choice_rows(self):
		with pytest.raises(ValueError, match="choice rows"):
			self.run(n_choices=4)

	def test_a_node_that_missed_the_append(self):
		with pytest.raises(ValueError, match="enum node 1 branches went"):
			self.run(branches=(3, 1), before=(2, 1))

	def test_a_node_out_of_step_with_the_choice_rows(self):
		# both deltas are right, but the node started from a different base, so
		# index N means different things in the two modes
		with pytest.raises(ValueError, match="would disagree about what index"):
			self.run(branches=(3, 4), before=(2, 3))

	def test_lua_slots_out_of_step(self):
		with pytest.raises(ValueError, match="ResultParams slots"):
			self.run(n_slots=4)
