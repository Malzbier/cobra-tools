"""A raw pack is a contract with tools outside this repo, so its bytes are the
thing under test, not just its behaviour. A pack written today must still
unpack the same way after any change in here, and a malformed one must be
refused outright rather than half-read into a plausible-looking result.

Pinned below: the round trip, the sentinel that keeps an empty pointer
distinguishable from one aimed at block 0, and the three ways a bad pack is
rejected. pack/unpack are pure functions over bytes, so this needs no game
install, no OVL fixture and no Blender.
"""
import random
import struct

import pytest

from modules.formats.raw_pack import MAGIC, NONE, VERSION, pack, unpack


def export(**overrides):
	"""A raw_export-shaped dict; each override replaces a whole key."""
	payload = {
		"name": "aq_doors01",
		"ext": ".motiongraph",
		"ovs_name": "STATIC",
		"version": 7,
		"pool_type": 2,
		"set_pool_type": 4,
		"root_block": 0,
		"blocks": [
			{"pool_type": 2, "data": b"\x01\x02\x03\x04" * 4},
			{"pool_type": 4, "data": b"AnimationActivity\x00"},
			{"pool_type": 2, "data": b""},
		],
		"relocations": [(0, 0, 1, 0), (0, 8, 2, 4)],
		"dependencies": [(0, 12, "somefile.tex")],
	}
	payload.update(overrides)
	return payload


def round_trip(exported):
	"""unpack(pack(x)), asserting the parts that must survive unchanged."""
	got = unpack(pack(exported))
	assert [b["data"] for b in got["blocks"]] == [b["data"] for b in exported["blocks"]]
	assert [b["pool_type"] for b in got["blocks"]] == [b["pool_type"] for b in exported["blocks"]]
	assert got["relocations"] == [tuple(r) for r in exported["relocations"]]
	assert got["dependencies"] == [tuple(d) for d in exported.get("dependencies", ())]
	return got


class TestRoundTrip:

	def test_scalars_survive(self):
		got = round_trip(export())
		assert got["name"] == "aq_doors01"
		assert got["ext"] == ".motiongraph"
		assert got["ovs_name"] == "STATIC"
		assert got["version"] == 7
		assert got["pool_type"] == 2
		assert got["set_pool_type"] == 4
		assert got["root_block"] == 0

	def test_block_data_survives_byte_for_byte(self):
		# the middle block is the interesting one: an embedded NUL must not be
		# read as a string terminator once block data starts
		got = round_trip(export())
		assert got["blocks"][1]["data"] == b"AnimationActivity\x00"
		assert got["blocks"][2]["data"] == b""
		assert got["blocks"][2]["size"] == 0

	def test_all_byte_values_survive(self):
		data = bytes(range(256))
		got = round_trip(export(blocks=[{"pool_type": 0, "data": data}]))
		assert got["blocks"][0]["data"] == data

	def test_repacking_is_byte_identical(self):
		# the property that actually matters to an external tool: decoding and
		# re-encoding must not drift, or two tools disagree about the same file
		once = pack(export())
		assert pack(unpack(once)) == once

	def test_degenerate_pack_with_nothing_in_it(self):
		got = round_trip(export(blocks=[], relocations=[], dependencies=[]))
		assert got["blocks"] == []

	def test_names_are_utf8_not_ascii(self):
		got = round_trip(export(name="dörren", ovs_name="STATIC_ü"))
		assert got["name"] == "dörren"
		assert got["ovs_name"] == "STATIC_ü"

	def test_many_shapes(self):
		# a seeded sweep rather than one hand-picked case, so block/reloc/dep
		# counts and empty-pointer placement vary; seeded so a failure repeats
		rng = random.Random(20260808)
		for _ in range(200):
			n_blocks = rng.randrange(0, 6)
			blocks = [{"pool_type": rng.randrange(0, 8),
					   "data": bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40)))}
					  for _ in range(n_blocks)]
			relocs = []
			deps = []
			if n_blocks:
				for _ in range(rng.randrange(0, 10)):
					dst = rng.choice([None] + list(range(n_blocks)))
					relocs.append((rng.randrange(n_blocks), rng.randrange(0, 64),
								   dst, rng.randrange(0, 64)))
				for _ in range(rng.randrange(0, 4)):
					deps.append((rng.randrange(n_blocks), rng.randrange(0, 64),
								 f"dep{rng.randrange(100)}.tex"))
			round_trip(export(blocks=blocks, relocations=relocs, dependencies=deps))


class TestAbsentValues:

	def test_empty_pointer_stays_distinguishable_from_block_zero(self):
		# the whole reason dst_block has a sentinel: None must not come back as
		# 0, or an empty pointer silently retargets the first block
		got = round_trip(export(relocations=[(0, 0, None, 1), (0, 4, 0, 0)]))
		assert got["relocations"][0][2] is None
		assert got["relocations"][1][2] == 0

	def test_absent_scalars_come_back_absent(self):
		got = unpack(pack(export(pool_type=None, set_pool_type=None, root_block=None)))
		assert got["pool_type"] is None
		assert got["set_pool_type"] is None
		assert got["root_block"] is None

	def test_negative_means_absent(self):
		# _u32 treats a negative as 'absent', which is how the OVL side spells
		# "no such pool" in places
		got = unpack(pack(export(root_block=-1, set_pool_type=-1)))
		assert got["root_block"] is None
		assert got["set_pool_type"] is None

	def test_empty_strings_survive_as_empty(self):
		got = unpack(pack(export(name="", ovs_name="")))
		assert got["name"] == ""
		assert got["ovs_name"] == ""

	def test_a_missing_version_is_refused_at_pack_time(self):
		# version has no NONE sentinel on the way back out - unpack cannot
		# tell a real 0xFFFFFFFF from "absent", unlike root_block/set_pool_type
		# above, so a missing one used to silently become the literal integer
		# 4294967295 on read instead of round-tripping as anything sensible
		with pytest.raises(ValueError, match="needs a version"):
			pack(export(version=None))

	def test_a_missing_block_pool_type_is_refused_at_pack_time(self):
		# same reasoning as version: a block's own pool is not an optional
		# concept the way the loader-level fields above are
		blocks = [{"pool_type": None, "data": b"x"}]
		with pytest.raises(ValueError, match="block 0 has no pool_type"):
			pack(export(blocks=blocks))

	def test_the_missing_block_is_named_by_index(self):
		blocks = [{"pool_type": 0, "data": b"a"},
				 {"pool_type": None, "data": b"b"}]
		with pytest.raises(ValueError, match="block 1 has no pool_type"):
			pack(export(blocks=blocks))

	def test_none_name_becomes_empty_string(self):
		assert unpack(pack(export(name=None)))["name"] == ""


class TestRejectsMalformed:

	def test_bad_magic(self):
		data = bytearray(pack(export()))
		data[0:4] = b"XXXX"
		with pytest.raises(ValueError, match="not a cobra raw pack"):
			unpack(bytes(data))

	def test_unsupported_version(self):
		data = bytearray(pack(export()))
		struct.pack_into("<I", data, 4, VERSION + 1)
		with pytest.raises(ValueError, match="unsupported raw pack version"):
			unpack(bytes(data))

	def test_trailing_data(self):
		# a truncated or over-long pack must fail loudly; silently ignoring the
		# tail is how a partly-written file passes for a whole one
		with pytest.raises(ValueError, match="trailing data"):
			unpack(pack(export()) + b"\x00")

	def test_value_too_large_for_its_field(self):
		with pytest.raises(ValueError, match="does not fit a u32"):
			pack(export(root_block=NONE))

	def test_magic_is_what_the_docstring_says(self):
		assert pack(export())[:4] == MAGIC == b"CBRW"
