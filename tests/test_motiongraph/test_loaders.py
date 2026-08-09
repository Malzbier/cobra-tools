"""What this fork added to the two motiongraph loaders.

Scoped deliberately to the changed behaviour: the root-tail capture and write
back, the game gate on create, the `$` clip-reference form in accept_string,
and the motiongraphvars recursion dict. Everything else in these classes is
upstream's and is left alone.

Three of these decide whether a shipped asset works at all and none is visible
in the resulting file: a PC2 clip reference is spelled `Asset$Clip`, so missing
it leaves a renamed asset's state machine driving nothing; a write on a game
whose layout was never measured must refuse rather than guess; and the
unmodelled tail of an oversized root block must survive a round trip.

The loaders need an OVL only for its context, so this runs against a stub. No
game install, no fixture.
"""
import io
import logging

import pytest

from modules.formats.MOTIONGRAPH import MotiongraphLoader
from modules.formats.MOTIONGRAPHVARS import MotiongraphvarsLoader


class StubContext:
	version = 20
	user_version = 0
	mime_version = 0


class StubOvl:
	"""Just enough OvlFile for BaseFile.__init__ and the loaders' own gates."""

	def __init__(self, game="Planet Coaster 2", version=20, user_version=0, **cfg):
		self.game = game
		self.version = version
		self.user_version = user_version
		self.cfg = cfg
		self.context = StubContext()


class StubPool:

	def __init__(self, data, size_map):
		self.data = io.BytesIO(data)
		self.size_map = size_map


class StubHeader:

	def __init__(self, io_size=72):
		self.io_size = io_size


def loader(ovl=None, cls=MotiongraphLoader):
	return cls(ovl or StubOvl(), "asset.motiongraph", 7)


class TestAcceptStringTakesTheDollarForm:
	"""accept_string gained the `$` form; the `@` form must still work."""

	@pytest.mark.parametrize("name", ["Asset$Idle02", "Prefix$", "$"])
	def test_pc2_clip_references_are_renamed(self, name):
		# without this a renamed PC2 asset keeps every <mani> reference pointed
		# at the ORIGINAL name, and the copy animates nothing
		assert loader().accept_string(name) is True

	@pytest.mark.parametrize("name", ["Acrocanthosaurus@JumpAttack", "@"])
	def test_the_existing_at_form_still_works(self, name):
		assert loader().accept_string(name) is True

	@pytest.mark.parametrize("name", ["Acro_FightReact", "", "plain"])
	def test_names_with_neither_marker_fall_through_unchanged(self, name):
		# the fallback is upstream's: it defers to the rename_sound config
		assert loader().accept_string(name) is False
		assert loader(StubOvl(motiongraph_rename_sound=True)).accept_string(name) is True


class TestCreateIsGameGated:
	"""Upstream refused every game; this fork allows exactly one."""

	@pytest.mark.parametrize("game", [
		"Planet Zoo", "Planet Coaster", "Jurassic World Evolution",
		"Jurassic World Evolution 2", "Jurassic World Evolution 3", "",
	])
	def test_unmeasured_games_raise_rather_than_guess(self, game, tmp_path):
		with pytest.raises(NotImplementedError) as excinfo:
			loader(StubOvl(game=game)).create(str(tmp_path / "x.xml"))
		# the message must name the offending game, or a user cannot tell why
		assert repr(game) in str(excinfo.value)

	def test_jwe3_is_refused_even_though_is_pc_2_covers_it(self):
		# the gate tests `game` rather than is_pc_2 on purpose: is_pc_2 is true
		# for JWE3 too, and JWE3 is equally unmeasured
		with pytest.raises(NotImplementedError):
			loader(StubOvl(game="Jurassic World Evolution 3")).create("x.xml")


class TestRootTail:
	"""An oversized root block carries bytes no struct models; they must survive."""

	def test_captures_the_bytes_past_the_header(self):
		lo = loader()
		lo.header = StubHeader(io_size=72)
		# a 288-byte block against a 72-byte header, the retail shape
		payload = bytes(range(256)) * 2
		lo.root_ptr = (StubPool(payload, {0: 288}), 0)
		lo.capture_root_tail()
		assert lo.header.name_root_tail == payload[72:288].hex()

	def test_captures_from_a_nonzero_block_offset(self):
		lo = loader()
		lo.header = StubHeader(io_size=72)
		payload = bytes(range(256)) * 2
		lo.root_ptr = (StubPool(payload, {16: 200}), 16)
		lo.capture_root_tail()
		assert lo.header.name_root_tail == payload[16 + 72:16 + 200].hex()

	def test_no_tail_when_the_block_is_exactly_the_header(self):
		lo = loader()
		lo.header = StubHeader(io_size=72)
		lo.root_ptr = (StubPool(b"\x00" * 72, {0: 72}), 0)
		lo.capture_root_tail()
		assert not hasattr(lo.header, "name_root_tail")

	def test_no_tail_when_the_block_size_is_unknown(self):
		lo = loader()
		lo.header = StubHeader(io_size=72)
		lo.root_ptr = (StubPool(b"\x00" * 288, {}), 0)
		lo.capture_root_tail()
		assert not hasattr(lo.header, "name_root_tail")

	def test_no_tail_when_the_block_runs_past_the_pool_data(self):
		# size_map claims 288 but the pool holds only the header, so the slice
		# comes out empty and nothing should be recorded
		lo = loader()
		lo.header = StubHeader(io_size=72)
		lo.root_ptr = (StubPool(b"\x00" * 72, {0: 288}), 0)
		lo.capture_root_tail()
		assert not hasattr(lo.header, "name_root_tail")

	def test_a_capture_failure_is_logged_at_load_time(self, caplog):
		# the load itself is never failed over this - one asset's odd root
		# block must not block inspecting or extracting anything else.
		# root_ptr stays (None, 0) until a real load
		lo = loader()
		lo.header = StubHeader(io_size=72)
		with caplog.at_level(logging.ERROR):
			lo.capture_root_tail()
		assert "Could not capture root tail" in caplog.text
		assert not hasattr(lo.header, "name_root_tail")

	def test_a_capture_failure_refuses_at_write_time(self, caplog):
		# the loss becomes permanent on write, not on load, so THIS is where
		# it must stop rather than silently shipping a short file
		lo = loader()
		lo.header = StubHeader(io_size=72)
		with caplog.at_level(logging.ERROR):
			lo.capture_root_tail()
		with pytest.raises(ValueError, match="could not be read"):
			lo.write_root_tail(io.BytesIO())

	def test_a_clean_load_with_no_tail_writes_nothing(self):
		# the case capture failure must stay distinguishable from: a root
		# block that is genuinely no larger than the header has no tail at
		# all, and must not be treated as though capture had failed
		lo = loader()
		lo.header = StubHeader(io_size=72)
		lo.root_ptr = (StubPool(b"\x00" * 72, {0: 72}), 0)
		lo.capture_root_tail()
		stream = io.BytesIO()
		lo.write_root_tail(stream)
		assert stream.getvalue() == b""

	def test_written_back_verbatim(self):
		lo = loader()
		lo.header = StubHeader()
		tail = b"\xde\xad\xbe\xef" * 54
		lo.header.name_root_tail = tail.hex()
		stream = io.BytesIO()
		lo.write_root_tail(stream)
		assert stream.getvalue() == tail

	@pytest.mark.parametrize("tail", [None, ""])
	def test_nothing_written_when_there_is_no_tail(self, tail):
		lo = loader()
		lo.header = StubHeader()
		if tail is not None:
			lo.header.name_root_tail = tail
		stream = io.BytesIO()
		lo.write_root_tail(stream)
		assert stream.getvalue() == b""

	def test_capture_then_write_round_trips(self):
		# the pair is the actual contract: what capture stores, write must emit
		lo = loader()
		lo.header = StubHeader(io_size=72)
		payload = bytes(range(200))
		lo.root_ptr = (StubPool(payload, {0: 200}), 0)
		lo.capture_root_tail()
		stream = io.BytesIO()
		lo.write_root_tail(stream)
		assert stream.getvalue() == payload[72:200]


class TestCollectCapturesTheTail:
	"""collect() gained a capture_root_tail() call; it must fire on the path
	that actually collects, and not on the paths upstream gates out."""

	def collect_spy(self, monkeypatch):
		calls = []
		monkeypatch.setattr("modules.formats.BaseFormat.MemStructLoader.collect",
							lambda self: calls.append("super"))
		monkeypatch.setattr(MotiongraphLoader, "capture_root_tail",
							lambda self: calls.append("tail"))
		return calls

	def test_captures_after_collecting(self, monkeypatch):
		calls = self.collect_spy(monkeypatch)
		loader(StubOvl(version=20)).collect()
		# order matters: the tail is read out of the pool the collect populated
		assert calls == ["super", "tail"]

	def test_no_capture_when_the_version_gate_skips_collection(self, monkeypatch):
		calls = self.collect_spy(monkeypatch)
		loader(StubOvl(version=18)).collect()
		assert calls == []

	def test_no_capture_for_jwe(self, monkeypatch):
		calls = self.collect_spy(monkeypatch)
		loader(StubOvl(version=19, user_version=24724)).collect()
		assert calls == []


class TestMotiongraphvarsLoader:

	def test_collect_sets_its_own_recursion_dict(self, monkeypatch):
		# an enum variable's enum_name aliases its var_name, so the file is a
		# DAG; nothing guarantees a motiongraph was collected first, and without
		# a recursion dict the two pointers write the string twice
		calls = []
		monkeypatch.setattr("modules.formats.BaseFormat.MemStructLoader.collect",
							lambda self: calls.append(self.context.recursion))
		lo = loader(cls=MotiongraphvarsLoader)
		lo.collect()
		assert calls == [{}]

	def test_a_recursion_dict_is_fresh_per_collect(self, monkeypatch):
		monkeypatch.setattr("modules.formats.BaseFormat.MemStructLoader.collect",
							lambda self: None)
		lo = loader(cls=MotiongraphvarsLoader)
		lo.collect()
		first = lo.context.recursion
		first["stale"] = object()
		lo.collect()
		assert lo.context.recursion == {}
		assert lo.context.recursion is not first

	def test_neither_graph_format_interns_strings(self):
		# both readers record block identity, so value-interning on write would
		# collapse blocks retail ships separately
		assert MotiongraphvarsLoader.INTERN_STRINGS is False
		assert MotiongraphLoader.INTERN_STRINGS is False
