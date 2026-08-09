"""Operators for editing an animated scenery prop's animation dropdown.

APPEND-ONLY BY DESIGN. A placed prop stores the choice INDEX, not its name, so
inserting, reordering or deleting an entry that has already shipped silently
repoints every instance already placed in a park. There is deliberately no
"move up"/"move down" here, and removal is restricted to the last entry.
"""
import bpy
from bpy.types import Operator

from modules.animspec_rows import renumber_markers_after_removal


class COBRA_OT_anim_choice_add(Operator):
	"""Append an animation choice. New entries can only be added at the end"""
	bl_idname = "cobra.anim_choice_add"
	bl_label = "Add Animation Choice"
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		return context.object is not None

	def execute(self, context):
		scenery = context.object.scenery
		item = scenery.anim_choices.add()
		item.label = f"Animation {len(scenery.anim_choices)}"
		# select the entry we just made so the user edits the right one
		scenery.anim_choices_index = len(scenery.anim_choices) - 1
		return {"FINISHED"}


class COBRA_OT_anim_choice_remove(Operator):
	"""Remove the LAST animation choice.

	Only the last one, and only ever from the end - see the module docstring.
	"""
	bl_idname = "cobra.anim_choice_remove"
	bl_label = "Remove Last Animation Choice"
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		return context.object is not None and len(context.object.scenery.anim_choices) > 0

	def execute(self, context):
		scenery = context.object.scenery
		last = len(scenery.anim_choices) - 1
		if scenery.anim_choices_index != last:
			self.report({"WARNING"},
						"Only the last entry can be removed - indices are frozen once "
						"a prop is placed in a park")
			return {"CANCELLED"}
		scenery.anim_choices.remove(last)
		scenery.anim_choices_index = max(0, last - 1)
		return {"FINISHED"}


def _active_choice(context):
	scenery = context.object.scenery
	if not scenery.anim_choices:
		return None
	return scenery.anim_choices[scenery.anim_choices_index]


class COBRA_OT_anim_event_add(Operator):
	"""Add an audio/VFX event to the SELECTED animation choice.

	Unlike anim_choices, events are not append-only - nothing stores an event
	index anywhere a placed prop would see, so free add/remove/reorder is fine.
	"""
	bl_idname = "cobra.anim_event_add"
	bl_label = "Add Event"
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		return context.object is not None and _active_choice(context) is not None

	def execute(self, context):
		choice = _active_choice(context)
		choice.events.add()
		choice.events_index = len(choice.events) - 1
		return {"FINISHED"}


class COBRA_OT_anim_event_remove(Operator):
	"""Remove the selected event from the selected animation choice."""
	bl_idname = "cobra.anim_event_remove"
	bl_label = "Remove Event"
	bl_options = {"REGISTER", "UNDO"}

	@classmethod
	def poll(cls, context):
		choice = _active_choice(context) if context.object is not None else None
		return choice is not None and len(choice.events) > 0

	def execute(self, context):
		choice = _active_choice(context)
		idx = choice.events_index
		renumber_markers_after_removal(choice.action, idx, len(choice.events))
		choice.events.remove(idx)
		choice.events_index = max(0, choice.events_index - 1)
		return {"FINISHED"}
