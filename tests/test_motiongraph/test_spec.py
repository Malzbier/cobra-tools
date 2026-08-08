"""The animation spec has ONE reader; these tests pin its contract.

parse_spec validates untrusted, shareable input - enum names and clip names
become identifiers and localisation FILENAMES, so the charset rules are a
security boundary, not cosmetics. read_animspec adapts validated rows to
generator choices and enforces the anim_choices-family conventions.
"""
import json
import os

import pytest

import xml.etree.ElementTree as ET

from modules.motiongraph_author import append_datastreams, parse_events, parse_spec
from modules.prop_builder import PropBuildError, read_animspec


def spec(**overrides):
	payload = {
		"spec_version": 2,
		"clips": [
			{"clip": "ClipA", "enum_name": "Static", "label_text": "Rest",
			 "loop": True, "weight": 1},
			{"clip": "ClipB", "label_text": "Play", "loop": True, "weight": 3},
		],
	}
	payload.update(overrides)
	return payload


class TestParseSpec:

	def test_valid_rows(self):
		rows = parse_spec(spec())
		assert [r["enum_name"] for r in rows] == ["Static", "ClipB"]
		assert [r["weight"] for r in rows] == [1, 3]

	def test_weight_defaults_to_one(self):
		payload = spec()
		del payload["clips"][0]["weight"]
		assert parse_spec(payload)[0]["weight"] == 1

	def test_weight_null_opts_out(self):
		payload = spec()
		payload["clips"][0]["weight"] = None
		assert parse_spec(payload)[0]["weight"] is None

	@pytest.mark.parametrize("bad", [-1, 0, True, "3"])
	def test_bad_weight_refused(self, bad):
		payload = spec()
		payload["clips"][0]["weight"] = bad
		with pytest.raises(ValueError):
			parse_spec(payload)

	def test_enum_traversal_refused(self):
		# enum_name becomes the tail of a loc FILENAME; a separator here is a
		# path traversal, and the message must say so
		payload = spec()
		payload["clips"][0]["enum_name"] = "..\\..\\evil"
		with pytest.raises(ValueError, match="localisation"):
			parse_spec(payload)

	def test_duplicate_enum_refused(self):
		payload = spec()
		payload["clips"][1]["enum_name"] = "Static"
		with pytest.raises(ValueError, match="duplicates"):
			parse_spec(payload)

	@pytest.mark.parametrize("field,bad", [
		("blend_time", 0), ("blend_time", "fast"),
		("family", 7), ("family", " "),
		("auto_label", 7),
	])
	def test_bad_payload_fields_refused(self, field, bad):
		with pytest.raises(ValueError):
			parse_spec(spec(**{field: bad}))


class TestParseEvents:
	"""Event rules are measured, not chosen: curve_type follows the type, VFX
	places itself by naming a prefab child, audio places itself by location."""

	def test_defaults(self):
		[e] = parse_events([{"name": "MyEvent", "type": "AudioEvent"}], "c")
		assert e == {"name": "MyEvent", "type": "AudioEvent", "at": 0.1,
					 "location": "Default", "particle": None}

	def test_vfx_location_forced_empty(self):
		[e] = parse_events([{"name": "VFX_Fire", "type": "VFXEnable"}], "c")
		assert e["location"] == ""

	def test_vfx_with_location_refused(self):
		with pytest.raises(ValueError, match="must not set"):
			parse_events([{"name": "VFX_Fire", "type": "VFXEnable",
						   "location": "Default"}], "c")

	def test_unknown_type_refused(self):
		with pytest.raises(ValueError, match="datastream type"):
			parse_events([{"name": "X", "type": "Explode"}], "c")

	@pytest.mark.parametrize("bad", [-0.1, 1.5, "half", True])
	def test_out_of_clip_trigger_refused(self, bad):
		with pytest.raises(ValueError):
			parse_events([{"name": "X", "type": "AudioEvent", "at": bad}], "c")

	def test_hostile_name_refused(self):
		with pytest.raises(ValueError):
			parse_events([{"name": "X<y", "type": "AudioEvent"}], "c")

	def test_absent_is_empty(self):
		assert parse_events(None, "c") == []


class TestEventCatalogue:
	"""Offline typo-catching against the mined catalogues (constants/<game>/
	particles.py, constants/<game>/audio_hashes.bin). Names below are real
	retail Planet Coaster 2 data, proven in game and re-proven by the mod-side
	catalogue-mining self-check this data was ported from - see
	constants/audio_hashes.py."""

	GAME = "Planet Coaster 2"
	REAL_EVENT = "Sny_AnimatedSny_Medusa_Snap"
	REAL_PARTICLE = "sce_vi_bellows_blow"

	def test_audio_name_no_game_skips_check(self):
		# no game given: shape-valid but fabricated names must NOT be rejected
		[e] = parse_events([{"name": "Not_A_Real_Event", "type": "AudioEvent"}], "c")
		assert e["name"] == "Not_A_Real_Event"

	def test_audio_name_unmeasured_game_skips_check(self):
		[e] = parse_events([{"name": "Not_A_Real_Event", "type": "AudioEvent"}], "c",
						   game="Jurassic World Evolution 2")
		assert e["name"] == "Not_A_Real_Event"

	def test_audio_name_real_event_passes(self):
		[e] = parse_events([{"name": self.REAL_EVENT, "type": "AudioEvent"}], "c",
						   game=self.GAME)
		assert e["name"] == self.REAL_EVENT

	def test_audio_name_fake_event_refused(self):
		with pytest.raises(ValueError, match="does not hash"):
			parse_events([{"name": "Not_A_Real_Event", "type": "AudioEvent"}], "c",
						 game=self.GAME)

	def test_particle_real_name_passes(self):
		[e] = parse_events([{"name": "VFX_Fire", "type": "VFXEnable",
							 "particle": self.REAL_PARTICLE}], "c", game=self.GAME)
		assert e["particle"] == self.REAL_PARTICLE

	def test_particle_fake_name_refused(self):
		with pytest.raises(ValueError, match="not a known"):
			parse_events([{"name": "VFX_Fire", "type": "VFXEnable",
						   "particle": "not_a_real_particle"}], "c", game=self.GAME)

	def test_particle_absent_is_none(self):
		[e] = parse_events([{"name": "VFX_Fire", "type": "VFXEnable"}], "c",
						   game=self.GAME)
		assert e["particle"] is None

	def test_particle_on_audio_event_refused(self):
		with pytest.raises(ValueError, match="only applies to VFX"):
			parse_events([{"name": self.REAL_EVENT, "type": "AudioEvent",
						   "particle": self.REAL_PARTICLE}], "c")

	def test_particle_hostile_name_refused(self):
		with pytest.raises(ValueError):
			parse_events([{"name": "VFX_Fire", "type": "VFXEnable",
						   "particle": "../evil"}], "c")


class TestAppendDatastreams:
	"""append_clips's event emitter - the APPEND path's counterpart to the
	generator's datastreams_xml, same measured curve, ElementTree instead of
	string building because this mutates an already-parsed graph tree."""

	def state(self):
		# the minimal shape append_datastreams needs: a state whose data
		# carries additional_data_streams and its own sync_prop_through_variable
		s = ET.fromstring(
			'<state id="9"><activities><activityreference><activity>'
			'<data><sync_prop_through_variable id="42" />'
			'<additional_data_streams /></data>'
			'</activity></activityreference></activities></state>')
		return s

	def test_empty_events_untouched(self):
		s = self.state()
		assert append_datastreams(s, [], "42") == 0
		assert list(s.find(".//additional_data_streams")) == []

	def test_curve_shape_matches_measured_constants(self):
		s = self.state()
		events = parse_events([{"name": "MyEvent", "type": "AudioEvent", "at": 0.3}], "c")
		n = append_datastreams(s, events, "42")
		assert n == 1
		d = s.find(".//datastreamresourcedata")
		pts = list(d.iter("curvedatapoint"))
		assert [p.get("x") for p in pts] == ["0.0", "0.3", "1.0"]
		assert [p.get("y") for p in pts] == ["16384", "16448", "16448"]
		assert all(p.get("subsequent_curve_param") == "16384" for p in pts)

	def test_bone_ref_is_the_states_own_sync_id(self):
		s = self.state()
		events = parse_events([{"name": "MyEvent", "type": "AudioEvent"}], "c")
		append_datastreams(s, events, "42")
		bone = s.find(".//bone_i_d")
		assert bone.get("ref") == "42"

	def test_vfx_location_self_closing(self):
		s = self.state()
		events = parse_events([{"name": "VFX_Fire", "type": "VFXEnable"}], "c")
		append_datastreams(s, events, "42")
		loc = s.find(".//location")
		assert loc.text is None  # self-closing <location />, not ""

	def test_audio_location_has_text(self):
		s = self.state()
		events = parse_events([{"name": "MyEvent", "type": "AudioEvent",
								"location": "Base_Default"}], "c")
		append_datastreams(s, events, "42")
		assert s.find(".//location").text == "Base_Default"

	def test_multiple_events_all_land(self):
		s = self.state()
		events = parse_events([
			{"name": "VFX_A", "type": "VFXEnable", "at": 0.1},
			{"name": "VFX_A", "type": "VFXDisable", "at": 0.9},
			{"name": "MyEvent", "type": "AudioEvent", "at": 0.5},
		], "c")
		assert append_datastreams(s, events, "42") == 3
		assert len(s.findall(".//datastreamresourcedata")) == 3

	def test_at_zero_shares_x_with_the_off_point(self):
		# in range (parse_events accepts [0.0, 1.0]) and reachable from a
		# Blender pose marker on a clip's first frame - see
		# TestEventsAtClipBoundary in test_generator.py for why a shared x is
		# not a conflict on this STEP curve
		s = self.state()
		events = parse_events([{"name": "MyEvent", "type": "AudioEvent", "at": 0.0}], "c")
		append_datastreams(s, events, "42")
		pts = list(s.find(".//datastreamresourcedata").iter("curvedatapoint"))
		assert [p.get("x") for p in pts] == ["0.0", "0.0", "1.0"]
		assert [p.get("y") for p in pts] == ["16384", "16448", "16448"]

	def test_at_one_shares_x_with_the_final_point(self):
		s = self.state()
		events = parse_events([{"name": "MyEvent", "type": "AudioEvent", "at": 1.0}], "c")
		append_datastreams(s, events, "42")
		pts = list(s.find(".//datastreamresourcedata").iter("curvedatapoint"))
		assert [p.get("x") for p in pts] == ["0.0", "1.0", "1.0"]
		assert [p.get("y") for p in pts] == ["16384", "16448", "16448"]


class TestReadAnimspec:

	def write(self, tmp_path, payload):
		p = os.path.join(tmp_path, "myprop.animspec.json")
		with open(p, "w", encoding="utf-8") as f:
			json.dump(payload, f)
		return p

	def test_auto_choice_synthesized_first(self, tmp_path):
		choices, blend = read_animspec(self.write(tmp_path, spec(
			blend_time=0.2, auto_label="Auto label")))
		assert choices[0] == ("Default", None, None, "Auto label", ())
		assert choices[1:] == [("Static", "ClipA", 1, "Rest", ()),
							   ("ClipB", "ClipB", 3, "Play", ())]
		assert blend == 0.2

	def test_events_reach_the_choice_rows(self, tmp_path):
		payload = spec()
		payload["clips"][0]["events"] = [
			{"name": "VFX_Fire", "type": "VFXEnable", "at": 0.02},
			{"name": "MyEvent", "type": "AudioEvent", "at": 0.1,
			 "location": "Base_Default"},
		]
		choices, _ = read_animspec(self.write(tmp_path, payload))
		events = choices[1][4]
		assert [e["type"] for e in events] == ["VFXEnable", "AudioEvent"]
		# a VFX event places itself by naming a child, so its location is empty
		assert events[0]["location"] == ""
		assert events[1]["location"] == "Base_Default"

	def test_unknown_family_refused(self, tmp_path):
		with pytest.raises(PropBuildError, match="family"):
			read_animspec(self.write(tmp_path, spec(family="door")))

	def test_one_shot_rows_refused(self, tmp_path):
		# graph generation emits looping states only; the append path is the
		# one that supports one-shots, so this must refuse rather than
		# silently emit a looping entry
		payload = spec()
		payload["clips"][0]["loop"] = False
		with pytest.raises(PropBuildError, match="loop"):
			read_animspec(self.write(tmp_path, payload))

	def test_missing_file_diagnosed(self, tmp_path):
		with pytest.raises(PropBuildError, match="not found"):
			read_animspec(os.path.join(tmp_path, "nope.animspec.json"))

	def test_invalid_json_diagnosed(self, tmp_path):
		p = os.path.join(tmp_path, "bad.animspec.json")
		with open(p, "w", encoding="utf-8") as f:
			f.write("{not json")
		with pytest.raises(PropBuildError, match="JSON"):
			read_animspec(p)
