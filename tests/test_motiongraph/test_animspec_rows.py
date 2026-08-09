"""The bpy-free half of the Blender authoring panel.

Split out of plugin/mods/operators.py specifically so it could be tested at
all - everything here runs on duck-typed stand-ins for Action/pose marker/UI
row, with no bpy import anywhere. The marker bookkeeping is the only real
algorithm the authoring UI has, and its failures are silent ones: a marker
left under its old index does not crash, it just makes an event fire on the
wrong frame or the wrong animation entirely.
"""
import pytest

from modules.animspec_rows import (events_from_choice, marker_fraction,
								   renumber_markers_after_removal, specs_from_scenery)


class Marker:

	def __init__(self, name, frame):
		self.name = name
		self.frame = frame


class PoseMarkers:
	"""The subset of bpy's PoseMarkers this module actually touches.

	Backed by a LIST, not a dict keyed at construction time: renumber_markers_
	after_removal renames a marker by setting .name directly, exactly like
	Blender's own collection, where a marker's name IS its live lookup key -
	a dict snapshotting names up front would go stale the moment a rename
	happens and silently look up nothing.
	"""

	def __init__(self, markers=()):
		self._markers = list(markers)

	def get(self, name):
		for m in self._markers:
			if m.name == name:
				return m
		return None

	def remove(self, marker):
		self._markers.remove(marker)

	def names(self):
		return {m.name for m in self._markers}


class Action:

	def __init__(self, name="MyClip", frame_range=(1, 25), markers=()):
		self.name = name
		self.frame_range = frame_range
		self.pose_markers = PoseMarkers(markers)


class Event:

	def __init__(self, kind="audio", event="", child="", particle="", location=""):
		self.kind = kind
		self.event = event
		self.child = child
		self.particle = particle
		self.location = location


class Choice:

	def __init__(self, action=None, events=(), label="Label", frame_count=10,
				loop=True, weight=1, in_random_pool=True, dur=2.5):
		self.action = action
		self.events = list(events)
		self.label = label
		self.frame_count = frame_count
		self.loop = loop
		self.weight = weight
		self.in_random_pool = in_random_pool
		self._dur = dur

	def duration(self, fps):
		return self._dur


class Scenery:

	def __init__(self, choices=()):
		self.anim_choices = list(choices)


class TestRenumberMarkersAfterRemoval:

	def test_none_action_is_a_no_op(self):
		# a choice with no Action assigned yet - nothing to renumber
		renumber_markers_after_removal(None, 0, 3)

	def test_the_removed_events_own_markers_are_deleted(self):
		action = Action(markers=[Marker("fx.1.on", 5), Marker("fx.1.off", 8)])
		renumber_markers_after_removal(action, 1, 2)
		assert action.pose_markers.names() == set()

	def test_later_markers_shift_down_by_one(self):
		action = Action(markers=[Marker("fx.0.on", 1), Marker("fx.1.on", 5),
								 Marker("fx.1.off", 8), Marker("fx.2.on", 12)])
		renumber_markers_after_removal(action, 0, 3)
		assert action.pose_markers.names() == {"fx.0.on", "fx.0.off", "fx.1.on"}
		assert action.pose_markers.get("fx.0.on").frame == 5
		assert action.pose_markers.get("fx.0.off").frame == 8
		assert action.pose_markers.get("fx.1.on").frame == 12

	def test_earlier_markers_are_untouched(self):
		action = Action(markers=[Marker("fx.0.on", 1), Marker("fx.2.on", 20)])
		renumber_markers_after_removal(action, 2, 3)
		assert action.pose_markers.get("fx.0.on").frame == 1

	def test_removing_the_last_event_only_deletes_nothing_renumbers(self):
		action = Action(markers=[Marker("fx.0.on", 1), Marker("fx.1.on", 5)])
		renumber_markers_after_removal(action, 1, 2)
		assert action.pose_markers.names() == {"fx.0.on"}

	def test_a_missing_off_marker_is_not_an_error(self):
		# vfx_off/audio events never have an 'off' marker at all
		action = Action(markers=[Marker("fx.0.on", 1), Marker("fx.1.on", 5)])
		renumber_markers_after_removal(action, 0, 2)
		assert action.pose_markers.names() == {"fx.0.on"}
		assert action.pose_markers.get("fx.0.on").frame == 5

	def test_removing_from_an_empty_timeline_does_not_raise(self):
		renumber_markers_after_removal(Action(), 0, 1)


class TestMarkerFraction:

	def test_absent_marker_is_none(self):
		assert marker_fraction(Action(), "fx.0.on") is None

	def test_marker_on_the_first_frame_is_zero(self):
		action = Action(frame_range=(1, 25), markers=[Marker("fx.0.on", 1)])
		assert marker_fraction(action, "fx.0.on") == 0.0

	def test_marker_on_the_last_frame_is_one(self):
		action = Action(frame_range=(1, 25), markers=[Marker("fx.0.on", 25)])
		assert marker_fraction(action, "fx.0.on") == 1.0

	def test_marker_at_the_midpoint(self):
		action = Action(frame_range=(0, 20), markers=[Marker("fx.0.on", 5)])
		assert marker_fraction(action, "fx.0.on") == 0.25

	def test_a_zero_length_frame_range_is_refused(self):
		action = Action(name="Weird", frame_range=(10, 10),
						markers=[Marker("fx.0.on", 10)])
		with pytest.raises(ValueError, match="zero-length frame range"):
			marker_fraction(action, "fx.0.on")


def audio_event(**over):
	row = {"kind": "audio", "event": "Wwise_Foo", "location": ""}
	row.update(over)
	return Event(**row)


def vfx_event(**over):
	row = {"kind": "vfx", "child": "Sparks", "particle": "PT_Sparks"}
	row.update(over)
	return Event(**row)


class TestEventsFromChoice:

	def test_an_audio_event(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[audio_event()])
		[row] = events_from_choice(item)
		assert row == {"name": "Wwise_Foo", "type": "AudioEvent", "at": 0.25,
					   "location": "Default"}

	def test_an_audio_event_keeps_its_own_location(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[audio_event(location="Close")])
		[row] = events_from_choice(item)
		assert row["location"] == "Close"

	def test_a_vfx_off_event(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[vfx_event(kind="vfx_off")])
		[row] = events_from_choice(item)
		assert row == {"name": "Sparks", "type": "VFXDisable", "at": 0.25}

	def test_a_vfx_event_with_only_an_on_marker(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[vfx_event()])
		[row] = events_from_choice(item)
		assert row == {"name": "Sparks", "type": "VFXEnable", "at": 0.25,
					   "particle": "PT_Sparks"}

	def test_a_vfx_event_with_both_markers_produces_enable_then_disable(self):
		action = Action(markers=[Marker("fx.0.on", 1), Marker("fx.0.off", 3)],
						frame_range=(0, 4))
		item = Choice(action=action, events=[vfx_event()])
		rows = events_from_choice(item)
		assert [r["type"] for r in rows] == ["VFXEnable", "VFXDisable"]
		assert rows[1] == {"name": "Sparks", "type": "VFXDisable", "at": 0.75}

	def test_a_missing_on_marker_is_refused(self):
		item = Choice(action=Action(), events=[audio_event()])
		with pytest.raises(ValueError, match="fx.0.on"):
			events_from_choice(item)

	def test_an_audio_event_needs_a_wwise_name(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[audio_event(event="")])
		with pytest.raises(ValueError, match="Wwise event name"):
			events_from_choice(item)

	def test_a_vfx_event_needs_a_child(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[vfx_event(child="")])
		with pytest.raises(ValueError, match="prefab child name"):
			events_from_choice(item)

	def test_a_vfx_off_event_needs_a_child_too(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[vfx_event(kind="vfx_off", child="")])
		with pytest.raises(ValueError, match="prefab child name"):
			events_from_choice(item)

	def test_a_vfx_event_needs_a_particle(self):
		action = Action(markers=[Marker("fx.0.on", 1)], frame_range=(0, 4))
		item = Choice(action=action, events=[vfx_event(particle="")])
		with pytest.raises(ValueError, match="particle"):
			events_from_choice(item)

	def test_events_are_looked_up_by_their_own_index_not_position(self):
		# fx.<i> is the event's INDEX in the list, so two events need two
		# distinct markers, not the same one reused
		action = Action(frame_range=(0, 4),
						markers=[Marker("fx.0.on", 0), Marker("fx.1.on", 4)])
		item = Choice(action=action, events=[audio_event(event="A"),
											 audio_event(event="B")])
		rows = events_from_choice(item)
		assert [r["at"] for r in rows] == [0.0, 1.0]

	def test_no_events_is_an_empty_list(self):
		assert events_from_choice(Choice(action=Action(), events=[])) == []


class TestSpecsFromScenery:

	def test_a_normal_row(self):
		action = Action(name="Idle", markers=[Marker("fx.0.on", 1)],
						frame_range=(0, 4))
		item = Choice(action=action, events=[audio_event()], label="Rest",
					 frame_count=10, loop=True, weight=3, in_random_pool=True,
					 dur=1.5)
		[spec] = specs_from_scenery(Scenery([item]), fps=30)
		assert spec.clip == "Idle"
		assert spec.enum_name == "Idle"
		assert spec.label_symbol is None
		assert spec.label_text == "Rest"
		assert spec.duration == 1.5
		assert spec.loop is True
		assert spec.weight == 3
		assert len(spec.events) == 1

	def test_weight_is_none_when_opted_out_of_the_random_pool(self):
		item = Choice(action=Action(), events=[], weight=5, in_random_pool=False)
		[spec] = specs_from_scenery(Scenery([item]), fps=30)
		assert spec.weight is None

	def test_the_duration_call_receives_the_given_fps(self):
		calls = []

		class RecordingChoice(Choice):
			def duration(self, fps):
				calls.append(fps)
				return 2.0

		item = RecordingChoice(action=Action(), events=[])
		specs_from_scenery(Scenery([item]), fps=24)
		assert calls == [24]

	def test_a_row_with_no_action_is_refused(self):
		item = Choice(action=None, events=[], label="Broken")
		with pytest.raises(ValueError, match="entry 0 \\('Broken'\\) has no Action"):
			specs_from_scenery(Scenery([item]), fps=30)

	def test_a_row_with_too_few_frames_is_refused(self):
		item = Choice(action=Action(), events=[], frame_count=1)
		with pytest.raises(ValueError, match="at least 2"):
			specs_from_scenery(Scenery([item]), fps=30)

	def test_the_same_action_twice_is_refused(self):
		a = Action(name="Shared")
		items = [Choice(action=a, events=[]), Choice(action=a, events=[])]
		with pytest.raises(ValueError, match="entry 0"):
			specs_from_scenery(Scenery(items), fps=30)

	def test_two_different_actions_are_both_kept(self):
		items = [Choice(action=Action(name="A"), events=[]),
				Choice(action=Action(name="B"), events=[])]
		specs = specs_from_scenery(Scenery(items), fps=30)
		assert [s.clip for s in specs] == ["A", "B"]

	def test_no_entries_is_an_empty_list(self):
		assert specs_from_scenery(Scenery([]), fps=30) == []

	def test_an_events_error_propagates_with_the_row_still_identifiable(self):
		# events_from_choice's own errors are not swallowed or reworded here
		item = Choice(action=Action(), events=[audio_event()])
		with pytest.raises(ValueError, match="fx.0.on"):
			specs_from_scenery(Scenery([item]), fps=30)
