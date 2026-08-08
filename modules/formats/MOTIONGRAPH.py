import logging
from inspect import isclass

from generated.formats.motiongraph.structs.DataStreamResourceData import DataStreamResourceData
from generated.formats.motiongraph.structs.MotiongraphHeader import MotiongraphHeader
import generated.formats.ovl.versions as ovl_versions
from modules.formats.BaseFormat import MemStructLoader


class MotiongraphLoader(MemStructLoader):
	target_class = MotiongraphHeader
	extension = ".motiongraph"
	# The reader records block identity for every string (context.recursion plus
	# the id/ref alias machinery), and retail graphs genuinely store equal text in
	# several separate blocks - a measured graph held four distinct 'AnimationActivity'
	# blocks. Value-interning on write collapsed those, losing 15 blocks and 21 of
	# 708 relocations on a null round trip, so sharing is reproduced from recorded
	# identity only
	INTERN_STRINGS = False

	@property
	def motiongraph_rename_sound(self):
		return self.ovl.cfg.get("motiongraph_rename_sound", False)

	def create(self, file_path):
		"""Write a .motiongraph from XML - PC2 only.

		Every measurement behind pointer aliasing, array inference and root-tail
		capture (this file, source/formats/ovl_base/structs/Pointer.py) was taken
		against Planet Coaster 2's own corpus - never JWE1/2/3, Planet Zoo or PC1.
		`is_pc_2` also covers JWE3, which is equally unmeasured, so this checks
		`game` directly - the same test motiongraph_generator.generate() uses for
		the from-scratch build path, so injection and generation refuse on the
		same terms.

		Before this write path existed, create() was inherited unimplemented and
		raised NotImplementedError for every game; that stays true for anything
		that is not Planet Coaster 2, rather than attempting an unvalidated write
		using structural assumptions measured on a different game's layout.
		"""
		if self.ovl.game != "Planet Coaster 2":
			raise NotImplementedError(
				f"writing .motiongraph is only measured and verified for "
				f"'Planet Coaster 2', not {self.ovl.game!r}")
		super().create(file_path)

	def collect(self):
		self.context.recursion = {}
		if self.ovl.version >= 19:
			# structs are too different, doesn't register anim names, would break rename contents
			if ovl_versions.is_jwe(self.ovl):
				return
			super().collect()
			self.capture_root_tail()

	def capture_root_tail(self):
		"""The root block is sometimes LARGER than MotiongraphHeader.

		The header is exactly 72 bytes, but in several retail assets (SF_DogBot,
		SF_LaserScanner, SF_OrbitalCannon, SF_OutpostSoldierAnimatronics,
		SF_RadarDish, SF_RepairDroid, SF_RobotArm, WS_Cowboy) the root block is
		288: 72 of header plus 216 of unmodelled data that
		`first_non_transition_state` indexes into. Writing only the 72 bytes we
		model loses the rest.

		Rather than guess the record semantics, preserve the tail VERBATIM. The
		attribute is named `name_root_tail` deliberately: BaseStruct._to_xml only
		carries instance metadata whose name CONTAINS "name", and
		MemStruct._from_xml restores unknown attributes verbatim - so this rides
		the existing round-trip channel instead of inventing one.
		"""
		try:
			pool, off = self.root_ptr
			blk = pool.size_map.get(off)
			hdr = int(self.header.io_size)
			if blk is not None and int(blk) > hdr:
				tail = pool.data.getvalue()[int(off) + hdr:int(off) + int(blk)]
				if tail:
					self.header.name_root_tail = tail.hex()
		except Exception:
			logging.exception(f"Could not capture root tail for {self.name}")

	def write_root_tail(self, stream):
		"""Write back the root tail captured by capture_root_tail.

		Several retail assets have a 288-byte root block against a 72-byte header;
		the remaining 216 bytes are preserved verbatim rather than modelled.
		"""
		tail = getattr(self.header, "name_root_tail", None)
		if tail:
			stream.write(bytes.fromhex(tail))

	def get_audio_strings(self):
		def cond(x):
			try:
				return x[1].__name__ == "DataStreamResourceData"
			except:
				return False
		# condition_function = lambda x: hasattr(x[1], "__name__") and x[1].__name__ == "DataStreamResourceData"
		# condition_function = lambda x:  issubclass(x[1], DataStreamResourceData)
		for data_stream_resource_data in self.header.get_condition_fields(cond):
			if data_stream_resource_data.type.data in ("AudioEvent", "AudioLoopingEvent", "AudioBlend", "AudioRTPC"):
				yield data_stream_resource_data.ds_name.data

	def accept_string(self, in_str):
		"""Return True if string should receive replacement"""
		# anims have @ eg. Acrocanthosaurus@JumpAttackDefendFlankLeft
		# PC2 uses $ instead, i.e. Asset$Clip - without this, renaming an
		# animated PC2 asset leaves every <mani> reference pointing at the
		# ORIGINAL name, so the renamed copy's state machine drives nothing
		if "@" in in_str or "$" in in_str:
			return True
		# sound events don't, e.g. Acrocanthosaurus_FightReact
		return self.motiongraph_rename_sound
