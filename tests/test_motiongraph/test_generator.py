"""Semantic invariants of motiongraph generation.

Deliberately no byte or hash assertions: the OVL toolchain does not promise
byte-stable output, so these tests pin the properties that make a generated
graph WORK - id/ref integrity, the coupling between the choice list and every
artefact derived from it, and the loud refusal of inputs that would emit a
broken graph.
"""
import re
import xml.etree.ElementTree as ET

import pytest

from modules.motiongraph_generator import Ids, generate

AUTO = ("Default", None, None, "Auto")
VALID = [AUTO,
		 ("Static", "ClipA", 1, "Rest label"),
		 ("Idle01", "ClipB", 3, "Play label")]


def ids_and_refs(xml_text):
	ids = re.findall(r'id="(\d+)"', xml_text)
	refs = re.findall(r'ref="(\d+)"', xml_text)
	return ids, refs


class TestGenerate:

	@pytest.fixture(scope="class")
	def output(self):
		return generate("MyProp", VALID, "MyPropVars", 0.15)

	def test_well_formed(self, output):
		graph, enum = output
		ET.fromstring(graph)
		ET.fromstring(enum)

	def test_no_duplicate_ids_no_dangling_refs(self, output):
		graph, _ = output
		ids, refs = ids_and_refs(graph)
		assert len(ids) == len(set(ids))
		assert not set(refs) - set(ids)

	def test_manis_match_choices(self, output):
		graph, _ = output
		manis = set(re.findall(r"<mani>([^<]+)</mani>", graph))
		assert manis == {"MyProp$ClipA", "MyProp$ClipB"}

	def test_t4_params_match_choice_count(self, output):
		graph, _ = output
		lua = re.search(r"<lua_results>(.*?)</lua_results>", graph, re.S).group(1)
		t4 = re.search(r"t\[4\] = \{ ResultParams = \{(.*?)\},\s*VariableName", lua,
					   re.S).group(1)
		assert len(re.findall(r"\[\d+\]", t4)) == len(VALID)

	def test_t5_weights_match_pool(self, output):
		graph, _ = output
		lua = re.search(r"<lua_results>(.*?)</lua_results>", graph, re.S).group(1)
		weights = re.findall(r"Weight = (\d+)", lua)
		assert weights == ["1", "3"]

	def test_enumnamer_order(self, output):
		_, enum = output
		names = re.findall(r"<pointer>([^<]+)</pointer>", enum)
		assert names == ["Default", "Static", "Idle01"]

	def test_shared_clip_across_choices(self):
		# two dropdown entries over one clip is a legitimate, documented case
		graph, _ = generate("MyProp", [AUTO,
									   ("A", "ClipX", 1, "a"),
									   ("B", "ClipX", 2, "b")],
							"MyPropVars", 0.15)
		ids, refs = ids_and_refs(graph)
		assert len(ids) == len(set(ids))
		assert not set(refs) - set(ids)

	def test_weight_null_keeps_branch_and_state(self):
		# a null weight opts a clip out of the Auto pool, nothing else: its
		# state, its enum branch and its slot number must all survive
		graph, enum = generate("MyProp", [AUTO,
										  ("A", "ClipX", 1, "a"),
										  ("B", "ClipY", None, "b"),
										  ("C", "ClipZ", 2, "c")],
							   "MyPropVars", 0.15)
		manis = set(re.findall(r"<mani>([^<]+)</mani>", graph))
		assert "MyProp$ClipY" in manis
		assert re.findall(r"<pointer>([^<]+)</pointer>", enum) == \
			["Default", "A", "B", "C"]
		lua = re.search(r"<lua_results>(.*?)</lua_results>", graph, re.S).group(1)
		assert re.findall(r"Weight = (\d+)", lua) == ["1", "2"]


class TestEvents:
	"""The emitted curve must match the shape measured on armed, in-game
	verified entries: a step spanning the whole clip, 16384 before the
	trigger and 16448 from it onward, with curve_type derived from the type."""

	@pytest.fixture(scope="class")
	def graph(self):
		events = ({"name": "VFX_Fire", "type": "VFXEnable", "at": 0.02,
				   "location": ""},
				  {"name": "VFX_Fire", "type": "VFXDisable", "at": 0.98,
				   "location": ""},
				  {"name": "MyEvent", "type": "AudioEvent", "at": 0.1,
				   "location": "Base_Default"})
		choices = [AUTO,
				   ("Static", "ClipA", 1, "Rest", ()),
				   ("Idle01", "ClipB", 3, "Play", events)]
		return generate("MyProp", choices, "MyPropVars", 0.15)[0]

	def test_well_formed_with_events(self, graph):
		ET.fromstring(graph)

	def test_curve_type_derived_from_type(self, graph):
		root = ET.fromstring(graph)
		got = {d.findtext("type"): d.get("curve_type")
			   for d in root.iter("datastreamresourcedata")}
		assert got == {"VFXEnable": "65537", "VFXDisable": "65537",
					   "AudioEvent": "1"}

	def test_curve_is_a_step_spanning_the_clip(self, graph):
		root = ET.fromstring(graph)
		for d in root.iter("datastreamresourcedata"):
			pts = list(d.iter("curvedatapoint"))
			assert len(pts) == 3
			xs = [float(p.get("x")) for p in pts]
			ys = [p.get("y") for p in pts]
			assert xs[0] == 0.0 and xs[2] == 1.0
			assert xs[0] <= xs[1] <= xs[2]
			assert ys == ["16384", "16448", "16448"]

	def test_trigger_position_preserved(self, graph):
		root = ET.fromstring(graph)
		at_by_type = {d.findtext("type"): float(list(d.iter("curvedatapoint"))[1].get("x"))
					  for d in root.iter("datastreamresourcedata")}
		assert at_by_type["VFXEnable"] == pytest.approx(0.02)
		assert at_by_type["VFXDisable"] == pytest.approx(0.98)
		assert at_by_type["AudioEvent"] == pytest.approx(0.1)

	def test_bone_id_refs_the_states_sync_variable(self, graph):
		root = ET.fromstring(graph)
		for data in root.iter("data"):
			streams = list(data.iter("datastreamresourcedata"))
			if not streams:
				continue
			sync = data.find("sync_prop_through_variable").get("id")
			assert all(s.find("bone_i_d").get("ref") == sync for s in streams)

	def test_clip_without_events_emits_empty_tag(self, graph):
		root = ET.fromstring(graph)
		empties = [d for d in root.iter("additional_data_streams") if not len(d)]
		assert empties, "the events-free state should still emit the empty tag"

	def test_events_are_optional(self):
		# a 4-tuple choice (no events member) stays valid
		graph, _ = generate("MyProp", VALID, "MyPropVars", 0.15)
		assert "<datastreamresourcedata" not in graph


class TestEventsAtClipBoundary:
	"""at=0.0 and at=1.0 are in-range (parse_events accepts [0.0, 1.0]
	inclusive) and reachable from ordinary authoring: a Blender pose marker
	placed on a clip's first or last frame yields exactly this fraction
	(marker_fraction in modules/animspec_rows.py).

	The curve is a STEP (see TestEvents), not a linear ramp, so a point
	sharing an x with its neighbour is not an ambiguous overlap: value holds
	from each point's x onward, and retail's own staircase-trigger curves
	(.scratch/motiongraph-format/issues/14-curve-y-encoding.md - ws_cowboy$tiphat,
	8 risers, "the final point just holds") use exactly this encoding for a
	trigger at a boundary sample. These tests pin that reading so it cannot
	silently change.
	"""

	def _curve(self, at):
		events = ({"name": "VFX_Boundary", "type": "VFXEnable", "at": at,
				   "location": ""},)
		choices = [AUTO, ("Static", "ClipA", 1, "Rest", ()),
				   ("Idle01", "ClipB", 3, "Play", events)]
		graph = generate("MyProp", choices, "MyPropVars", 0.15)[0]
		root = ET.fromstring(graph)
		d = next(root.iter("datastreamresourcedata"))
		return list(d.iter("curvedatapoint"))

	def test_at_zero_fires_from_the_start(self):
		pts = self._curve(0.0)
		xs = [float(p.get("x")) for p in pts]
		ys = [p.get("y") for p in pts]
		# the OFF point and the trigger point share x=0.0 - a zero-width dead
		# point, not a conflict, since the step evaluates to the LAST point at
		# any given x (retail's own staircases rely on the same read)
		assert xs == [0.0, 0.0, 1.0]
		assert ys == ["16384", "16448", "16448"]

	def test_at_one_fires_only_at_the_last_sample(self):
		pts = self._curve(1.0)
		xs = [float(p.get("x")) for p in pts]
		ys = [p.get("y") for p in pts]
		assert xs == [0.0, 1.0, 1.0]
		assert ys == ["16384", "16448", "16448"]


class TestGenerateRefusals:

	def refused(self, *args, **kwargs):
		with pytest.raises(ValueError):
			generate(*args, **kwargs)

	def test_unverified_game(self):
		self.refused("MyProp", VALID, "MyPropVars", 0.15, game="Planet Zoo")

	def test_choice_zero_must_be_auto(self):
		self.refused("MyProp", [("Static", "ClipA", 1, "a")], "MyPropVars", 0.15)

	def test_all_weights_null(self):
		self.refused("MyProp", [AUTO, ("A", "ClipX", None, "a")],
					 "MyPropVars", 0.15)

	def test_clipless_non_auto_choice(self):
		self.refused("MyProp", [AUTO, ("A", None, 1, "a")], "MyPropVars", 0.15)

	def test_hostile_asset_name(self):
		self.refused('P"><x', [AUTO, ("A", "ClipA", 1, "a")], "MyPropVars", 0.15)

	def test_hostile_vars_ref(self):
		self.refused("MyProp", [AUTO, ("A", "ClipA", 1, "a")], "V&<", 0.15)

	def test_hostile_clip_name(self):
		self.refused("MyProp", [AUTO, ("A", "Clip<A", 1, "a")], "MyPropVars", 0.15)

	def test_empty_choices(self):
		self.refused("MyProp", [], "MyPropVars", 0.15)

	def test_the_auto_choice_alone_has_nothing_to_pick_from(self):
		# a dropdown whose only entry picks at random between no clips
		self.refused("MyProp", [AUTO], "MyPropVars", 0.15)


class TestIds:
	"""The id allocator underpins every ref in the graph."""

	def test_first_mention_defines_and_numbers_from_one(self):
		ids = Ids()
		assert ids.define("state") == 'id="1"'
		assert ids.define("other") == 'id="2"'

	def test_a_key_defined_twice_is_refused(self):
		# a raise rather than an assert on purpose: two blocks under one id
		# corrupt every ref to it, and asserts vanish under -O
		ids = Ids()
		ids.define("state")
		with pytest.raises(ValueError, match="defined twice"):
			ids.define("state")
