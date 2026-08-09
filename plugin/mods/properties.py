import uuid

import bpy
from bpy.props import (StringProperty, EnumProperty, IntProperty, FloatProperty,
					   BoolProperty, PointerProperty, CollectionProperty)
from bpy.types import PropertyGroup


def update_uuid(self, context):
	if self.uuid == "":
		self.uuid = str(uuid.uuid4())
	return


class ModData(PropertyGroup):
	"""Stores enough information to create a mod from this blender file"""
	name: StringProperty(name="Name", description='This is the human-readable name of the mod.')
	desc: StringProperty(name="Description", description="Mod's description is used to create the readme file.")
	uuid: StringProperty(name="UUID", description="Mod's uuid. Delete to generate a new one", update=update_uuid, )
	path: StringProperty(name="Mod Path", description="A folder will be created in this path for the mod files",
						 default="", maxlen=1024, subtype="DIR_PATH")
	pack: StringProperty(name="Pack into",
						 description="Create the ovl files for this mod into this folder, usually one level deep from the ovldata folder",
						 default="", maxlen=1024, subtype="DIR_PATH")


def clip_action_poll(self, action):
	"""Restrict the Action picker to Actions that will actually become clips.

	Unfiltered, a PointerProperty to Action lists every Action in the .blend -
	including ones stashed on a different armature and orphans kept alive by a
	fake user. Picking one of those authors a dropdown entry for a clip the manis
	will never contain, and nothing objects until the build fails with "clip X is
	not in the asset, run the art export first" - which blames the art pipeline
	for what was really a mis-click in this list.

	get_actions is the export side's own definition - the active action plus
	every NLA strip - so filtering by it makes the picker and the manis the same
	set by construction. Imported lazily: this runs at draw time, long after
	registration, and importing it at module level would pull numpy in earlier
	than it needs to be.
	"""
	from plugin.modules_export.animation import get_actions
	ob = self.id_data
	if not isinstance(ob, bpy.types.Object):
		return True
	return action in get_actions(ob)


_EVENT_KIND_ITEMS = (
	("audio", "Audio", "A Wwise event fires once, at one point in the clip"),
	("vfx", "VFX (enable + disable)",
	 "A particle effect switches on at one point in the clip, optionally off "
	 "at another"),
	("vfx_off", "VFX (disable only)",
	 "Switches off a particle effect armed by a 'VFX' event on another clip "
	 "- e.g. a rest-clip kill switch for an effect that outlives its clip"),
)

def effects_game_candidates(context):
	"""(scene's own game, "Planet Coaster 2"), scene's game first, both only
	if truthy and not already tried - the order every catalogue lookup below
	tries games in.

	CobraSceneSettings.game (plugin/utils/properties.py) declares no explicit
	`default`, so bpy defaults a fresh scene to the FIRST entry of the
	generated `games` enum - never empty/falsy, and not guaranteed to be
	Planet Coaster 2. Falling back to PC2 only when the scene's value is
	falsy therefore missed the common case (a scene that simply never had
	its game set), silently starving every catalogue lookup below. Effects
	authoring is PC2-only today regardless of this scene-wide setting (see
	generate()'s own game refusal), so trying PC2 whenever the scene's own
	choice has no catalogue is correct, not a guess.
	"""
	game = getattr(getattr(context.scene, "cobra", None), "game", None)
	seen = set()
	for candidate in (game, "Planet Coaster 2"):
		if candidate and candidate not in seen:
			seen.add(candidate)
			yield candidate


class SearchNameItem(PropertyGroup):
	"""One entry in a prop_search source list - see ensure_audio_names and
	ensure_child_names, its two uses. `name` is the only field prop_search
	reads; it is registered once (WindowManager.cobra_audio_names,
	.cobra_child_names) and reused as the generic "one named string" holder
	rather than declaring a near-identical PropertyGroup per list."""
	name: StringProperty()


_audio_names_populated = set()
_particle_names_populated = set()


def ensure_audio_names(context):
	"""Populate context.window_manager.cobra_audio_names once per game, for
	the Effects panel's audio event field to prop_search against.

	UNLIKE particles, this is NOT exhaustive: Wwise banks store FNV-1 hashes
	of the lowercased name with no strings at all, so only the subset already
	identified by name (constants/<game>/audio.py) can be offered here. A
	real event outside that subset still has to be typed by hand, which is
	why `event`'s prop_search call passes results_are_suggestions=True - see
	the panel. The hash-based check at build time (parse_events) is what
	actually catches a typo either way, regardless of whether the name came
	from this list.
	"""
	wm = context.window_manager
	from importlib import import_module
	for candidate in effects_game_candidates(context):
		if candidate in _audio_names_populated:
			return
		try:
			names = sorted(set(import_module(f"constants.{candidate}.audio").audio.values()))
		except ModuleNotFoundError:
			continue
		wm.cobra_audio_names.clear()
		for n in names:
			item = wm.cobra_audio_names.add()
			item.name = n
		_audio_names_populated.add(candidate)
		return


def ensure_particle_names(context):
	"""Populate context.window_manager.cobra_particle_names once per game,
	for the Effects panel's `particle` field to prop_search against.

	prop_search, not the EnumProperty this used to be: an EnumProperty
	popup is a plain scrollable list with no type-to-filter, useless for
	finding one name among 972. prop_search's popup has a real search box.
	Left at prop_search's default results_are_suggestions=False (UNLIKE
	`event`/`child` below) on purpose: particles ARE exhaustive (Content0
	is the game's only particle carrier), so constraining input to this
	list costs nothing and keeps the same guarantee the old EnumProperty
	gave - a typo cannot silently become a value at all, not even one
	parse_events would later reject.
	"""
	wm = context.window_manager
	from importlib import import_module
	for candidate in effects_game_candidates(context):
		if candidate in _particle_names_populated:
			return
		try:
			names = sorted(import_module(f"constants.{candidate}.particles").particles.values())
		except ModuleNotFoundError:
			continue
		wm.cobra_particle_names.clear()
		for n in names:
			item = wm.cobra_particle_names.add()
			item.name = n
		_particle_names_populated.add(candidate)
		return


def ensure_child_names(context):
	"""Populate context.window_manager.cobra_child_names for the Effects
	panel's `child` field to prop_search against - rebuilt every call, unlike
	the once-per-game particle/audio catalogues, because this source is this
	OBJECT'S OWN live data, not static game data: it changes as the artist
	types. Cheap enough (a handful of events per object) to just redo.

	There is no game catalogue for `child` to check against at all - it is a
	name the artist invents for their own prefab, not shipped data - so this
	is not a typo-catching picker the way particle/audio are. What it does
	catch: a `vfx` event's enable and a `vfx_off` event's disable on a
	DIFFERENT clip must name the exact same child to be the same kill
	switch, and retyping that name by hand is exactly how the two silently
	drift (see AnimEventItem's `vfx_off` doc). Offering every child name
	already used anywhere on this object makes reusing the same string the
	path of least resistance instead of retyping it.
	"""
	wm = context.window_manager
	wm.cobra_child_names.clear()
	scenery = getattr(context.object, "scenery", None)
	if scenery is None:
		return
	seen = set()
	for choice in scenery.anim_choices:
		for ev in choice.events:
			if ev.child and ev.child not in seen:
				seen.add(ev.child)
	for n in sorted(seen):
		item = wm.cobra_child_names.add()
		item.name = n


class AnimEventItem(PropertyGroup):
	"""One audio/VFX event on one animation choice's clip.

	The fields (kind/child/event/particle/location) are the same authoring
	model a hand-written effect list uses, in Blender instead of Python.

	WHEN an event fires is deliberately NOT a field here. It comes from
	Action Pose Markers named "fx.<event index>.on" (required) and
	"fx.<event index>.off" (kind="vfx" only, optional) on the choice's own
	Action at export time - see modules/animspec_rows.py:events_from_choice.
	Blender's timeline is already scrubbed and synced to the Action; a
	second, disconnected numeric field here would only invite it to
	disagree with what the artist actually placed.
	"""
	kind: EnumProperty(name="Kind", items=_EVENT_KIND_ITEMS, default="audio")
	child: StringProperty(
		name="Prefab child",
		description="Name of the prefab child entity this VFX enables/disables. "
					"IS the graph's ds_name, so it is not free - it must match "
					"the prefab lua exactly")
	event: StringProperty(
		name="Wwise event",
		description="Audio event name. Cannot be looked up from Blender - Wwise "
					"banks store hashes, not strings - but a typo IS caught "
					"offline at build time against the measured event-bank hashes")
	particle: StringProperty(
		name="Particle",
		description="The .particleeffect this child plays. Informational only - "
					"it never reaches the graph - but build()'s report uses it "
					"to tell you which particle to assign to which prefab child. "
					"Searched, not typed freely - see ensure_particle_names")
	location: StringProperty(
		name="Emitter location",
		description="Audio only. A locator name plus a distance suffix (Default/"
					"Close/Far) that must match the child's AudioDataStream.Name. "
					"Blank defaults to 'Default'")


class AnimChoiceItem(PropertyGroup):
	"""One entry in the in-game animation dropdown of an animated scenery prop.

	Everything that CAN be derived is derived, so there is one source of truth:
	the clip name and its length come from the Action, not from typed-in values.
	Only the player-facing label and the loop flag are authored here.
	"""
	action: PointerProperty(
		name="Clip",
		description="Action to play for this entry. Name it for the CLIP alone "
					"(\"MyClip\"), not \"Asset$MyClip\" - the build reads the asset "
					"prefix from the target and generates the qualified name and "
					"the localisation symbol, so they cannot disagree with it. "
					"Only Actions on this object are offered, which are exactly "
					"the ones exported into the .manis",
		type=bpy.types.Action,
		poll=clip_action_poll)
	label: StringProperty(
		name="Label",
		description="Text shown in the game's animation dropdown",
		default="New animation")
	loop: BoolProperty(
		name="Loop",
		description="Loop forever (animation_flags 17). Off plays once and holds the "
					"final pose (16) - needed for 'turn and stay turned'",
		default=True)
	in_random_pool: BoolProperty(
		name="In random pool",
		description="Off excludes this entry from the Auto choice's random "
					"selection entirely (weight: null in the spec), rather than "
					"just giving it a low chance",
		default=True)
	weight: FloatProperty(
		name="Weight",
		description="Relative chance of the Auto choice picking this entry, "
					"among entries that are In random pool. Only meaningful for "
					"a from-scratch build (buildprop) - the append path ignores it",
		default=1.0, min=0.0001)
	# One events list per CHOICE, not global: the JSON schema is clips[i].events[j],
	# and each event's timing comes from pose markers on THIS row's own Action -
	# nesting here mirrors both directly instead of needing a second index to
	# correlate rows across two separate top-level lists
	events: CollectionProperty(type=AnimEventItem)
	events_index: IntProperty(name="Selected event", default=0)

	@property
	def frame_count(self):
		"""Exactly what export_manis writes, INCLUSIVE of the last frame.

		export_manis.py:139-141 is

			first_frame = int(first_frame)
			last_frame  = int(last_frame) + 1
			mani_info.frame_count = last_frame - first_frame

		so the `+ 1` matters. Omitting it made this one frame short on every clip,
		and because duration is derived from frame_count the error propagated: a
		149-frame clip reported 6.125s while the built .manis said 6.1667s, so the
		build's drift guard rejected every GUI-exported spec with SPEC/ASSET
		MISMATCH. Found by walking the UI journey end to end; a hand-written spec
		does not catch it, because the bug is in what the UI derives.
		"""
		if not self.action:
			return 0
		first, last = self.action.frame_range
		return int(last) + 1 - int(first)

	def duration(self, fps):
		"""Seconds, identical to plugin/export_manis.py:143.

		Authoring this by hand duplicates a number the exporter already derives,
		and the two WERE drifting - a 90-frame clip at 24 fps was hand-written as
		3.75 (90/24) while the exporter stores 3.708 (89/24).
		"""
		fc = self.frame_count
		return (fc - 1) / fps if fc > 1 else 0.0


class SceneryData(PropertyGroup):
	"""Stores enough information to create a mod from this blender file"""
	name: StringProperty(name="Name", description='This is the human-readable name of the asset.')
	desc: StringProperty(name="Description", description="Asset long description.")
	price: FloatProperty(name="Price", min=0)
	cost: FloatProperty(name="Running cost", min=0)
	# APPEND-ONLY. Placed props store the choice INDEX, so inserting, reordering
	# or deleting a shipped entry silently repoints every instance already placed
	# in a park. There are deliberately no move up/down operators
	anim_choices: CollectionProperty(type=AnimChoiceItem)
	anim_choices_index: IntProperty(name="Selected animation choice", default=0)
	# There is deliberately NO loc-prefix property. The symbol is generated by the
	# build from the target asset's clip prefix (motiongraph_author.loc_symbol),
	# because only the target knows that prefix: when appending, it must match the
	# clips already in the asset, and a wrong one yields a dropdown entry that
	# resolves to nothing and plays silently
	#
	# The field this replaces was also actively harmful: it defaulted to
	# "InfoPanel_AnimationType_" with no asset segment, so accepting the default
	# produced "InfoPanel_AnimationType_MyClip" - a symbol any other mod with a
	# MyClip clip would also claim, colliding in the game's loc namespace
