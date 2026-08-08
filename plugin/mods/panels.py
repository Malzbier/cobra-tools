import bpy

from plugin.mods.properties import ensure_audio_names, ensure_child_names, ensure_particle_names
from plugin.utils.panels import PropertiesPanel


class COBRA_MOD_PT_mod(PropertiesPanel):
	"""Creates a Panel in the Collection properties window for mod attributes"""
	bl_label = "Cobra Mod information"
	bl_context = "collection"

	def draw(self, context):
		mod = context.collection.mod
		self.layout.prop(mod, "name")
		self.layout.prop(mod, "desc")
		self.layout.prop(mod, "uuid")
		self.layout.prop(mod, "path")
		# todo - re-add operators
		# self.layout.operator("cobra.export_mod")
		self.layout.prop(mod, "pack")
		# self.layout.operator("cobra.pack_mod")


class COBRA_UL_anim_choice(bpy.types.UIList):
	"""One row per entry of an animated prop's in-game animation dropdown.

	Frame count and duration are shown READ-ONLY because they are derived from
	the Action. That is the point: the choice table can then never disagree with
	the clip it points at, which it previously did - durations were typed by hand
	and every one of them was wrong.
	"""

	def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
		if self.layout_type in {'GRID'}:
			layout.alignment = 'CENTER'
			layout.label(text=str(index))
			return
		# TWO lines per entry, deliberately. On one line at default panel width
		# Blender truncates both the Action name and the label to a few
		# characters, and the label is prose that needs the room
		col = layout.column(align=True)

		top = col.split(factor=0.07)
		top.label(text=str(index))
		top_rest = top.split(factor=0.74)
		picker = top_rest.row(align=True)
		# NO alert on a missing '$'. This used to flag any Action whose name had
		# no asset qualifier, which is now exactly the CORRECT form: Actions are
		# named for the clip alone ("MyClip") and the build generates
		# "<prefix>$MyClip" from the target asset. A qualifier here is not an
		# error either - parse_spec discards it - so there is nothing to warn
		# about in either direction
		picker.prop(item, "action", text="", icon='ACTION')
		meta = top_rest.row(align=True)
		if item.action:
			fps = context.scene.render.fps
			meta.label(text=f"{item.frame_count}f {item.duration(fps):.2f}s")
		else:
			meta.label(text="no clip")
		meta.prop(item, "loop", text="", icon='FILE_REFRESH')

		bottom = col.split(factor=0.07)
		bottom.label(text="")
		bottom.prop(item, "label", text="", icon='FONT_DATA')


class COBRA_UL_anim_events(bpy.types.UIList):
	"""One row per audio/VFX event on the SELECTED animation choice.

	WHEN is deliberately not drawn here - it lives on the choice's Action's
	own pose markers, named for this row's index (see AnimEventItem's
	docstring). This list only edits WHAT fires and, for VFX, WHERE from.
	"""

	def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
		if self.layout_type in {'GRID'}:
			layout.alignment = 'CENTER'
			layout.label(text=str(index))
			return
		col = layout.column(align=True)
		top = col.split(factor=0.07)
		top.label(text=str(index))
		top.prop(item, "kind", text="")

		bottom = col.split(factor=0.07)
		bottom.label(text="")
		rest = bottom.row(align=True)
		if item.kind == "audio":
			# prop_search, not a plain text field: it offers autocomplete
			# against the known-name subset (constants/<game>/audio.py) but,
			# unlike particle's EnumProperty, does NOT restrict input to
			# it - Wwise banks store hashes with no strings, so a real event
			# absent from that subset must stay typeable. ensure_audio_names
			# populates the search source once, lazily, cached like particles
			#
			# results_are_suggestions=True is NOT optional here. Blender's
			# default (False) makes prop_search a SEARCH-SELECT: typing a
			# name absent from the list is silently reverted to blank on
			# confirm, exactly like the EnumProperty this was deliberately
			# built to avoid - measured by typing an event outside the known
			# list and watching it vanish on Enter with the default
			ensure_audio_names(context)
			rest.prop_search(item, "event", context.window_manager,
							 "cobra_audio_names", text="", icon='SPEAKER',
							 results_are_suggestions=True)
			rest.prop(item, "location", text="", icon='EMPTY_AXIS')
		else:
			# Autocomplete against child names already used on THIS object,
			# not a game catalogue - see ensure_child_names. Populated once
			# per panel draw by the parent (COBRA_MOD_PT_scenery_effects),
			# not here: this runs once per VISIBLE ROW, and rebuilding a
			# fresh scan for every row of every redraw would be needless
			# results_are_suggestions=True for the same reason as `event`
			# above - without it, the FIRST use of any child name (nothing
			# to suggest yet) gets silently reverted to blank on confirm
			rest.prop_search(item, "child", context.window_manager,
							 "cobra_child_names", text="", icon='OUTLINER_OB_EMPTY',
							 results_are_suggestions=True)
			if item.kind == "vfx":
				# prop_search, not the plain EnumProperty dropdown this used to
				# be: an enum popup has no type-to-filter, useless for finding
				# one name among 972. Default results_are_suggestions=False on
				# purpose here (unlike event/child above) - particles ARE
				# exhaustive, so keeping input constrained to the list costs
				# nothing and preserves the old EnumProperty's guarantee
				ensure_particle_names(context)
				rest.prop_search(item, "particle", context.window_manager,
								 "cobra_particle_names", text="", icon='PARTICLES')

		marker = f"fx.{index}.on" + (" / .off" if item.kind == "vfx" else "")
		hint = col.row()
		hint.scale_y = 0.7
		hint.label(text=f"timed by pose marker '{marker}'", icon='MARKER_HLT')


class COBRA_MOD_PT_scenery(PropertiesPanel):
	"""Creates a Panel in the Object properties window for Scenery asset attributes"""
	bl_label = "Cobra Asset information"
	bl_context = "object"
	# Closed by default: this is PC2 animated-scenery authoring specifically,
	# not something every cobra-tools user touches, so it should not dominate
	# the Object properties tab for anyone working on a different asset type
	bl_options = {"DEFAULT_CLOSED"}

	def draw(self, context):
		scenery = context.object.scenery
		self.layout.prop(scenery, "name")
		self.layout.prop(scenery, "desc")

		row = self.layout.row()
		row.label(text="Gameplay", icon='WORLD_DATA')
		row.prop(scenery, "price")
		row.prop(scenery, "cost")

		# todo - re-add operator
		# self.layout.operator("cobra.generate_icon")


class COBRA_MOD_PT_scenery_animation(PropertiesPanel):
	"""The in-game animation dropdown: one row per selectable choice.

	A real sub-panel, matching Effects below it - both fold independently
	of the parent and of each other, rather than always drawing in full the
	moment "Cobra Asset information" itself is open.
	"""
	bl_label = "Animation Dropdown"
	bl_context = "object"
	bl_parent_id = "COBRA_MOD_PT_scenery"
	bl_options = {"DEFAULT_CLOSED"}

	def draw(self, context):
		scenery = context.object.scenery
		box = self.layout
		# The Action picker is filtered to this object's own Actions, so on an
		# object with none it looks broken rather than empty. Say why
		from plugin.modules_export.animation import get_actions
		if not get_actions(context.object):
			box.label(text="No Actions on this object - stash clips in NLA tracks",
					  icon='INFO')
		# rows counts UIList lines, and each entry draws two - so 6 shows 3 entries
		box.template_list("COBRA_UL_anim_choice", "", scenery, "anim_choices",
						  scenery, "anim_choices_index", rows=6)
		row = box.row(align=True)
		row.operator("cobra.anim_choice_add", icon='ADD')
		row.operator("cobra.anim_choice_remove", icon='REMOVE')
		# No loc-prefix field: the build generates the symbol from the target
		# asset's prefix. See SceneryData for why authoring it here was wrong
		#
		# Say so in the panel rather than leaving it implicit. Removing the field
		# without explaining where the names now come from just moves the mystery:
		# the artist sees "MyClip" here and "MyProp$MyClip" in game with nothing
		# connecting the two
		note = box.column(align=True)
		note.scale_y = 0.8
		note.label(text="Clip and loc names are generated at build time", icon='INFO')
		note.label(text="from the target asset, e.g. MyClip → <Asset>$MyClip")
		# No export button and no path field here on purpose. Writing the spec is a
		# normal export: File > Export > Cobra Animation Spec (.json), alongside
		# Export MS2 / Export Manis, so the file dialog supplies the path. A third
		# place to click was easy to forget, and a stale spec makes the build fail
		# with SPEC/ASSET MISMATCH
		box.label(text="Write via File > Export > Cobra Animation Spec",
				  icon='EXPORT')
		# Not a style choice - a correctness one. A placed prop stores the choice
		# INDEX, so reordering or deleting a shipped entry repoints every instance
		# already placed in a park. Hence append-only, and no move up/down
		box.label(text="Append-only: indices are frozen once a prop is placed",
				  icon='INFO')


class COBRA_MOD_PT_scenery_effects(PropertiesPanel):
	"""Effects, scoped to the SELECTED animation choice above.

	A real sub-panel (bl_parent_id), not another box in the parent's draw -
	its own native foldout header, closed by default same as the parent:
	even someone already doing scenery authoring may not be touching effects
	on a given prop, and a UIList-in-a-UIList reads as a lot of chrome to
	load eagerly.
	"""
	bl_label = "Effects"
	bl_context = "object"
	bl_parent_id = "COBRA_MOD_PT_scenery"
	bl_options = {"DEFAULT_CLOSED"}

	def draw(self, context):
		scenery = context.object.scenery
		if not scenery.anim_choices:
			self.layout.label(text="Add an animation choice above first", icon='INFO')
			return
		# Once per draw, not per row: COBRA_UL_anim_events.draw_item is called
		# once per VISIBLE row, and this scans every event on the object
		ensure_child_names(context)
		choice = scenery.anim_choices[scenery.anim_choices_index]
		target = choice.label or (choice.action.name if choice.action else
								  f"choice {scenery.anim_choices_index}")
		row = self.layout.row(align=True)
		row.label(text=f"For: {target}")
		row.prop(choice, "in_random_pool", text="In random pool")
		wrow = row.row(align=True)
		wrow.enabled = choice.in_random_pool
		wrow.prop(choice, "weight", text="Weight")

		self.layout.template_list("COBRA_UL_anim_events", "", choice, "events",
								  choice, "events_index", rows=5)
		erow = self.layout.row(align=True)
		erow.operator("cobra.anim_event_add", icon='ADD')
		erow.operator("cobra.anim_event_remove", icon='REMOVE')

		note = self.layout.column(align=True)
		note.scale_y = 0.8
		note.label(text="Timing comes from Action Pose Markers, not a typed value",
				  icon='INFO')
		note.label(text="Name them fx.<row>.on / fx.<row>.off on the Action's timeline")
