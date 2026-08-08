"""Wwise event-bank hash membership, ported from mod-side catalogue mining.

Banks store FNV-1(lowercased name) and no strings at all, so this is a bare
hash SET, not a name lookup - a different shape from the audio.py hash->name
dict (which only knows the subset of names someone has already identified).
Kept out of ConstantsProvider's per-game dict merge because it isn't a named
dict: ~1M entries with no names would either bloat that convention for no
benefit or collide with dict_names' substring matching in _ConstantsProvider.
"""
import os
import struct
from functools import lru_cache


@lru_cache(maxsize=None)
def load_audio_event_hashes(game):
	"""The set of 32-bit event-bank hashes for `game`, or None if unmeasured."""
	path = os.path.join(os.path.dirname(__file__), game, "audio_event_hashes.bin")
	if not os.path.isfile(path):
		return None
	with open(path, "rb") as f:
		raw = f.read()
	return frozenset(struct.unpack(f"<{len(raw) // 4}I", raw))
