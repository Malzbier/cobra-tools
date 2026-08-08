from generated.formats.ovl.imports import name_type_map
import itertools
import logging
import os
import re
import zlib
import math
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import cpu_count
from collections import Counter
from contextlib import contextmanager
from io import BytesIO

from constants import ConstantsProvider
from generated.formats.ovl.structs.ArchiveEntry import ArchiveEntry
from generated.formats.ovl.structs.BufferGroup import BufferGroup
from generated.formats.ovl.structs.Header import Header
from generated.formats.ovl.structs.OvsHeader import OvsHeader
from generated.formats.ovl.versions import *
from generated.formats.ovl_base.enums.Compression import Compression
from modules.formats.formats_dict import FormatDict
from modules.formats import raw_pack
from modules.formats.shared import djb2, DummyReporter, unescape_path, make_out_dir_func, splitext_safe

try:
	from modules.formats.utils.oodle.oodle import oodle_compressor, OodleDecompressEnum, INPUT_CHUNK_SIZE, OODLE_CODEC_NAME
except:
	oodle_compressor = OodleDecompressEnum = None

UNK_HASH = "UnknownHash"


def oodle_compress_chunk(args):
	"""Picklable function for ThreadPoolExecutor"""
	uncompressed_bytes, oodle_codec, oodle_level = args
	return oodle_compressor.compress(uncompressed_bytes, oodle_codec, level=oodle_level)


def order_like_source(rows, remembered, fallback_key, identity=tuple):
	"""Order `rows` the way the source file had them, falling back for anything new.

	Retail's order for some arrays is reproducible by no sort of their own fields --
	all 24 permutations of the four fragment fields were tested against 19 shipped
	Planet Coaster archives and none matches anywhere, and the same is true of the
	names buffer. It is consistent, just not derivable, which looks like a fixed
	engine-side order. So the only way to write it back is to remember what was read.

	Rows absent from `remembered` are genuinely new content, and keep the writer's
	existing deterministic order behind the remembered ones. With nothing remembered
	(a file built from scratch) this degrades to exactly the previous behaviour.

	Version 20 already matches retail on these arrays, so this is a no-op there; it is
	version 18 that needs it. That is why there is no version gate -- preserving an
	order that already agrees changes nothing.
	"""
	positions = {}
	for i, row in enumerate(remembered):
		positions.setdefault(row, []).append(i)
	used = {}

	def key(row):
		t = identity(row)
		known = positions.get(t)
		n = used.get(t, 0)
		if known and n < len(known):
			used[t] = n + 1
			return 0, known[n], ()
		return 1, 0, fallback_key(row)

	# sorted() evaluates key exactly once per element, so the duplicate counter is safe
	return sorted(rows, key=key)


def aux_layout_key(loader):
	"""Where this loader's data sat in the source aux file, or infinity if unknown.

	Read before flush_to_aux runs, while the loaded mip offsets still describe the
	shipped layout. Anything without one sorts last and keeps its name order, so a
	from-scratch build behaves exactly as before.
	"""
	texbuffer = getattr(loader, "texbuffer", None)
	mips = getattr(texbuffer, "mip_maps", None) if texbuffer is not None else None
	if not mips:
		return float("inf")
	try:
		# only the first num_mips entries carry a real offset -- flush_to_aux assigns
		# offsets under exactly that bound, and the unused tail sits at 0. Taking min()
		# over all of them returns 0 for every loader and silently sorts nothing
		n_valid = int(getattr(texbuffer, "num_mips", 0))
		valid = [int(m.offset) for m in mips[:n_valid]] if n_valid else []
		return min(valid) if valid else float("inf")
	except Exception:
		return float("inf")


def buffer_identity(buffer):
	"""Persisted fields of a BufferEntry, stable across a rebuild."""
	return int(buffer.index), int(buffer.size), int(buffer.file_hash)


class OvsFile(OvsHeader):

	def __init__(self, context, ovl_inst, archive_entry):
		# init with a dummy default archive
		dummy_archive = ArchiveEntry(context, None, None)
		super().__init__(context, dummy_archive, None)
		self.ovl = ovl_inst
		# set arg later to avoid initializing huge arrays with default data
		self.arg = archive_entry

	def clear_ovs_arrays(self):
		self.arg.num_datas = self.arg.num_buffers = self.arg.num_buffer_groups = 0
		self.reset_field("data_entries")
		self.reset_field("buffer_entries")
		self.reset_field("buffer_groups")

	@contextmanager
	def decompress(self, reporter, compressed_bytes, uncompressed_size):
		"""Decompress compressed_bytes according to ovl settings and yield the decompressed stream"""
		# logging.debug(f"Compression magic bytes: {compressed_bytes[:2]}, {len(compressed_bytes)} bytes total")
		# logging.debug(f"Compressed: {len(compressed_bytes)}")
		try:
			if self.ovl.user_version.compression == Compression.OODLE:
				logging.debug(f"Oodle compression")
				if not oodle_compressor:
					reporter.show_error(f"Can't run Oodle to decompress this archive")
					raise
				decompressed = oodle_compressor.decompress(compressed_bytes, len(compressed_bytes), uncompressed_size)
			elif self.ovl.user_version.compression == Compression.ZLIB:
				logging.debug("Zlib compression")
				# https://stackoverflow.com/questions/1838699/how-can-i-decompress-a-gzip-stream-with-zlib
				# we avoid the two zlib magic bytes to get our unzipped content
				decompressed = zlib.decompress(compressed_bytes[2:], wbits=-zlib.MAX_WBITS)
			# uncompressed archive
			else:
				logging.debug("No compression")
				decompressed = compressed_bytes
		except:
			reporter.show_error(f"{self.ovl.user_version.compression.name} decompression failed for {self.arg.name} - potentially damaged file?")
			raise
		with BytesIO(decompressed) as stream:
			yield stream

	def compress(self, uncompressed_bytes, use_threads=True):
		"""Compress uncompressed_bytes according to ovl settings"""
		if self.ovl.user_version.compression == Compression.OODLE:
			try:
				# Retail Planet Zoo ships Kraken at level 6, not 5. Same class of defect
				# as the zlib level below: with identical uncompressed bytes the archive's
				# compressed_size still differs, so no file can be byte-identical
				# Measured by decompressing shipped archives and re-compressing across
				# every codec and level: Kraken 6 reproduces the shipped bytes EXACTLY
				# (Aardvark_Female, PlayerReward_Jaguar_AMAL, EA_PathExtras_Picnic_02,
				# Loc), while the old default of 5 came out consistently larger
				oodle_level = self.ovl.cfg.get("oodle_level", 6)
				# todo - allow changing codec from cfg?
				codec_name = OODLE_CODEC_NAME
				# Compress in larger chunks, have each Oodle thread handle 256KB chunking
				uncompressed_size = len(uncompressed_bytes)
				chunk_size = min(INPUT_CHUNK_SIZE, uncompressed_size)
				num_chunks = math.ceil(uncompressed_size / chunk_size)

				# Split the input accordingly
				chunks = []
				for i in range(num_chunks):
					start = i * chunk_size
					chunks.append((uncompressed_bytes[start:start + chunk_size], codec_name, oodle_level))

				if use_threads:
					num_threads = min(cpu_count(), num_chunks)
					logging.debug(f"Oodle compression: {codec_name} using {num_threads} threads")
					with ThreadPoolExecutor(max_workers=num_threads) as executor:
						compressed = b"".join(executor.map(oodle_compress_chunk, chunks))
				else:
					logging.debug(f"Oodle compression: {codec_name} using single thread")
					compressed = b"".join(oodle_compress_chunk(chunk) for chunk in chunks)

				return len(uncompressed_bytes), len(compressed), compressed
			except:
				logging.exception(f"Oodle compression failed, falling back to Zlib")
				self.ovl.user_version.compression = Compression.ZLIB

		if self.ovl.user_version.compression == Compression.ZLIB:
			# Retail's level is not the same on every title, and getting it wrong is
			# the whole remaining gap on Planet Coaster: with identical uncompressed
			# bytes, level 9 compresses ~100-400 bytes SMALLER than shipped, so the
			# archive's compressed_size differs and no file can be byte-identical
			# Measured by decompressing shipped archives and re-compressing at every
			# level: Planet Coaster reproduces the shipped bytes EXACTLY at level 6
			# (10/10 archives), Planet Coaster 2 at level 9 (some inputs also match at
			# 7 or 8, where those levels happen to emit the same output as 9)
			# Only versions 18 and 20 were measured; the threshold follows the same
			# schema boundary the rest of this file gates on. Planet Zoo is Oodle, so
			# this path does not apply to it
			default_level = 6 if self.ovl.version < 19 else 9
			zlib_level = self.ovl.cfg.get("zlib_level", default_level)
			compressed = zlib.compress(uncompressed_bytes, zlib_level)
			return len(uncompressed_bytes), len(compressed), compressed

		# uncompressed only stores the raw length, 0 for decompressed size
		return 0, len(uncompressed_bytes), uncompressed_bytes

	def update_hashes(self):
		logging.info(f"Updating hashes for {self.arg.name}")
		entry_lists = (
			self.pools,
			self.data_entries,
			# self.buffer_entries
		)
		# update references to ovl files
		for entry_list in entry_lists:
			for entry in entry_list:
				if not entry.name:
					logging.warning(f"{entry} has no name assigned to it, cannot assign proper ID")
					continue
				try:
					loader = self.ovl.loaders[entry.name]
				except KeyError:
					# raise KeyError(f"No loader for '{entry.name}' ({type(entry).__name__})")
					logging.warning(f"No loader for '{entry.name}' ({type(entry).__name__})")
					continue
				if self.ovl.user_version.use_djb:
					entry.file_hash = loader.file_hash
				else:
					entry.file_hash = loader.file_index
				entry.ext_hash = loader.ext_hash
		for pool in self.pools:
			if not self.ovl.user_version.use_djb:
				# PZ, PC2
				pool.ext_hash = 0

	def load(self, archive_entry, stream):
		logging.debug(
			f"Loading archive {archive_entry.name}")
		logging.debug(
			f"Compressed stream {archive_entry.name} in {os.path.basename(archive_entry.ovs_path)} starts at {stream.tell()}")
		compressed_bytes = stream.read(archive_entry.compressed_size)
		with self.decompress(self.ovl.reporter, compressed_bytes, archive_entry.uncompressed_size) as stream:
			try:
				super().read_fields(stream, self)
				# print(self)
				pool_index = 0
				for pool_type in self.pool_groups:
					for i in range(pool_type.num_pools):
						pool = self.pools[pool_index]
						pool.type = pool_type.type
						self.assign_name(pool)
						pool_index += 1

				for data_entry in self.data_entries:
					self.assign_name(data_entry)
					loader = self.ovl.loaders[data_entry.name]
					loader.data_entries[archive_entry.name] = data_entry

				# this can have colliding hashes
				self.root_entries_name = self.get_names_list(self.root_entries)

				if not (self.set_header.sig_a == 1065336831 and self.set_header.sig_b == 16909320):
					raise AttributeError("Set header signature check failed!")
				if self.set_header.io_size != self.arg.set_data_size:
					raise AttributeError(
						f"Set data size incorrect (got {self.set_header.io_size}, expected {self.arg.set_data_size})!")
				self.map_assets()
				# add IO object to every pool
				self.read_pools(stream)
				self.map_buffers()
				for buffer in self.buffers_io_order:
					# read buffer data and store it in buffer object
					buffer.read_data(stream)
			except:
				self.ovl.reporter.show_error(f"Loading {archive_entry.name} from {os.path.basename(archive_entry.ovs_path)} failed")
				logging.warning(archive_entry)
				logging.warning(self)
				if self.ovl.do_debug:
					with open(archive_entry.ovs_path + f"_{archive_entry.name}.dmp", "wb") as dump_ovs:
						stream.seek(0)
						dump_ovs.write(stream.read())

	def read_pools(self, stream):
		for pool in self.pools:
			pool.data = BytesIO(stream.read(pool.size))

	def map_assets(self):
		"""Parse set and asset entries, and store children on loaders"""
		# store start and stop asset indices
		set_entries_name = self.get_names_list(self.set_header.sets)
		set_entries_offsets = list(self.set_header.sets["start"])
		# add end value
		set_entries_offsets.append(self.set_header.asset_count)
		for name, (start, end) in zip(set_entries_name, itertools.pairwise(set_entries_offsets)):
			# map assets to entry
			assets = self.set_header.assets[start: end]
			assets_root_index = assets["root_index"]
			# store the references on the corresponding loader
			loader = self.ovl.loaders[name]
			loader.children = [self.ovl.loaders[self.root_entries_name[i]] for i in assets_root_index]

	@staticmethod
	def transfer_identity(source_entry, target_entry):
		source_entry.name = target_entry.name
		source_entry.basename, source_entry.ext = splitext_safe(source_entry.name)
		source_entry.file_hash = target_entry.file_hash
		source_entry.ext_hash = target_entry.ext_hash

	def rebuild_buffer_groups(self, mime_lut):
		logging.info(f"Updating buffer groups for {self.arg.name}")
		if self.data_entries:
			for data_entry in self.data_entries:
				for buffer in data_entry.buffers:
					self.transfer_identity(buffer, data_entry)
				# to ensure that the io order matches, sort buffers per data too, as that will influence the order
				if self.ovl.version < 20:
					data_entry.buffers.sort(key=lambda b: b.index)
			# sort buffers
			if self.ovl.version < 20:
				# rely on the data_entry's hashes for sorting for JWE
				# sorting by index is not enforced in JWE stock
				self.buffer_entries.sort(key=lambda b: (b.ext, b.file_hash, b.index))
				# Planet Coaster ships an order this sort does not reproduce -- 13 of 34
				# sampled archives came out reordered, same entries throughout, and no
				# simple key matches (index gets 15/36, nothing better). So put back the
				# order the file was read with, keeping the sort as the tiebreak for
				# buffers the source did not have. Version 20 already agrees with retail
				# and is untouched
				remembered = getattr(self, "source_buffer_order", None)
				if remembered:
					self.buffer_entries[:] = order_like_source(
						list(self.buffer_entries), remembered,
						lambda b: (b.ext, b.file_hash, b.index), buffer_identity)
					# buffers_io_order (v<20) walks data_entry.buffers, not this array, so
					# the payload is written in whatever order those per-data lists are in
					# -- sorted by index just above, independently of what we just restored
					# On load each data entry's buffers are a slice of the file's
					# buffer_entries, so re-deriving the per-data order from the restored
					# global order reproduces how the file actually had them
					rank = {id(b): i for i, b in enumerate(self.buffer_entries)}
					for data_entry in self.data_entries:
						data_entry.buffers.sort(key=lambda b: rank.get(id(b), len(rank)))
			else:
				# cobra < 20 used buffer index per data entry
				self.buffer_entries.sort(key=lambda b: (b.ext, b.index, b.file_hash))

				# buffer_map = {buffer.ext: (set(), set()) for buffer in self.buffer_entries}
				# for buffer in self.buffer_entries:
				# 	indices, hashes = buffer_map[buffer.ext]
				# 	indices.add(buffer.index)
				# 	hashes.add(buffer.file_hash)
				# for ext, (indices, hashes) in buffer_map.items():
				# 	print(ext, indices, hashes)

				# generate the buffergroup entries
				buffer_group = None
				buffer_offset = 0
				data_offset = 0
				for buffer in self.buffer_entries:
					# logging.debug(f"Buffer {i}, last: {last_ext} this: {buffer.ext}")
					# we have to create a new group
					if not buffer_group or buffer.ext != buffer_group.ext or buffer.index != buffer_group.buffer_index:
						buffer_group = BufferGroup(self.context)
						buffer_group.ext = buffer.ext
						buffer_group.buffer_index = buffer.index
						buffer_group.buffer_offset = buffer_offset
						buffer_group.data_offset = data_offset
						self.buffer_groups.append(buffer_group)
					# gotta add this buffer to the current group
					buffer_group.buffer_count += 1
					buffer_group.size += buffer.size
					buffer_group.data_count += 1

				# fix the offsets of the buffer_groups
				for previous_group, buffer_group in itertools.pairwise(self.buffer_groups):
					buffer_group.buffer_offset = previous_group.buffer_offset + previous_group.buffer_count
					if buffer_group.ext != previous_group.ext:
						buffer_group.data_offset = previous_group.data_offset + previous_group.data_count
					else:
						buffer_group.data_offset = previous_group.data_offset
						if buffer_group.data_count < previous_group.data_count:
							buffer_group.data_count = previous_group.data_count
				# tex buffer_groups sometimes are 0,1 instead of 1,2 so the offsets need additional correction
				tex_data_offset = 0
				tex_buffer_count = 0
				texturestream_buffer_count = 0
				for buffer_group in self.buffer_groups:
					if ".tex" == buffer_group.ext:
						tex_data_offset = max(buffer_group.data_offset, tex_data_offset)
						tex_buffer_count = max(buffer_group.buffer_count, tex_buffer_count)
					elif ".texturestream" == buffer_group.ext:
						texturestream_buffer_count += buffer_group.buffer_count
				for buffer_group in self.buffer_groups:
					if ".tex" == buffer_group.ext:
						buffer_group.data_offset = tex_data_offset
						buffer_group.data_count = tex_buffer_count
					elif ".texturestream" == buffer_group.ext:
						buffer_group.data_count = texturestream_buffer_count

				if (self.buffer_groups[-1].data_count + self.buffer_groups[-1].data_offset) < len(self.data_entries):
					for x in range(self.buffer_groups[-1].buffer_index + 1):
						self.buffer_groups[-1 - x].data_count = len(self.data_entries) - self.buffer_groups[
							-1 - x].data_offset
		# update buffer groups
		for buffer_group in self.buffer_groups:
			buffer_group.ext_index = mime_lut.get(buffer_group.ext)

	def get_shared_pool_owner(self, pool, candidates):
		"""Pick the loader that should be credited as `pool`'s owner, given
		`candidates`: {id(loader): (loader, loader's own earliest offset into
		this pool)}.

		Retail packs multiple files' data into one shared pool routinely (not
		an edge case). Every rule here was mined against real retail data and
		cross-checked across three games:

			game            shared-pool cases   accuracy
			Planet Coaster 2          248         248/248 (100%)
			Planet Zoo                2695        2695/2695 (100%)
			Planet Coaster 1          1616        1614/1616 (99.9%)

		The two PC1 misses are both in one unusually complex 9-part
		animatronic (GHB_OmniGhost_Animatronics) whose sub-graphs cross-
		reference each other's states, so a loader's earliest offset into the
		pool isn't reliably "its own" content there - a case none of these
		rules claim to model. Still PC2-gated below despite this evidence:
		JWE/JWE2/JWE3/Warhammer/Zoo Tycoon are entirely unchecked.
		"""
		# the owner is whoever's OWN data starts earliest in the pool
		entries = list(candidates.values())  # [(loader, offset), ...]
		min_offset = min(o for l, o in entries)
		tied = [l for l, o in entries if o == min_offset]
		if len(tied) == 1:
			loader = tied[0]
		else:
			# a "set" container (.banis/.manis) shares its very first block
			# with its own member entries (.bani/.mani); retail always
			# credits the container. Beyond that, when several independent
			# files are tied (e.g. sibling animatronic .motiongraphs sharing
			# one pool's opening block), retail credits whichever name sorts
			# alphabetically first - confirmed against dict/file-table order
			# as the competing explanation on the cases where the two
			# disagree: alphabetical order won 5/6, file order 0/6
			containers = [l for l in tied if l.ext in (".banis", ".manis")]
			pool_ = containers if containers else tied
			loader = sorted(pool_, key=lambda l: l.name)[0]
		# one further exception, struct (type 3) pools only:
		# datastreamsonly.motiongraph is a single stub file reused verbatim
		# across many retail assets, in every game checked above (not PC2-
		# specific - PC1 alone ships it in 83 assets). Retail credits it over
		# any competing .fgm/.ms2/.datastreams file regardless of physical
		# offset - but not over a .tex (a derived, never independently
		# authored resource) or another real .motiongraph
		if pool.type == 3 and loader.ext not in (".tex", ".motiongraph"):
			stub = self.ovl.loaders.get("datastreamsonly.motiongraph")
			if stub is not None and id(stub) in candidates:
				loader = stub
		return loader

	def rebuild_pools(self):
		logging.info("Updating pool names, deleting unused pools")
		# build pool -> {id(loader): (loader, loader's earliest offset into
		# this pool)} in one pass over every loader's stack, rather than
		# re-scanning all loaders per pool. Only get_shared_pool_owner (PC2
		# only) uses more than the single first-offset case other games use
		pool_owners = {}
		for loader in self.ovl.loaders.values():
			for pool, offset in loader.stack:
				if offset is None:
					# an unresolved/null pointer target - not a real position
					# in the pool, so it can't be used to rank ownership
					continue
				candidates = pool_owners.setdefault(pool, {})
				key = id(loader)
				if key not in candidates or offset < candidates[key][1]:
					candidates[key] = (loader, offset)
		# map the pool types to pools
		pools_by_type = {}
		for pool_index, pool in enumerate(self.ovl.reporter.iter_progress(self.pools, "Rebuilding pools", cond=len(self.pools) > 1)):
			if pool.offsets:
				# store pool in pool_groups map
				if pool.type not in pools_by_type:
					pools_by_type[pool.type] = []
				pools_by_type[pool.type].append(pool)
				# try to get a name for the pool
				logging.debug(f"Pool[{pool_index}]: {len(pool.offsets)} structs")
				candidates = pool_owners.get(pool, {})
				if not candidates:
					# Two different states reach here and only one is corruption
					#
					# Structs that came out of the source file and simply were never
					# claimed are ordinary. collect() only claims what the struct model
					# can walk, and load() already reports the shortfall as "Could not
					# map N fragments" and keeps them for saving. Measured on assets
					# containing a .motiongraph: 13 of 60 on PC2, 5 of 60 on PC1, and in
					# both titles the assets with unclaimed structs are exactly the ones
					# with uncaught fragments. An untouched save writes these correctly,
					# so injecting some unrelated file must not turn them fatal - which
					# it did, failing 7 of 8 motiongraph reinjections
					#
					# A pool a half-finished create() left behind is the real defect this
					# guard was added for: get_pool + write_ptrs wrote bytes and offsets,
					# the exception aborted before register_loader, and padding it anyway
					# would ship a wrong header size that shifts every later pool's read
					# position on reload
					#
					# The two are separable because the first kind existed at load time
					from_file = getattr(pool, "offsets_from_file", None)
					if from_file is not None and set(pool.offsets) <= from_file:
						logging.warning(
							f"Pool[{pool_index}] type {pool.type}: {len(pool.offsets)} structs "
							f"from the source file that no loader claims, preserving as-is")
						# no loader to take a name from, so keep the one the file gave it
						pool.pad()
						continue
					raise ValueError(
						f"Pool[{pool_index}] type {pool.type} has {len(pool.offsets)} offsets "
						f"but no owning loader, and they did not come from the source file - "
						f"a create() failed partway through a write. Refusing to write an "
						f"orphaned, unsized pool.")
				if self.ovl.is_pc_2:
					loader = self.get_shared_pool_owner(pool, candidates)
				else:
					# unchanged original behaviour for every other game
					first_offset = pool.get_first_offset()
					ptr = (pool, first_offset)
					for loader in self.ovl.loaders.values():
						if ptr in loader.stack:
							break
					else:
						logging.warning(f"Could not find loader to get name for Pool[{pool_index}] type {pool.type} at offset {first_offset}")
						continue
				logging.debug(f"Pool[{pool_index}]: '{pool.name}' -> '{loader.name}'")
				self.transfer_identity(pool, loader)
				# logging.debug(f"Pool[{pool_index}]: '{pool.name}' (renamed)")
				# make sure that all pools are padded already before writing
				pool.pad()
			else:
				logging.debug(
					f"Pool[{pool_index}]: deleting '{pool.name}' from archive '{self.arg.name}' as it has no pointers")
				# logging.debug(
				# 	f"Pool[{pool_index}]: data '{pool.data.getvalue()}'")
		self.pools.clear()
		# rebuild pool groups
		self.arg.num_pool_groups = len(pools_by_type)
		self.reset_field("pool_groups")
		# print(pools_by_type)
		for pool_group, (pool_type, pools) in zip(self.pool_groups, sorted(pools_by_type.items())):
			pool_group.type = pool_type
			pool_group.num_pools = len(pools)
			self.pools.extend(pools)
		self.arg.num_pools = len(self.pools)

	def map_buffers(self):
		"""Map buffers to data entries"""
		logging.debug("Mapping buffers")
		if self.ovl.version >= 20:
			for data in self.data_entries:
				data.buffers = []
			logging.debug("Assigning buffer indices")
			for b_group in self.buffer_groups:
				b_group.ext = f".{self.ovl.mimes_ext[b_group.ext_index]}"
				# note that datas can be bigger than buffers
				buffers = self.buffer_entries[b_group.buffer_offset: b_group.buffer_offset + b_group.buffer_count]
				datas = self.data_entries[b_group.data_offset: b_group.data_offset + b_group.data_count]
				for buffer in buffers:
					buffer.index = b_group.buffer_index
					for data in datas:
						if buffer.file_hash == data.file_hash:
							self.transfer_identity(buffer, data)
							data.buffers.append(buffer)
							break
					else:
						raise BufferError(
							f"Buffer group {b_group.ext}, index {b_group.buffer_index} did not find a data entry for buffer {buffer.file_hash}")
		else:
			# sequentially attach buffers to data entries by each entry's buffer count
			i = 0
			for data in self.data_entries:
				data.buffers = self.buffer_entries[i: i+data.buffer_count]
				for buffer in data.buffers:
					self.transfer_identity(buffer, data)
				i += data.buffer_count

	@property
	def buffers_io_order(self):
		"""sort buffers into the order in which they are read from the file"""
		if self.ovl.version >= 20:
			return self.buffer_entries
		else:
			# sorting depends on order of data entries, and order of buffers therein (not necessarily sorted by index)
			io_order = []
			# only do this if there are any data entries so that max() doesn't choke
			if self.data_entries:
				# check how many buffers occur at max in one data block
				max_buffers_per_data = max([data.buffer_count for data in self.data_entries])
				# first read the first buffer for every file
				# then the second if it has any
				# and so on, until there is no data entry left with unprocessed buffers
				for i in range(max_buffers_per_data):
					for j, data in enumerate(self.data_entries):
						if i < data.buffer_count:
							io_order.append(data.buffers[i])
			return io_order

	def dump_pools(self, fp):
		"""for debugging"""
		with open(f"{fp}.pools", "w", encoding="utf-8") as f:
			for i, pool in enumerate(self.pools):
				f.write(f"\nPool {i} (type: {pool.type})")
		for pool in self.pools:
			with open(f"{fp}_{pool.i}.pool", "wb") as f:
				pool.get_debug_dump()
				f.write(pool.debug_dump)

	def dump_buffer_groups_log(self, fp):
		with open(f"{fp}_buffers.log", "w") as f:
			f.write("\nBuffers IO order")
			for x, buffer in enumerate(self.buffers_io_order):
				f.write(f"\n{buffer.name} index: {buffer.index}| {buffer.size}")
			f.write("\n\nBuffers")
			for x, buffer in enumerate(self.buffer_entries):
				f.write(f"\n{buffer.name} index: {buffer.index}| {buffer.size}")
			f.write("\n\n")
			for x, buffer_group in enumerate(self.buffer_groups):
				f.write(
					f"\n{buffer_group.ext} {buffer_group.buffer_offset} {buffer_group.buffer_count} {buffer_group.buffer_index} | {buffer_group.size} {buffer_group.data_offset} {buffer_group.data_count} ")

	def dump_stack(self, fp, only_files):
		"""for development; dumps the stack of selected files per file extension"""
		for ext, loaders in self.ovl.get_loaders_by_ext().items():
			# skip writing this stack if no file should be dumped
			# only dump loaders that are valid for this ovs, are in only_files and have a root_ptr
			valid_loaders = [loader for loader in loaders if loader.ovs == self and loader.name in only_files and loader.root_ptr[0]]
			if not valid_loaders:
				continue
			# write stack for ext
			with open(f"{fp}_{ext[1:]}.stack", "w", encoding="utf-8") as f:
				for loader in valid_loaders:
					pool, offset = loader.root_ptr
					size = pool.size_map[offset]
					f.write("\n\n\n")
					f.write(f"FILE {pool.i} | {offset} ({size: 4}) {loader.name}")
					f.write(loader.get_hex_dump(pool, offset, size))
					try:
						loader.dump_ptr_stack(f, loader.root_ptr, set())
					except AttributeError:
						logging.exception(f"Dumping {loader.name} failed")
						f.write("\n!FAILED!")

	def assign_name(self, entry):
		"""Fetch a filename for an entry"""
		# JWE style
		if self.ovl.user_version.use_djb:
			try:
				n = self.ovl.hash_table_local[entry.file_hash]
				e = f".{self.ovl.hash_table_local[entry.ext_hash]}"
			except KeyError:
				raise KeyError(
					f"No match for entry {entry.file_hash} [{entry.__class__.__name__}] from archive {self.arg.name}")
		# PZ Style and PC Style
		else:
			# file_hash is an index into ovl files
			try:
				file_name = self.ovl.files_name[entry.file_hash]
				loader = self.ovl.loaders[file_name]
				n = loader.basename
				e = loader.ext
			except IndexError:
				logging.warning(
					f"Entry ID {entry.file_hash} [{entry.__class__.__name__}] does not index into ovl file table of length {len(self.ovl.files)}")
				n = "none"
				e = ".ext"
		entry.ext = e
		entry.basename = n
		entry.name = f"{n}{e}"

	def get_names_list(self, array):
		"""Fetch list of file names for an array"""
		# JWE style
		if self.ovl.user_version.use_djb:
			# look up the hashes
			return [f"{n}.{e}" for n, e in zip(
					[self.ovl.hash_table_local[h] for h in array["file_hash"]],
					[self.ovl.hash_table_local[h] for h in array["ext_hash"]])]
		# PZ Style and PC Style
		else:
			# file_hash is an index into ovl files
			return [self.ovl.files_name[i] for i in array["file_hash"]]

	def assign_ids(self, array, loaders, with_ext_hash=True):
		"""Assign ids to an array"""
		if loaders:
			# JWE style
			if self.ovl.user_version.use_djb:
				# look up the hashes
				array["file_hash"] = [loader.file_hash for loader in loaders]
			# PZ Style and PC Style
			else:
				# file_hash is an index into ovl files
				array["file_hash"] = [loader.file_index for loader in loaders]
			if with_ext_hash:
				# PZ does not consistently store ext_hash, eg. it is no longer used on pools
				try:
					array["ext_hash"] = [loader.ext_hash for loader in loaders]
				except:
					pass

	def write_pools(self):
		logging.debug(f"Writing pools for {self.arg.name}")
		# do this first so pools can be updated
		pools_data_writer = BytesIO()
		for pool in self.pools:
			pool_bytes = pool.data.getvalue()
			pool.offset = self.get_pool_offset(pools_data_writer.tell())
			# logging.debug(f"pool.offset {pool.offset}, pools_start {self.arg.pools_start}")
			pools_data_writer.write(pool_bytes)
		self.pools_data = pools_data_writer.getvalue()

	def get_pool_offset(self, in_offset):
		# JWE, JWE2: relative offset for each pool
		if self.ovl.user_version.use_djb:
			return in_offset
		# PZ, PC: offsets relative to the whole pool block
		else:
			return self.arg.pools_start + in_offset

	def write_archive(self):
		logging.info(f"Writing archive {self.arg.name}")
		with BytesIO() as stream:
			# write out all entries
			super().write_fields(stream, self)
			# write the pools data containing all the pointers' datas
			stream.write(self.pools_data)
			# write buffer data
			for b in self.buffers_io_order:
				stream.write(b.data)
			return stream.getvalue()


class OvlFile(Header):

	def __init__(self):
		# pass self as context
		super().__init__(self)
		self.magic.data = b'FRES'
		self._game = None

		self.is_dev = 0
		self.cfg = {}

		self.formats_dict = FormatDict()
		self.constants = {}
		self.loaders = {}
		self.commands = {}
		self.included_ovl_names = []
		# set a default reporter here
		self.reporter = DummyReporter()

	@classmethod
	def context_to_xml(cls, elem, prop, instance, arg, template, debug):
		elem.attrib[prop] = str(instance.game)

	@property
	def is_pc_2(self):
		return self.game in ("Planet Coaster 2", "Jurassic World Evolution 3")

	@property
	def game(self):
		if self._game is None:
			self._game = get_game(self)[0].value
		return self._game

	@property
	def do_debug(self):
		return self.cfg.get("debug_mode", False)

	@game.setter
	def game(self, game_name):
		self._game = game_name
		set_game(self, game_name)

	def clear(self):
		self.num_archives = 0
		self.reset_field("archives")
		self.loaders = {}

	def init_loader(self, filename, ext, version):
		# fall back to BaseFile loader, but only for collecting, not creating
		from modules.formats.BaseFormat import BaseFile
		loader_cls = self.formats_dict.get(ext, BaseFile)
		return loader_cls(self, filename, version)

	def remove(self, filenames):
		"""
		Removes files from an ovl file
		:param filenames: list of file names (eg. "example.ext") that should be removed from ovl
		:return:
		"""
		logging.info(f"Removing files for {filenames}")
		for filename in filenames:
			self.loaders[filename].remove()
		self.send_files()

	@staticmethod
	def format_rename_str(name_tups):
		return ', '.join(' -> '.join(tup) for tup in name_tups)

	def rename(self, name_tups):
		logging.info(f"Renaming {self.format_rename_str(name_tups)}")
		# todo - support renaming included_ovls?
		# make a temporary copy
		temp_loaders = list(self.loaders.values())
		# see if the loaders would cause collisions
		# get new names for all loaders, unchanged name is None
		new_names = [loader.rename_check(name_tups) for loader in temp_loaders if loader.rename_check(name_tups)]
		# check the new names don't collide
		if len(new_names) != len(set(new_names)):
			counted = Counter(new_names)
			dupes = [k for k, v in counted.items() if v > 1]
			dupes_str = "\n".join(dupes)
			raise NameError(
				f"Can not rename, as new names collide with each other:\n"
				f"{dupes_str}"
			)
		assert not any([new in self.loaders for new in new_names]), "Can not rename, as new names collide with existing names"
		for loader in temp_loaders:
			loader.rename(name_tups)
		# recreate the loaders dict
		self.loaders = {loader.name: loader for loader in temp_loaders}
		self.send_files()
		logging.info(f"Renamed {len(new_names)} of {len(self.loaders)} files")

	def rename_contents(self, name_tups, only_files):
		if not only_files:
			only_files = list(self.loaders.keys())
		logging.info(f"Renaming contents for {self.format_rename_str(name_tups)} for {len(only_files)} selected files")
		with self.reporter.report_error_files("Renaming contents for") as error_files:
			for file_name in only_files:
				try:
					self.loaders[file_name].rename_content(name_tups)
				except:
					error_files.append(file_name)
		logging.info("Finished renaming contents!")

	def get_loaders_for_extract(self, only_names=(), only_types=()):
		_only_types = [s.lower() for s in only_types]
		_only_names = [s.lower() for s in only_names]
		for loader in self.loaders.values():
			# for batch operations, only export those that we need
			if _only_types and loader.ext not in _only_types:
				continue
			if _only_names and loader.name not in _only_names:
				continue
			# ignore types in the count that we export from inside other type exporters
			if loader.ext in self.formats_dict.ignore_types:
				continue
			yield loader

	def get_relative_extract_folder(self, src_folder, dst_folder):
		"""Return a relative path so that the ovl relative to dst_folder resembles a folder nested in src_folder"""
		rel_folder = os.path.relpath(self.path_no_ext, start=dst_folder)
		return os.path.join(src_folder, rel_folder)

	def extract_packs(self, out_dir, only_names=(), only_types=()):
		"""Write a raw pack per selected file, instead of extracting through the model.

		The counterpart to create_from_pack. Use this for anything the struct model does
		not fully describe: on PC2 every Characters/Staff motiongraph is 71-77% modelled,
		so the normal extract hands back an XML that silently omits a quarter of the
		relocations, and re-injecting it destroys them. A pack carries all of them.

		Named separately rather than folded into extract() behind a flag, because the two
		produce different things and the caller should say which one they want.
		"""
		out_dir_func = make_out_dir_func(out_dir)
		out_paths = []
		loaders = list(self.get_loaders_for_extract(only_names=only_names,
													only_types=only_types))
		logging.info(f"Writing {len(loaders)} raw packs")
		with self.reporter.report_error_files("Packing") as error_files:
			for loader in self.reporter.iter_progress(loaders, "Packing"):
				try:
					out_paths.append(loader.raw_extract(
						out_dir_func(loader.name + raw_pack.PACK_EXT)))
				except:
					logging.exception(f"Could not pack {loader.name}")
					error_files.append(loader.name)
		return out_paths

	def extract(self, out_dir, only_names=(), only_types=()):
		"""Extract the files, after all archives have been read"""

		out_dir_func = make_out_dir_func(out_dir)
		out_paths = []
		loaders_for_extract = list(self.get_loaders_for_extract(only_names=only_names, only_types=only_types))
		logging.info(f"Extracting {len(loaders_for_extract)} / {len(self.files)} files")
		with self.reporter.report_error_files("Extracting") as error_files:
			for loader in self.reporter.iter_progress(loaders_for_extract, "Extracting"):
				try:
					ret_paths = loader.extract(out_dir_func)
					ret_paths = loader.handle_paths(ret_paths)
					out_paths.extend(ret_paths)
				except:
					logging.exception(f"An exception occurred while extracting {loader.name}")
					error_files.append(loader.name)
		return out_paths

	def get_extract_paths(self, out_dir, only_names=(), only_types=()):
		"""Get the predicted paths of files to be extracted"""

		out_dir_func = make_out_dir_func(out_dir)
		out_paths = []
		for loader in self.get_loaders_for_extract(only_names=only_names, only_types=only_types):
			ret_paths = loader.get_extract_paths(out_dir_func)
			ret_paths = loader.handle_paths(ret_paths)
			out_paths.extend(ret_paths)
		return out_paths

	def create_file(self, file_path, file_name, ovs_name="STATIC"):
		"""Create a loader from a file path"""
		file_path = os.path.normpath(file_path)
		file_name = unescape_path(file_name.lower())
		_, ext = splitext_safe(file_name)
		logging.info(f"Creating {file_name} in {ovs_name}")
		if ext == raw_pack.PACK_EXT:
			return self.create_from_pack(file_path, ovs_name)
		try:
			loader = self.init_loader(file_name, ext, self.get_mime(ext, "version"))
			loader.get_constants_entry()
			loader.set_ovs(ovs_name)
			loader.create(file_path)
			loader.check_controlled_conflicts()
			return loader
		except NotImplementedError:
			logging.warning(f"Creation not implemented for {file_name}")
			raise
		except UserWarning:
			logging.warning(f"Did not create {file_name}")
			# don't raise as to not cause annoying error pop-ups
		except BaseException:
			logging.exception(f"Could not create: {file_name}")
			raise

	def create_from_pack(self, file_path, ovs_name="STATIC"):
		"""Slot a raw pack straight into this OVL, bypassing the struct model.

		A pack carries blocks and the relocation table taken from the pool link tables, so
		it round-trips content the model cannot describe. On PC2 that matters: every
		Characters/Staff motiongraph is only 71-77% modelled, so extracting one to XML and
		injecting it back silently drops a quarter of its relocations, while a pack carries
		all of them.

		The pack names the file it holds, so the loader is built from the pack rather than
		from the pack FILE's name - callers may rename it, and a pack for
		'bardaf.motiongraph' must still land as bardaf.motiongraph.
		"""
		exported = raw_pack.unpack(open(file_path, "rb").read())
		name = exported["name"]
		ext = exported["ext"]
		logging.info(f"Slotting raw pack for {name} into {ovs_name}")
		loader = self.init_loader(name, ext, exported.get("version") or 0)
		try:
			loader.get_constants_entry()
		except Exception:
			# a format with no mime entry on this game still packs and unpacks fine;
			# the pack carries pool_type and set_pool_type itself
			logging.debug(f"No mime constants for {ext}, using the pack's own")
		loader.set_ovs(exported.get("ovs_name") or ovs_name)
		loader.track_ptrs()
		loader.raw_import(exported)
		return loader

	def create(self, ovl_dir, commands={}):
		logging.info(f"Creating OVL from {ovl_dir}")
		# store commands
		self.commands = commands
		self.store_filepath(f"{ovl_dir}.ovl")
		self.create_archive(name="STATIC")
		self.loaders = {}
		self.add_files([os.path.join(ovl_dir, file_name) for file_name in os.listdir(ovl_dir)], common_root_dir=ovl_dir)
		self.load_included_ovls(os.path.join(ovl_dir, "ovls.include"))

	def add_files(self, file_paths, common_root_dir=None):
		logging.info(f"Adding {len(file_paths)} files to OVL [{self.game}]")
		logging.debug(file_paths)
		if not file_paths:
			return
		if not common_root_dir:
			# file_paths must be direct children of the same folder
			common_root_dir = os.path.dirname(file_paths[0])
		file_paths = {os.path.normpath(file_path) for file_path in file_paths}
		inject_paths = set()
		# process the children of root
		for fp in file_paths:
			# files can be added directly
			if os.path.isfile(fp):
				inject_paths.add(fp)
			# get all files in subfolders of a dir and add them
			elif os.path.isdir(fp):
				subfolder = os.path.basename(fp)
				if subfolder in ("backups",) or subfolder[0] in (".", "_"):
					logging.info(f"Ignoring subfolder {subfolder}")
					continue
				for root, dirs, files in os.walk(fp, topdown=False):
					for name in files:
						inject_paths.add(os.path.join(root, name))
		with self.reporter.report_error_files("Adding") as error_files:
			# sorted: inject_paths is a set of STRINGS, and this iteration order is
			# the order files become loaders, which is the order they land in pools
			# Python randomises string hashing per process, so leaving it unsorted
			# made every build non-reproducible - identical input produced 57983 /
			# 57993 / 58003 bytes across three runs. Any order is functionally
			# valid, so sorting costs nothing and makes builds byte-stable across
			# machines, Python versions and platforms
			for file_path in self.reporter.iter_progress(sorted(inject_paths), "Adding files"):
				# ensure lowercase, especially for file extension checks
				bare_path, ext = splitext_safe(file_path.lower())
				# ignore dirs, links etc.
				if not os.path.isfile(file_path):
					continue
				if ext in self.formats_dict.ignore_types:
					logging.debug(f"Ignoring {file_path}")
					continue
				# endswith, NOT "stream" in ext: the streamed buffer formats all END in
				# "stream" (.texturestream, .model2stream, .landscapestream), but a
				# substring test also swallows .dataSTREAMS, which is a normal
				# MemStruct format and the only way retail attaches effects to a clip
				# That made .datastreams silently un-injectable - add_files dropped it
				# with a debug log while create_file handled it fine
				elif ext.endswith("stream"):
					logging.debug(f"Ignoring {file_path} as it will be created from its streamer")
					continue
				elif ext in (".include", ):
					# not for a loader, will be dealt with separately by create
					continue
				elif ext in (".png", ".dds"):
					# find and remove any suffices in png basepath
					channel_re = re.compile(r"_[rgba]+$")
					array_re = re.compile(r"_\[[0-9]+\]$")
					bare_path_no_suffices = f"{array_re.sub('', channel_re.sub('', bare_path, count=1), count=1)}.tex"
					lower_tex_paths = {fp.lower() for fp in inject_paths if fp.lower().endswith(".tex")}
					# compare this reconstructed tex path to the other file paths (case-insensitive)
					if bare_path_no_suffices in lower_tex_paths:
						logging.info(f"Ignoring {file_path} as matching .tex file is also selected")
						continue
					else:
						logging.error(f"Inject the corresponding .tex file for {file_path}")
						error_files.append(file_path)
						continue
				# A raw pack names the format it holds INSIDE itself, so it never has a mime
				# entry of its own and the check below would silently skip it - which it
				# did, dropping both of Bard's motiongraphs without a word
				elif ext == raw_pack.PACK_EXT:
					pass
				# no loader exists, check if it should warn about missing loader or just ignore it
				elif ext not in self.formats_dict:
					# test if this ext is a cobra file format by querying its mime version
					try:
						self.get_mime(ext, "version")
					except:
						logging.debug(f"Ignoring {file_path} - not a cobra format")
						continue
				# make relative to the common root, use forward slash as separator
				file_name = os.path.relpath(file_path, common_root_dir).replace("\\", "/")
				try:
					loader = self.create_file(file_path, file_name)
					self.register_loader(loader)
				except:
					error_files.append(file_path)
			self.validate_loaders()
		self.send_files()

	def send_files(self):
		f_list = [[loader.name, loader.ext, "\n".join(l.name for l in loader.children)] for loader in self.loaders.values()]
		f_list.sort(key=lambda t: (t[1], t[0]))
		self.reporter.files_list.emit(f_list)

	def register_loader(self, loader):
		"""register the loader, and delete any existing loader if needed"""
		if loader:
			# check if this file exists in this ovl, if so, first delete old loader
			if loader.name in self.loaders:
				old_loader = self.loaders[loader.name]
				old_loader.remove()
			# only store loader in self.loaders after successful create
			self.loaders[loader.name] = loader
			# also store any streams created by loader
			for stream in loader.controlled_loaders:
				self.loaders[stream.name] = stream

	def create_archive(self, name="STATIC"):
		# see if it exists
		for archive in self.archives:
			if archive.name == name:
				return archive.content
		# nope, gotta create it
		logging.debug(f"Creating archive {name}")
		archive = ArchiveEntry(self.context)
		archive.name = name
		self.archives.append(archive)
		content = OvsFile(self.context, self, archive)
		archive.content = content
		return content

	def store_filepath(self, filepath):
		# store file name for later
		self.filepath = filepath
		self.dir, self.name = os.path.split(filepath)
		self.basename, self.ext = os.path.splitext(self.name)
		self.path_no_ext = os.path.splitext(self.filepath)[0]

	def load_included_ovls(self, path):
		self.included_ovl_names.clear()
		if os.path.isfile(path):
			with open(path) as f:
				self.included_ovl_names = [line.strip() for line in f.readlines() if line.strip()]
		self.reporter.included_ovls_list.emit(self.included_ovl_names)

	def save_included_ovls(self, path):
		with open(path, "w") as f:
			for ovl_name in self.included_ovl_names:
				f.write(f"{ovl_name}\n")

	def load_hash_table(self):
		with self.reporter.log_duration("Loading constants"):
			self.constants = ConstantsProvider()

	def get_mime(self, ext, key):
		game = self.game
		if game in self.constants:
			game_lut = self.constants[game]
			if ext in game_lut["mimes"]:
				mime = game_lut["mimes"][ext]
				return getattr(mime, key)
			else:
				raise NotImplementedError(f"Unsupported extension {ext} in game {game}")
		else:
			raise NotImplementedError(f"Unsupported game {game}")

	def load(self, filepath, commands={}):
		# store commands
		self.commands = commands
		# automatically tag JWE2 dev build
		self.is_dev = 1 if "Jurassic World Evolution 2 1.3.1.0" in filepath else 0
		# pick game version from commands preset before reading anything
		preset_game = commands.get("game", None)
		if preset_game:
			self._game = preset_game
			logging.info(f"Game: {self.game}")
		else:
			logging.warning(f"No game preset was supplied")
		self.store_filepath(filepath)
		with self.reporter.log_duration(f"Loading {self.name}"):
			with open(filepath, "rb") as stream:
				magic = stream.read(4)
				stream.seek(0)
				if magic == b"FRES":
					pass
				elif magic == b"FREA":
					raise AttributeError(f"{self.name} is encrypted and cannot be read")
				else:
					raise AttributeError(f"Unknown OVL magic for {self.name}, cannot be read")
				self.read_fields(stream, self)
				self.eof = stream.tell()
			# verify preset game after reading context
			if preset_game:
				qualified_games = [game.value for game in get_game(self)]
				if preset_game not in qualified_games:
					logging.warning(f"Preset '{preset_game}' did not match any qualified game from {qualified_games}")

			self.mimes_name = [self.names.get_str_at(i) for i in self.mimes["name"]]
			# without leading . to avoid collisions on cases like JWE island.island
			self.mimes_ext = [name.split(':')[-1] if name else "" for name in self.mimes_name]
			# maps djb2 hash to string
			# store mime extension hash so we can use it
			self.hash_table_local = {djb2(ext): ext for ext in self.mimes_ext}

			if "triplet_offset" in self.mimes.dtype.fields:
				self.mimes_triplets = [self.triplets[o: o+c] for o, c in zip(
					self.mimes["triplet_offset"], self.mimes["triplet_count"])]
			else:
				self.mimes_triplets = []
			# add file name to hash dict; ignoring the extension pointer
			self.files_basename = [self.names.get_str_at(i) for i in self.files["basename"]]
			self.files_ext = [f".{self.mimes_ext[i]}" for i in self.files["extension"]]
			# remove any quotation chars, eg. homalocephale@huntkilledcarnotaurusin_srb".wsm
			self.files_name = [f"{b}{e}".replace('"', "") for b, e in zip(self.files_basename, self.files_ext)]
			# hash collisions are actually from shared basenames, so seem to be harmless
			# if len(self.files_basename) != len(set(self.files["file_hash"])):
			# 	logging.warning("There are djb2 hash collisions, some files will not load")
			# 	hash_counter = Counter(self.files["file_hash"])
			# 	for name, djb_hash in zip(self.files_name, self.files["file_hash"]):
			# 		if hash_counter[djb_hash] > 1:
			# 			# e.g. albertosaurus_juvenile.mdl2 [3060300434] and albertosaurus_juvenile.fgm [3060300434]
			# 			logging.warning(f"Djb2 hash collision for {name} [{djb_hash}]")
			dependencies_ext = [self.names.get_str_at(i).replace(":", ".") for i in self.dependencies["ext_raw"]]
			self.hash_table_local.update({h: b for b, h in zip(self.files_basename, self.files["file_hash"])})

			self.loaders = {}
			if "only_types" in self.commands:
				if not (ext for ext in self.commands['only_types'] if ext in self.files_ext):
					logging.info(f"OVL does not contain requested formats, skipping")
					return
			if "generate_hash_table" in self.commands:
				deps_exts = self.commands["generate_hash_table"]
				filtered_hash_table = {h: basename for h, basename, ext in zip(
					self.files["file_hash"], self.files_basename, self.files_ext) if ext in deps_exts}
				return filtered_hash_table, set(dependencies_ext)
			elif "generate_names" in self.commands:
				return self.files_name
			else:
				self.mimes_version = self.mimes["mime_version"]
				files_version = [self.mimes_version[i] for i in self.files["extension"]]
				# initialize the loaders right here
				for filename, ext, version, pt, set_pt in zip(self.files_name, self.files_ext, files_version, self.files["pool_type"], self.files["set_pool_type"]):
					loader = self.init_loader(filename, ext, version)
					loader.pool_type = pt
					loader.set_pool_type = set_pt
					self.loaders[filename] = loader

			# get included ovls
			self.included_ovl_names = [self.names.get_str_at(i) for i in self.included_ovls["basename"]]
			self.reporter.included_ovls_list.emit(self.included_ovl_names)

			self.dependencies_name = [self.get_dep_name(h, ext, self.files_name[f_i]) for h, ext, f_i in zip(
				self.dependencies["file_hash"], dependencies_ext, self.dependencies["file_index"])]

			aux_suffices = [self.names.get_str_at(i) for i in self.aux_entries["basename"]]
			for f_i, aux_size, aux_suffix in zip(self.aux_entries["file_index"], self.aux_entries["size"], aux_suffices):
				file_name = self.files_name[f_i]
				self.loaders[file_name].open_aux_readers(aux_suffix, aux_size)
			self.load_archives()
			if self.commands.get("update_aux", True):
				for loader in self.get_loaders_with_aux():
					loader.close_aux_handles()

	def get_dep_name(self, h, ext, using_file):
		"""Gets the name for a dependency identified by hash h"""
		if h in self.hash_table_local:
			return self.hash_table_local[h]+ext
		else:
			game = self.game
			fallback = f"{UNK_HASH}_{h}"
			if game in self.constants:
				game_lut = self.constants[game]
				if h in game_lut["hashes"]:
					return game_lut["hashes"][h]+ext
			logging.warning(f"{using_file} can't find the original name of {fallback}{ext}", extra={
				"details": (
					"An unknown hash means the tools cannot ascertain the original filename.\n\n"
					f"This means {using_file} likely will not work correctly without editing "
					"it to fix the unknown hash."
				)})
			return fallback+ext

	def load_archives(self):
		with self.reporter.log_duration("Loading archives"):
			with self.open_streams(mode="rb") as streams:
				for archive_entry in self.reporter.iter_progress(self.archives, "Reading archives"):
					# those point to external ovs archives
					if archive_entry.name == "STATIC":
						read_start = self.eof
					else:
						read_start = archive_entry.read_start
					stream = streams[archive_entry.ovs_path]
					stream.seek(read_start)
					archive_entry.content = OvsFile(self.context, self, archive_entry)
					archive_entry.content.load(archive_entry, stream)
			# logging.info(self.archives_meta)
			self.send_files()
			self.load_flattened_pools()
			self.load_pointers()

	def load_flattened_pools(self):
		"""Create flattened list of ovl.pools from all ovs.pools"""
		self.pools = [None for _ in range(self.num_pools)]
		for archive in self.archives:
			assert len(archive.content.pools) == archive.num_pools
			self.pools[archive.pools_offset: archive.pools_offset + archive.num_pools] = archive.content.pools

	def load_pointers(self):
		"""Handle all pointers of this file, including dependencies, fragments and root_entry entries"""
		with self.reporter.log_duration("Loading pointers"):
			version = self.version
			# reset pointer map for each pool
			for i, pool in enumerate(self.pools):
				pool.clear_data()
				pool.i = i
			logging.debug("Linking pointers to pools")
			for n, f_i, l_i, l_o in zip(
						self.dependencies_name,
						self.dependencies["file_index"],
						self.dependencies["link_ptr"]["pool_index"],
						self.dependencies["link_ptr"]["data_offset"]):
				file_name = self.files_name[f_i]
				pool = self.pools[l_i]
				# self.loaders[file_name].dependencies[n] = (pool, l_o)
				self.loaders[file_name].dependencies.append((n, (pool, l_o)))
				# the index goes into the flattened list of ovl pools
				pool.offset_2_link[l_o] = n
			# this loop is extremely costly in JWE2 c0 main.ovl, about 145 s
			for archive in self.reporter.iter_progress(self.archives, "Sorting pointers"):
				try:
					ovs = archive.content
					# attach all pointers to their pool
					for n, s_i, s_o, in zip(
							ovs.root_entries_name,
							ovs.root_entries["struct_ptr"]["pool_index"],
							ovs.root_entries["struct_ptr"]["data_offset"]):
						loader = self.loaders[n]
						loader.ovs = ovs
						# may not have a pool
						if s_i != -1:
							s_pool = ovs.pools[s_i]
							s_pool.offsets.add(s_o)
							loader.root_ptr = (s_pool, s_o)
					# vectorized like this, it takes virtually no time
					for l_i, l_o, s_i, s_o, in ovs.fragments:
						s_pool = ovs.pools[s_i]
						# replace offsets pointing to end of pool with None
						if s_pool.size != s_o:
							s_pool.offsets.add(s_o)
						else:
							s_o = None
						ovs.pools[l_i].offset_2_link[l_o] = (s_pool, s_o)
				except:
					logging.exception(f"Could not load pointers for {archive.name} - something went wrong before")
			logging.debug("Calculating pointer sizes")
			for pool in self.pools:
				pool.calc_size_map()
				# Remember which structs came out of the file, before any loader has
				# collected and before any create() can add more. rebuild_pools needs
				# this to tell two states apart that otherwise look identical: a pool
				# holding source structs nobody claims, which is ordinary (the same
				# assets already log "Could not map N fragments", 21.7% of PC2 assets
				# containing a .motiongraph), and a pool a half-finished create() left
				# behind, which is corrupt
				pool.offsets_from_file = set(pool.offsets)

		with self.reporter.log_duration("Loading file loaders"):
			if "only_types" in self.commands:
				only_types = self.commands['only_types']
				logging.info(f"Loading only {only_types}")
				self.loaders = {loader.name: loader for loader in self.loaders.values() if loader.ext in only_types}
			with self.reporter.report_error_files("Collecting") as error_files:
				for loader in self.reporter.iter_progress(self.loaders.values(), "Mapping files"):
					loader.track_ptrs()
					try:
						loader.collect()
					except:
						logging.exception(f"Collecting {loader.name} errored")
						error_files.append(loader.name)
						# we can keep collecting
					loader.link_streams()
					# if somebody stores a field called 'version', it overrides (ovl) context version
					if version != self.version:
						raise AttributeError(f"{loader.name} changed ovl version from {version} to {self.version}")
				self.validate_loaders()
		# no point in validating fragments on partially loaded ovls
		if not self.commands.get('only_types', ()):
			self.validate_fragments()

	def validate_fragments(self):
		archive_name_to_loaders = self.get_archive_name_to_loaders(self.loaders.values())
		for archive in self.reporter.iter_progress(self.archives, "Updating headers"):
			ovs = archive.content
			loaders = archive_name_to_loaders[archive.name]
			all_frags = set()

			def get_f(i, o):
				p = ovs.pools[i]
				# if o is None:
				# 	o = p.get_size()
				if o == p.size:
					o = None
				return p, o
			# convert to set to validate that the extras are not duplicates
			new_f = set((get_f(l_i, l_o), get_f(s_i, s_o)) for l_i, l_o, s_i, s_o, in ovs.fragments)
			assert len(new_f) == archive.num_fragments
			for loader in loaders:
				all_frags.update(loader.fragments)
			ovs.uncaught_fragments = all_frags.symmetric_difference(new_f)
			if ovs.uncaught_fragments:
				# print(archive.num_fragments, len(new_f), len(all_frags), len(all_frags.union(new_f)))
				# print(ovs.uncaught_fragments)
				logging.warning(f"Could not map {len(ovs.uncaught_fragments)} fragments in {archive.name}, storing them for saving")

	def get_archive_name_to_loaders(self, flat_sorted_loaders):
		archive_name_to_loaders = {archive.name: [] for archive in self.archives}
		for loader in flat_sorted_loaders:
			archive_name_to_loaders[loader.ovs_name].append(loader)
		return archive_name_to_loaders

	def validate_loaders(self):
		with self.reporter.report_error_files("Validating") as error_files:
			for loader in tuple(self.loaders.values()):
				try:
					loader.validate()
					if not loader.ovs_name:
						self.loaders.pop(loader.name)
						logging.warning(f"Deleted {loader.name} as it was not found in any OVS, possibly due to hash collision")
				except:
					logging.exception(f"Validating '{loader.name}' failed")
					error_files.append(loader.name)

	def get_ovs_path(self, archive_entry):
		if archive_entry.name == "STATIC":
			archive_entry.ovs_path = self.filepath
		else:
			# JWE style
			# note that for some reason self.is_dev is set to True by default
			if is_jwe(self) or is_jwe2(self) or is_jwe2dev(self):
				archive_entry.ovs_path = f"{self.path_no_ext}.ovs.{archive_entry.name.lower()}"
			# DLA, PZ, PC, ZTUAC Style
			else:
				archive_entry.ovs_path = f"{self.path_no_ext}.ovs"

	@staticmethod
	def get_dep_hash(name):
		if UNK_HASH in name:
			logging.warning(f"Won't update hash {name}")
			return int(name.replace(f"{UNK_HASH}_", ""))
		elif UNK_HASH.lower() in name:
			logging.warning(f"Won't update hash {name}")
			return int(name.replace(f"{UNK_HASH.lower()}_", ""))
		return djb2(name)

	def compact_pools(self):
		"""Reclaim pool bytes that nothing points at any more.

		Rewriting an entry appends the new copy and drops the old blocks from
		their pool, which would otherwise leave those bytes in the file with
		nothing referencing them. Must run before anything captures a pointer.
		"""
		remap = {}
		for archive in self.archives:
			for pool in archive.content.pools:
				remap.update(pool.compact())
		if not remap:
			return
		logging.info(f"Compacted pools, moved {len(remap)} structs")
		for archive in self.archives:
			ovs = archive.content
			for pool in ovs.pools:
				for l_offset, entry in pool.offset_2_link.items():
					if isinstance(entry, tuple):
						pool.offset_2_link[l_offset] = remap.get(entry, entry)
			if getattr(ovs, "uncaught_fragments", None):
				ovs.uncaught_fragments = {
					(remap.get(l, l), remap.get(s, s)) for l, s in ovs.uncaught_fragments}
			for pool in ovs.pools:
				# the load-time struct positions move with everything else, and
				# rebuild_pools compares against them to tell a preserved pool from a
				# corrupt one. Leaving them stale makes every compacted pool look like
				# it holds structs that never came from the file
				from_file = getattr(pool, "offsets_from_file", None)
				if from_file is not None:
					pool.offsets_from_file = {
						remap.get((pool, o), (pool, o))[1] for o in from_file}
		for loader in self.loaders.values():
			loader.remap_ptrs(remap)

	def rebuild_ovl_arrays(self, update_aux):
		"""Call this if any file names have changed and hashes or indices have to be recomputed"""

		# Remember the names buffer as it was read, before update_strings replaces it
		# Planet Coaster's order is not alphabetical and is reproducible by no sort of
		# the mimes' own fields, so it can only be written back by remembering it
		# Decoded permissively: a name that fails to round-trip simply won't match and
		# falls back to the sort, which is the current behaviour
		# Keep EMPTY entries. An aux suffix is often the empty string, a bare NUL in the
		# buffer, and it holds a real position. Filtering empties out made it unfindable,
		# so the aux segment scored as "not present in the source" and sorted last --
		# which reads as "moving the aux segment breaks PC2" (51/60 -> 16/60) when the
		# real fault is that its position was never recorded. Only trailing padding NULs
		# are dropped
		try:
			parts = self.names.data.split(b"\x00")
			while parts and not parts[-1]:
				parts.pop()
			# Gated: version 18 measured worse with empties kept (119/120 -> 110/120),
			# version 20 needs them because its aux suffix IS the empty string and that
			# position is real. Measured both ways on both titles rather than assumed
			if self.version < 19:
				parts = [n for n in parts if n]
			self.source_names_order = [n.decode("latin-1") for n in parts]
		except Exception:
			self.source_names_order = []
		self.compact_pools()
		# clear ovl lists
		loaders_with_deps = [loader for loader in self.loaders.values() if loader.dependencies]
		loaders_with_aux = self.get_loaders_with_aux()
		if update_aux:
			for loader in loaders_with_aux:
				loader.close_aux_handles()
			# write_aux_data APPENDS, so this loop order IS the aux file's layout. Sorting
			# by name matches retail for most PC2 textures and not all -- the note this
			# replaces already said "the rest is idiosyncratic" -- and where it does not,
			# the aux comes out the same length with its middle rearranged AND the mip
			# offsets recorded in the archive shift, which is the same defect seen from
			# two sides (tickets 36 and 37)
			# The source layout is still readable here: flush_to_aux has not run yet, so
			# each loader's mip offsets still hold where retail put that texture. Order by
			# that and the file is rebuilt as it was, with name order as the tiebreak for
			# loaders that have no remembered offset (new content, or non-texture aux)
			for loader_name, loader in sorted(self.loaders.items(),
											  key=lambda kv: (aux_layout_key(kv[1]), kv[0])):
				loader.flush_to_aux()
			for loader in loaders_with_aux:
				loader.delete_unused()
		# else:
		# 	self.loaders[file_name].open_aux_readers(aux_suffix, aux_size)

		loaders_with_aux = self.get_loaders_with_aux()

		# update file hashes and extend entries per loader
		loaders_by_extension = self.get_loaders_by_ext()
		mimes_ext = sorted(loaders_by_extension)
		mimes_triplets = [self.get_mime(ext, "triplets") for ext in mimes_ext]
		mimes_name = [self.get_mime(ext, "name") for ext in mimes_ext]
		# flat list of all dependencies
		loaders_and_deps = [((dep, ptr), loader) for loader in loaders_with_deps for dep, ptr in loader.dependencies]
		loaders_and_aux = [(aux_suffix, loader) for loader in loaders_with_aux for aux_suffix in loader.aux_handles]
		ovl_includes = sorted(set(self.included_ovl_names))
		ovl_includes = [ovl_path.replace(".ovl", "") for ovl_path in ovl_includes]

		self.num_dependencies = len(loaders_and_deps)
		self.num_files = self.num_files_2 = self.num_files_3 = len(self.loaders.values())
		self.num_mimes = len(loaders_by_extension)
		self.num_triplets = sum(len(trip) for trip in mimes_triplets)
		self.num_included_ovls = len(ovl_includes)
		self.num_aux_entries = len(loaders_and_aux)
		# remember the dependency order read from the file, before reset wipes it
		try:
			prev_dependencies = self.dependencies.tolist()
		except Exception:
			prev_dependencies = []
		self.reset_field("mimes")
		self.reset_field("dependencies")
		self.reset_field("files")
		self.reset_field("triplets")
		self.reset_field("included_ovls")
		self.reset_field("aux_entries")
		# print(loaders_and_deps)
		if loaders_and_deps:
			deps_basename, deps_ext = zip(*[splitext_safe(dep.lower()) for (dep, ptr), loader in loaders_and_deps])
		else:
			deps_basename = deps_ext = ()
		deps_ext = [ext.replace(".", ":") for ext in deps_ext]
		aux_suffices = [aux_suffix for aux_suffix, loader in loaders_and_aux]
		# Order each segment the way the source file had it. Retail's segment structure
		# already matches what is built here (aux suffixes, dependency extensions, mime
		# names, then basenames), so this only replaces the sort *within* a segment and
		# leaves genuinely new names in sorted order behind the remembered ones
		# Restore the source order across the dependency-extension, mime and basename
		# segments together, because retail's segment order is not fixed: most assets
		# lead with the dependency extension, but some put a mime name at offset 0 and
		# the extension later (PC1 VT_Coaster_Door_Clownface has ':tex' at 27)
		# aux_suffices is pinned first only on the v20 path, where it is one segment among
		# four and free to swap places with the others. v18 has no segments to swap, so
		# pinning there was not a constraint but a discarded measurement; see below
		# mimes["name"] and dependencies["ext_raw"] are offsets into this buffer, so the
		# order is what makes those fields right
		# Order the SEGMENTS by where the source put them, and each segment internally by
		# the source too. Retail does not keep a fixed segment order: PC2 _testTones_Media
		# ships the mime "FGDK:AudioBank:bnk" AHEAD of the aux suffix "S", which pinning
		# aux_suffices first cannot produce, and 38 PC2 assets diverge at exactly byte 144
		# for that reason
		# Ordering the names globally instead does express it, but destroys segment
		# integrity and took PC2 from 51/60 to 16/60. (Neither name matching nor duplicate
		# handling explained that: matching is 1142/1142 on PC2, and deduping changed
		# nothing.) Sorting whole segments keeps each one contiguous while still letting
		# two of them swap, which is the only thing retail actually varies here
		remembered = getattr(self, "source_names_order", None) or []
		rank = {n: i for i, n in enumerate(remembered)}

		def in_source_order(items):
			return order_like_source(sorted(items), remembered, lambda s: s, lambda s: s)

		def segment_rank(seg):
			"""Where this segment started in the source; unknown segments keep their place."""
			seen = [rank[n] for n in seg if n in rank]
			return min(seen) if seen else float("inf")

		segments = [
			list(aux_suffices),
			in_source_order(set(deps_ext)),
			in_source_order(mimes_name + ovl_includes),
			in_source_order(loader.basename for loader in self.loaders.values()),
		]
		# stable sort, so segments with no remembered position hold their existing order
		# Version-gated, and measured rather than assumed. Retail's mime order at v20 IS
		# alphabetical by full name (PC2 41/41, PZ 32/32) while v18 uses a fixed engine
		# order (PC1 0/33), so the two versions want different things here and the
		# measurements agree: segment reordering plus the empty-name position gains PC2
		# (51/60 -> 56/60) and costs PC1 (119/120 -> 110/120). Apply it only where it was
		# shown to help
		if self.version >= 19:
			names_list = [n for seg in sorted(segments, key=segment_rank) for n in seg]
		else:
			# v18 wants the three non-aux segments ordered as ONE list, so they can
			# interleave; forcing each to stay contiguous costs it 119/120 -> 110/120
			# v20 wants the opposite: segments contiguous but free to swap places
			# aux_suffices joins that list instead of being pinned ahead of it. Pinning
			# threw away the position source_names_order already recorded, and retail
			# does not always lead with it: PC1 writes the mime "FGDK:AudioBank:bnk"
			# before the aux suffix "B", exactly as PC2 _testTones_Media does at v20
			# All 138 PC1 assets carrying a .bnk differed for this, 137 at byte 144
			# Duplicates need no handling here: two bnk files make aux_suffices
			# ['B', 'B'], but update_strings already keeps the first occurrence only
			names_list = [
				*order_like_source(
					[*sorted(set(aux_suffices)),
					 *sorted(set(deps_ext)),
					 *sorted(mimes_name + ovl_includes),
					 *sorted(loader.basename for loader in self.loaders.values())],
					remembered, lambda s: s, lambda s: s)]
		self.names.update_strings(names_list)
		# create the mimes
		file_offset = 0
		triplet_offset = 0
		self.mimes["name"] = [self.names.offset_dic[name] for name in mimes_name]
		self.mimes["mime_version"] = [self.get_mime(ext, "version") for ext in mimes_ext]
		if self.context.version >= 18:
			self.mimes["mime_hash"] = [self.get_mime(ext, "hash") for ext in mimes_ext]
		for i, (mime, name, ext, triplets,) in enumerate(
				zip(self.mimes, mimes_name, mimes_ext, mimes_triplets)):
			mime.name = self.names.offset_dic[name]
			if self.context.version >= 20:
				mime.triplet_offset = triplet_offset
				mime.triplet_count = len(triplets)
			self.triplets[triplet_offset: triplet_offset+len(triplets)] = triplets
			# get the loaders using this ext
			loaders = loaders_by_extension[ext]
			mime.file_index_offset = file_offset
			mime.file_count = len(loaders)
			# take all files for this mime
			files = self.files[file_offset: file_offset+len(loaders)]
			# sort this mime's loaders by hash
			loaders.sort(key=lambda x: x.file_hash)
			# variable per loader
			files["basename"] = [self.names.offset_dic[loader.basename] for loader in loaders]
			files["file_hash"] = [loader.file_hash for loader in loaders]
			# shared by all loaders using this mime
			files["extension"] = i
			files["pool_type"] = loaders[0].pool_type
			files["set_pool_type"] = loaders[0].set_pool_type
			file_offset += len(loaders)
			triplet_offset += len(triplets)
		self.len_names = len(self.names.data)
		# catching ovl files without entries, default len_type_names is 0
		if self.loaders:
			self.len_type_names = min(self.files["basename"])
		else:
			self.len_type_names = 0

		flat_sorted_loaders = []
		for ext in mimes_ext:
			flat_sorted_loaders.extend(loaders_by_extension[ext])
		for i, loader in enumerate(flat_sorted_loaders):
			loader.file_index = i
		ext_lut = {ext: i for i, ext in enumerate(mimes_ext)}

		# update all pools before indexing anything that points into pools
		pools_offset = 0
		self.archives.sort(key=lambda a: a.name)
		for archive in self.reporter.iter_progress(self.archives, "Rebuilding pools"):
			ovs = archive.content
			# remember the order this archive was read with, before clearing wipes it
			ovs.source_buffer_order = [buffer_identity(b) for b in ovs.buffer_entries]
			ovs.clear_ovs_arrays()
			ovs.rebuild_pools()
			archive.pools_offset = pools_offset
			pools_offset += archive.num_pools
		self.num_pools = sum(a.num_pools for a in self.archives)
		self.num_pool_groups = sum(a.num_pool_groups for a in self.archives)
		# apply the new pools to the ovl
		self.load_flattened_pools()

		ptrs = [ptr for (dep, ptr), loader in loaders_and_deps]
		pools_lut = {pool: i for i, pool in enumerate(self.pools)}
		self.dependencies["file_hash"] = [self.get_dep_hash(name) for name in deps_basename]
		self.dependencies["ext_raw"] = [self.names.offset_dic[name] for name in deps_ext]
		self.dependencies["file_index"] = [loader.file_index for (dep, ptr), loader in loaders_and_deps]
		self.dependencies["link_ptr"] = [(pools_lut[pool], offset) for pool, offset in ptrs]
		self.dependencies.sort()  # contributions: src file hash, ext (?), target file, link_ptr
		# ...but Planet Coaster does not ship that sort order: 9 of 18 sampled archives
		# came out reordered with an identical row set. Put the source order back, after
		# the sort rather than before it -- the sort runs last and silently undid an
		# earlier attempt. Identity is file_hash + file_index only, because ext_raw is an
		# offset into the names buffer and legitimately moves; we want the new offsets in
		# the old order, not the old row. The sort survives as the tiebreak for rows the
		# source did not have, and for a file built from scratch nothing is remembered
		if prev_dependencies and len(self.dependencies):
			_fields = self.dependencies.dtype.names
			_at = [_fields.index(f) for f in ("file_hash", "file_index") if f in _fields]
			def dep_identity(row, _at=_at):
				return tuple(row[i] for i in _at)
			self.dependencies[:] = order_like_source(
				self.dependencies.tolist(), [dep_identity(r) for r in prev_dependencies],
				lambda r: r, dep_identity)

		self.included_ovls["basename"] = [self.names.offset_dic[name] for name in ovl_includes]

		self.aux_entries["file_index"] = [loader.file_index for aux_suffix, loader in loaders_and_aux]
		self.aux_entries["basename"] = [self.names.offset_dic[name] for name in aux_suffices]
		# Flush first. get_aux_size stats the file on disk, and flush_to_aux above may
		# still be holding an open buffered writer, so the stat can land mid-flush and
		# report a block-aligned prefix of the real size. Measured on PC2
		# FaunaSharedMaterials: the aux is 51712 bytes and byte-identical afterwards, yet
		# the header recorded 50176 (98 * 512, i.e. a partial flush). The comment in
		# get_aux_size claims the stat is "independent of flushing the aux"; it is exactly
		# dependent on it
		for aux_suffix, loader in loaders_and_aux:
			handle = loader.aux_handles.get(aux_suffix)
			if handle is not None and not handle.closed and handle.writable():
				handle.flush()
		self.aux_entries["size"] = [loader.get_aux_size(aux_suffix) for aux_suffix, loader in loaders_and_aux]

		if update_aux:
			for loader in loaders_with_aux:
				loader.close_aux_handles()
		self.rebuild_ovs_arrays(flat_sorted_loaders, ext_lut)

	def get_loaders_with_aux(self):
		return [loader for loader in self.loaders.values() if loader.aux_handles]
	
	def get_loaders_by_ext(self):
		"""Return dict of all extensions and the loaders that use them"""
		loaders_by_extension = {}
		for loader in self.loaders.values():
			# force an update on the loader's data for older versions' data
			# and link entries like bani to banis
			loader.update()
			if loader.ext not in loaders_by_extension:
				loaders_by_extension[loader.ext] = []
			loaders_by_extension[loader.ext].append(loader)
		return loaders_by_extension

	def rebuild_ovs_arrays(self, flat_sorted_loaders, ext_lut):
		"""Produces valid ovl.pools and ovs.pools and valid links for everything that points to them"""
		try:
			logging.debug(f"Sorting pools by type and updating pool groups")

			archive_name_to_loaders = self.get_archive_name_to_loaders(flat_sorted_loaders)
			# remove all entries to rebuild them from the loaders
			for archive in self.reporter.iter_progress(self.archives, "Updating headers"):
				ovs = archive.content
				loaders = archive_name_to_loaders[archive.name]
				archive.num_root_entries = len(loaders)
				all_frags = set()
				if hasattr(ovs, "uncaught_fragments") and ovs.uncaught_fragments:
					# Only those still pointing at a live pool. rebuild_pools has already
					# run and drops any pool left with no offsets, which is what happens
					# to the pools a rewritten loader used to own. A fragment into a
					# dropped pool cannot be expressed: resolve() below turns it into
					# index -1 rather than failing, and two of those collapse onto the
					# same row, so num_fragments (counted here, on pool OBJECTS) exceeds
					# the distinct rows actually written and validate_fragments' assert
					# fires on reload. Bard.ovl stranded 14 this way, 6 of them as
					# (-1, 0), when its motiongraphs were rewritten
					# Dropping is right: either whoever rewrote the pool re-registered
					# these links, or they point at content that no longer exists
					live = {id(pool) for pool in ovs.pools}
					kept = {(l, s) for l, s in ovs.uncaught_fragments
							if id(l[0]) in live and id(s[0]) in live}
					stranded = len(ovs.uncaught_fragments) - len(kept)
					logging.warning(f"Restoring {len(kept)} uncaught fragments to {archive.name}")
					if stranded:
						logging.warning(
							f"Dropped {stranded} uncaught fragments in {archive.name} whose "
							f"pool did not survive the rebuild")
					all_frags.update(kept)
				for i, loader in enumerate(loaders):
					all_frags.update(loader.fragments)
					loader.root_index = i
				archive.num_fragments = len(all_frags)
				# snapshot the order this archive was read with, before reset wipes it
				prev_fragments = [tuple(int(v) for v in row) for row in ovs.fragments]
				ovs.reset_field("root_entries")
				ovs.reset_field("fragments")
				# create lut for pool indices
				pools_lut = {pool: i for i, pool in enumerate(ovs.pools)}

				def resolve(pool, offset):
					# pools are already padded, and pool.size is set
					# pool is either None or part of pools_lut
					# offset is either None or an int
					if offset is None:
						assert pool
						offset = pool.size
					return pools_lut.get(pool, -1), offset

				if all_frags:
					rows = [(*resolve(p_pool, l_o), *resolve(s_pool, s_o)) for (p_pool, l_o), (s_pool, s_o) in all_frags]
					# all_frags is a set, so there is no insertion order to fall back on;
					# the old sort is kept as the tiebreak for rows the source didn't have
					ovs.fragments[:] = order_like_source(
						rows, prev_fragments,
						lambda r: (r[0], r[2], r[1], r[3]))
				# get root entries; not all ovs have root entries - some JWE2 ovs just have data
				if loaders:
					root_ptrs = [loader.root_ptr for loader in loaders]
					ovs.root_entries["struct_ptr"]["pool_index"], \
					ovs.root_entries["struct_ptr"]["data_offset"] = zip(*[resolve(s_pool, s_o) for s_pool, s_o in root_ptrs])
					ovs.assign_ids(ovs.root_entries, loaders)

				logging.info(f"Updating assets for {archive.name}")
				loaders_with_children = [loader for loader in loaders if loader.children]
				child_loaders = list(itertools.chain.from_iterable([loader.children for loader in loaders_with_children]))
				ovs.set_header.set_count = len(loaders_with_children)
				ovs.set_header.asset_count = len(child_loaders)
				ovs.set_header.reset_field("sets")
				ovs.set_header.reset_field("assets")
				ovs.assign_ids(ovs.set_header.sets, loaders_with_children)
				ovs.assign_ids(ovs.set_header.assets, child_loaders)
				ovs.set_header.assets["root_index"] = [loader.root_index for loader in child_loaders]
				start = 0
				for i, (set_entry, loader) in enumerate(zip(ovs.set_header.sets, loaders_with_children)):
					set_entry.start = start
					start += len(loader.children)
					# set_index is 1-based, so the first set = 1
					loader.data_entry.set_index = i + 1
			# add entries to correct ovs
			for loader in self.loaders.values():
				# attach the entries used by this loader to the ovs lists
				loader.register_entries()

			pools_byte_offset = 0
			# make a temporary copy so we can delete archive if needed
			for archive in self.reporter.iter_progress(tuple(self.archives), "Updating archives"):

				logging.debug(f"Sorting pools for {archive.name}")
				ovs = archive.content

				# needs to happen after loader.register_entries
				# change the hashes / indices of all entries to be valid for the current game version
				ovs.update_hashes()
				ovs.data_entries.sort(key=lambda b: (b.ext, b.file_hash))

				# depends on correct hashes applied to buffers and datas
				ovs.rebuild_buffer_groups(ext_lut)

				# update the ovs counts
				archive.num_datas = len(ovs.data_entries)
				archive.num_buffers = len(ovs.buffer_entries)
				archive.num_buffer_groups = len(ovs.buffer_groups)

				# remove stream archive if it has no pools and no roots and no datas, STATIC must always be present
				if archive.name != "STATIC" and not (archive.num_pools or archive.num_root_entries or archive.num_datas):
					logging.info(f"Removed stream archive {archive.name} as it was empty")
					self.archives.remove(archive)
					continue

				archive.pools_start = pools_byte_offset
				archive.content.write_pools()
				pools_byte_offset += len(archive.content.pools_data)
				archive.pools_end = pools_byte_offset
				# at least PZ & JWE require 4 additional bytes after each pool region
				pools_byte_offset += 4
				logging.debug(
					f"Archive {archive.name} has {archive.num_pools} pools in {archive.num_pool_groups} pool_groups")

			# update archive names
			# retail orders this buffer independently of the archive table, so keep
			# the order that was read instead of regenerating it from self.archives
			archive_names = [archive.name for archive in self.archives]
			read_order = [n for _, n in sorted(self.archive_names.offset_2_str.items()) if n]
			if sorted(read_order) == sorted(archive_names):
				archive_names = read_order
			self.archive_names.update_strings(archive_names)
			self.len_archive_names = len(self.archive_names.data)
			# update the ovl counts
			self.num_archives = len(self.archives)
			# sum counts of individual archives
			self.num_datas = sum(a.num_datas for a in self.archives)
			self.num_buffers = sum(a.num_buffers for a in self.archives)
		except:
			logging.exception("Rebuilding ovl arrays failed")

	@contextmanager
	def open_streams(self, mode="wb"):
		logging.debug("Opening streams")
		streams = {}
		for archive_entry in self.archives:
			# gotta update it here
			self.get_ovs_path(archive_entry)
			if archive_entry.ovs_path not in streams:
				# make sure that the ovs exists
				if mode == "rb" and not os.path.exists(archive_entry.ovs_path):
					raise FileNotFoundError(f"OVS file not found. Make sure it is here: {archive_entry.ovs_path}")
				logging.debug(f"Opening {archive_entry.ovs_path}")
				# open file in desired mode
				streams[archive_entry.ovs_path] = open(archive_entry.ovs_path, mode)
		# for simplicity, tests don't always have an ovs, so allow for pure ovl files
		if self.filepath not in streams:
			streams[self.filepath] = open(self.filepath, mode)
		yield streams
		logging.debug("Closing streams")
		# we don't use context manager to open the streams so close them manually
		for ovs_file in streams.values():
			ovs_file.close()

	def update_stream_files(self):
		logging.info("Updating stream file memory links")
		stream_loaders = [(loader, stream_loader) for loader in self.loaders.values() for stream_loader in loader.streams]
		stream_loaders.sort(key=lambda x: (x[1].ovs.arg.name, x[0].abs_mem_offset))
		self.num_stream_files = len(stream_loaders)
		if stream_loaders and self.name.lower() in ("main.ovl", "init.ovl"):
			tex_with_streams = [loader.name for loader in self.loaders.values() if loader.streams and loader.ext == ".tex"]
			if tex_with_streams:
				self.reporter.show_error(
					f"You're trying to save streamed textures in '{self.name}', which does not support streams - "
					f"please check 'Show Details' or the log.", tex_with_streams)
		self.reset_field("stream_files")
		if stream_loaders:
			self.stream_files["file_offset"], self.stream_files["stream_offset"] = zip(*[
				(loader.abs_mem_offset, stream_loader.abs_mem_offset) for loader, stream_loader in stream_loaders])
		# update the archive entries to point to the stream files
		stream_files_offset = 0
		for archive in self.archives:
			archive.stream_files_offset = stream_files_offset
			archive_streams = [stream_loader for (loader, stream_loader) in stream_loaders if stream_loader.ovs.arg == archive]
			# some JWE2 dino archives have no stream_files, just extra data_entries
			stream_files_offset += len(archive_streams)

	def dump_debug_data(self, only_files):
		"""Dumps various logs needed to reverse engineer and debug the ovl format"""
		if not only_files:
			only_files = list(self.loaders.keys())
		out_dir = os.path.join(self.dir, f"{self.basename}_dump")
		logging.info(f"Dumping debug data for {len(only_files)} files to {self.dir}")
		os.makedirs(out_dir, exist_ok=True)
		# todo - ensure every pool has valid pool.i in ovl.pools for created ovls
		for archive_entry in self.archives:
			fp = os.path.join(out_dir, f"{self.basename}_{archive_entry.name}")
			try:
				archive_entry.content.dump_pools(fp)
				archive_entry.content.dump_stack(fp, only_files)
				archive_entry.content.dump_buffer_groups_log(fp)
			except:
				logging.exception("Dumping failed")
		try:
			self.dump_buffer_info(out_dir, only_files)
		except:
			logging.exception("Dumping failed")

	@property
	def sorted_loaders(self):
		return sorted(self.loaders.values(), key=lambda l: l.name)

	def dump_buffer_info(self, out_dir, only_files):
		"""for development; collect info about fragment types"""
		def out_dir_func(n):
			"""Helper function to generate temporary output file name"""
			return os.path.normpath(os.path.join(out_dir, n))
		log_path = out_dir_func(f"{self.basename}_buffer_info.log")
		with open(log_path, "w") as f:
			for loader_name in only_files:
				loader = self.loaders[loader_name]
				loader.dump_buffer_infos(f)
				loader.dump_buffers(out_dir_func)

	def save(self, filepath, commands={}):
		# store commands
		self.commands = commands
		self.store_filepath(filepath)
		with self.reporter.log_duration(f"Writing {self.name}"):
			# do this last so we also catch the assets & sets
			self.rebuild_ovl_arrays(self.commands.get("update_aux", True))
			# these need to be done after the rest
			self.update_stream_files()
			ovs_types = set()
			for loader in self.loaders.values():
				if loader.ext not in (".tex", ".texturestream"):
					ovs_types.update(loader.ovs_names)
			ovs_types.discard("STATIC")
			# ovs_types = {archive.name for archive in self.archives if "Textures_L" not in archive.name}
			self.num_ovs_types = len(ovs_types)
			ovl_compressed = b""
			self.reset_field("archives_meta")
			# print(self)
			# compress data stream
			with self.open_streams() as streams:
				for archive, meta in zip(self.reporter.iter_progress(self.archives, "Saving archives"), self.archives_meta):
					# write archive into bytes IO stream
					uncompressed = archive.content.write_archive()
					archive.uncompressed_size, archive.compressed_size, compressed = archive.content.compress(
						uncompressed, self.commands.get("use_threads", True))
					# update set data size
					archive.set_data_size = archive.content.set_header.io_size
					if archive.name == "STATIC":
						ovl_compressed = compressed
						archive.read_start = 0
					else:
						ovs_stream = streams[archive.ovs_path]
						archive.read_start = ovs_stream.tell()
						ovs_stream.write(compressed)
					# unk_0 is the archive's resident size: its size_1 buffers, its pool
					# data, a fixed cost per root entry / data entry / pool, its set and
					# asset tables, and a constant. Exact on every retail archive shape,
					# so this supersedes the earlier single-pool rule (which it
					# reproduces value for value) and the 68 + uncompressed_size fallback
					# alongside it -- there is no longer a shape condition
					# per_root and the constant are version-gated, both +24 at 19, the
					# same ext_hash schema boundary the old base/per_entry moved on;
					# per_de and per_pool are 16 at both. The set/asset term is not a
					# constant at all, it is those arrays' own byte size, so it tracks
					# the schema on its own (SetEntry 8 -> 12, AssetEntry 16 -> 24)
					# Only versions 18 and 20 were ever measured, and only in the
					# PC/PZ user_version family, so two groups ride on this untested:
					# everything BELOW 19 takes the 32/160 branch, which is Disneyland
					# Adventures (15) and Zoo Tycoon (17) as well as PC1; and the whole
					# djb family at 20 (JWE 2, JWE 2 Dev, JWE 3, Warhammer) takes the
					# 56/184 branch. Version 19 itself (JWE 1, Planet Zoo pre-1.6) has
					# no retail data behind it either. That is the same exposure the old
					# 288/240 code had, not new, but it is wider than "version 18 vs 20"
					# Note use_djb explicitly "affects memory offsets" and gives the JWE
					# family per-pool relative offsets where PC/PZ are relative to the
					# whole pool block (see get_pool_offset) -- this sums pool.size and
					# never reads an offset, so it should carry over, but that is
					# reasoning, not a measurement
					# Verified zero residual on 11986 retail archives across PC2, PC1 and
					# PZ, every shape including empty ones (no pools and no entries gives
					# exactly the constant). Third-party mod ovls under ACSE/, ForgeUtils/
					# and Mod_* do NOT match, and are not counterexamples: they were
					# written by this code, so they carry the old formula's value
					ovs = archive.content
					set_header = ovs.set_header
					per_root = 56 if self.version >= 19 else 32
					meta.unk_0 = (sum(int(d.size_1) for d in ovs.data_entries)
								  + sum(int(p.size) for p in ovs.pools)
								  + per_root * len(ovs.root_entries)
								  + 16 * len(ovs.data_entries)
								  + 16 * len(ovs.pools)
								  + set_header.sets.nbytes + set_header.assets.nbytes
								  + (184 if self.version >= 19 else 160))
					# this is fairly good, doesn't work for tylo static but all others, all of jwe2 rex 93, JWE parrot, pz fallow deer
					meta.unk_1 = sum([data.size_2 for data in archive.content.data_entries])
				# write ovl + static
				stream = streams[self.filepath]
				self.write_fields(stream, self)
				stream.write(ovl_compressed)
		self.reporter.show_success(f"Saved {self.name}")


if __name__ == "__main__":
	ovl = OvlFile()
