"""Turn a row-based authoring surface into ClipSpecs.

Split out of the Blender operators deliberately. Nothing here imports bpy: the
rows, Actions and pose markers are duck-typed, so the whole conversion runs -
and can be tested - outside Blender, and the addon stays a thin caller. That
matters because the marker bookkeeping below is the only real algorithm in the
authoring UI, and its failures are silent ones: a marker left under its old
index does not crash, it just makes an event fire on the wrong animation.

The shapes expected of a caller:

    action  .name, .frame_range (first, last), .pose_markers, which supports
            .get(name) -> marker or None, .remove(marker), and markers with a
            writable .name plus a .frame
    event   .kind in {"audio", "vfx", "vfx_off"}, .event, .child, .particle,
            .location
    choice  .action, .events, .label, .frame_count, .duration(fps), .loop,
            .weight, .in_random_pool
"""
from modules.motiongraph_author import ClipSpec


def renumber_markers_after_removal(action, removed_idx, count):
	"""Keep fx.<i> pose markers aligned with event list indices across a removal.

	events_from_choice looks markers up BY INDEX (fx.<i>.on/.off), and removing
	event `removed_idx` shifts every later event's index down by one - without
	this, every later marker would silently point at the wrong event's name
	until the exporter raised a confusing "no fx.<i>.on marker" error naming a
	marker that visibly exists, just under its old number.

	The removed event's own markers no longer mean anything, so they are
	deleted rather than left as orphaned clutter; every later marker is
	renamed down to match its event's new index.
	"""
	if action is None:
		return
	pm = action.pose_markers
	for suffix in ("on", "off"):
		m = pm.get(f"fx.{removed_idx}.{suffix}")
		if m is not None:
			pm.remove(m)
	for i in range(removed_idx + 1, count):
		for suffix in ("on", "off"):
			m = pm.get(f"fx.{i}.{suffix}")
			if m is not None:
				m.name = f"fx.{i - 1}.{suffix}"


def marker_fraction(action, name):
	"""0..1 fraction for pose marker `name` on `action`, or None if absent.

	Matches duration_from_frames' own span convention (the exporter's
	source of truth for clip length), applied to a marker frame instead of
	the frame count: (frame - first) / (last - first), the same 0..1 the
	graph's curve spans end to end.
	"""
	marker = action.pose_markers.get(name)
	if marker is None:
		return None
	first, last = action.frame_range
	span = last - first
	if span <= 0:
		raise ValueError(f"{action.name!r} has a zero-length frame range")
	return (marker.frame - first) / span


def events_from_choice(item):
	"""One choice's AnimEventItem rows -> parse_events' raw input shape.

	The kind -> type mapping is audio / vfx / vfx_off, with an `off` marker
	adding a second VFXDisable, so a graph authored through this panel comes
	out the same shape as one written by hand from the same intent.

	Timing is NOT typed - see AnimEventItem's docstring - so a missing 'on'
	marker is a user error to report, not a value to default.
	"""
	action = item.action
	out = []
	for i, ev in enumerate(item.events):
		on = marker_fraction(action, f"fx.{i}.on")
		if on is None:
			raise ValueError(
				f"{action.name}: event {i} ({ev.kind}) has no 'fx.{i}.on' pose "
				f"marker on its timeline - place one to say when it fires")
		if ev.kind == "audio":
			if not ev.event:
				raise ValueError(f"{action.name}: event {i} needs a Wwise event name")
			out.append({"name": ev.event, "type": "AudioEvent", "at": on,
					   "location": ev.location or "Default"})
		elif ev.kind == "vfx_off":
			if not ev.child:
				raise ValueError(f"{action.name}: event {i} needs a prefab child name")
			out.append({"name": ev.child, "type": "VFXDisable", "at": on})
		else:  # "vfx"
			if not ev.child:
				raise ValueError(f"{action.name}: event {i} needs a prefab child name")
			if not ev.particle:
				raise ValueError(f"{action.name}: event {i} needs a particle")
			out.append({"name": ev.child, "type": "VFXEnable", "at": on,
					   "particle": ev.particle})
			off = marker_fraction(action, f"fx.{i}.off")
			if off is not None:
				out.append({"name": ev.child, "type": "VFXDisable", "at": off})
	return out


def specs_from_scenery(scenery, fps):
	"""Turn the UI list into ClipSpecs, deriving everything derivable.

	The clip name is the Action name verbatim - that is not a convention we chose,
	it is how the manis exporter names clips. The duration comes from the Action's
	frame range, never from a typed value.

	Raises ValueError with a message meant for the user, since every failure here
	is something they can fix in the list.
	"""
	specs = []
	# The same Action twice yields the same enum_name, which is ambiguous under
	# append-only indexing. parse_spec rejects it, but that happens at BUILD time,
	# long after the list is out of sight - so catch it here, where the user is
	# looking at the offending row and can fix it
	seen = {}
	for i, item in enumerate(scenery.anim_choices):
		if not item.action:
			raise ValueError(f"entry {i} ({item.label!r}) has no Action assigned")
		if item.frame_count < 2:
			raise ValueError(
				f"entry {i} ({item.action.name}) has {item.frame_count} frame(s) - "
				f"a clip needs at least 2")
		if item.action.name in seen:
			raise ValueError(
				f"entry {i} uses Action {item.action.name!r}, already used by entry "
				f"{seen[item.action.name]} - each entry needs its own clip, because "
				f"the clip name IS the dropdown entry's identity")
		seen[item.action.name] = i
		# UNQUALIFIED on purpose. The Action name is the clip-local name; the asset
		# prefix and the loc symbol are the build's job, because only the target
		# asset knows the prefix and a wrong one plays silently in game
		specs.append(ClipSpec.unqualified(
			item.action.name, item.label, item.duration(fps), item.loop,
			events=events_from_choice(item),
			weight=item.weight if item.in_random_pool else None))
	return specs
