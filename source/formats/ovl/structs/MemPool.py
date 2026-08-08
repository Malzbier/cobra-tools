# START_GLOBALS
import logging
import numpy as np

from generated.formats.base.structs.PadAlign import get_padding


# END_GLOBALS


class MemPool:

# START_CLASS

	def clear_data(self):
		self.new = False
		# lookup by offset
		self.offset_2_link = {}  # link_ptrs are unique
		self.size_map = {}
		self.offsets = set()
		self.link_offsets = None
		self.debug_dump = None
		# leading bytes nothing pointed at when this pool was read
		self.prefix = 0
		# retail stores each distinct string once, so intern them on write
		self.write_str_cache = {}

	# align_write's coarsest alignment, so a moved block keeps its residue
	ALIGNMENT = 16

	def compact(self):
		"""Drop bytes no live struct covers, returning {old ptr: new ptr}.

		Rewriting an entry drops its old blocks from self.offsets but leaves the
		bytes behind, so reclaim them here. Only self.prefix is kept: 9 of 1166
		retail pools have leading bytes nothing points at, but measuring against
		the CURRENT first block instead would spare a whole orphaned copy in
		every pool the rewrite replaced outright.

		The remap covers link offsets as well as block starts, because fragments
		and dependencies point INSIDE a block, not at its start.
		"""
		live = sorted(self.offsets)
		if not live:
			return {}
		data = self.data.getvalue()
		out = bytearray(data[:min(self.prefix, live[0])])
		links = sorted(self.offset_2_link.items())
		new_links = {}
		size_map = {}
		remap = {}
		i = 0
		for offset in live:
			size = self.size_map[offset]
			# only whole ALIGNMENT steps are reclaimed, so the block stays aligned
			out.extend(b"\x00" * ((offset - len(out)) % self.ALIGNMENT))
			new_offset = len(out)
			out.extend(data[offset: offset + size])
			size_map[new_offset] = size
			if new_offset != offset:
				remap[(self, offset)] = (self, new_offset)
			# link offsets travel with the block they sit in, the rest are gone
			while i < len(links) and links[i][0] < offset:
				i += 1
			while i < len(links) and links[i][0] < offset + size:
				l_offset, entry = links[i]
				new_links[l_offset - offset + new_offset] = entry
				if new_offset != offset:
					remap[(self, l_offset)] = (self, l_offset - offset + new_offset)
				i += 1
		if not remap and len(out) == len(data):
			return {}
		logging.debug(f"Compacted pool {self.i} from {len(data)} to {len(out)} bytes")
		self.data.seek(0)
		self.data.write(out)
		self.data.truncate()
		self.prefix = min(self.prefix, live[0])
		self.offsets = set(size_map)
		self.size_map = size_map
		self.offset_2_link = new_links
		self.link_offsets = np.array(list(new_links.keys()))
		return remap

	def get_ptrs_in_struct(self, p_offset, p_size):
		"""find all link offsets that are within the parent struct using a boolean mask"""
		k = self.link_offsets
		for l_offset in k[(p_offset <= k) * (k < p_offset + p_size)]:
			entry = self.offset_2_link[l_offset]
			yield l_offset, l_offset - p_offset, entry

	def get_first_offset(self):
		# usually 0 for original pools, but be safe and consider deleted pointers too
		if self.offsets:
			first_offset = sorted(self.offsets)[0]
			return first_offset

	def get_debug_dump(self):
		"""write a pointer marker at each offset"""
		self.data.seek(0)
		self.debug_dump = bytearray(self.data.read())
		for offs, entry in self.offset_2_link.items():
			self.debug_dump[offs: offs + 8] = b"@POINTER" if isinstance(entry, tuple) else b"@DEPENDS"

	def calc_size_map(self):
		"""Store size of every struct_ptr in size_map"""
		self.size_map = {}
		# sort them
		sorted_offsets = sorted(self.offsets)
		self.prefix = sorted_offsets[0] if sorted_offsets else 0
		# add the end of the header data block
		sorted_offsets.append(self.size)
		# get the size of each pointer
		for i, offset in enumerate(sorted_offsets[:-1]):
			# get the offset of the next pointer, substract this offset
			data_size = sorted_offsets[i + 1] - offset
			self.size_map[offset] = data_size
		# store array of link offsets for check_for_ptrs
		self.link_offsets = np.array(list(self.offset_2_link.keys()))

	def contains_at(self, offset, byte_name_tups):
		"""Checks if struct at offset contains any old of the bytes tuples in byte_name_tups"""
		data = self.get_data_at(offset)
		for old, new in byte_name_tups:
			if data and old in data:
				return True
		return False

	def replace_bytes_at(self, offset, byte_name_tups):
		"""Replaces the bytes tuples in byte_name_tups"""
		data = self.get_data_at(offset)
		for old, new in byte_name_tups:
			data = data.replace(old, new)
		stream = self.stream_at(offset)
		stream.write(data)

	def stream_at(self, offset):
		# emulate empty pointers to 'read' the end of a pool
		if offset is None:
			offset = self.get_size()
		self.data.seek(offset)
		return self.data

	def get_at(self, offset, size=-1):
		self.data.seek(offset)
		return self.data.read(size)

	def get_data_at(self, offset):
		"""Get data from pool writer"""
		if offset is not None:
			return self.get_at(offset, self.size_map[offset])

	def get_size(self):
		# seek to end of stream
		self.data.seek(0, 2)
		return self.data.tell()

	def pad(self, alignment=8):
		"""Pad the pool to `alignment`.

		Retail pools are 8-byte aligned, not 4: a survey of 60 shipped PC2 OVLs
		found 53 exactly 8-aligned and none 2- or 4-aligned. rebuild_pools is the
		only caller and passes no argument, so any pool whose payload was 1..4
		mod 8 came out exactly 4 bytes short. calc_size_map sizes each struct as
		"offset of the next pointer minus this offset", which folds the pool tail
		into the LAST struct -- so the symptom presented as "the final ZString
		lost 4 NULs" rather than as a pool-level problem.

		Measured on .sceneryanimchoices round-trip: alignment 4 -> 23/50 files
		byte-exact, 8 -> 50/50, 16 -> 22/50. So it is 8 specifically, not simply
		"more is better".
		"""
		size = self.get_size()
		padding_bytes = get_padding(size, alignment)
		logging.debug(f"Padded pool of ({size} bytes) with {len(padding_bytes)}, alignment = {alignment}")
		self.data.write(padding_bytes)
		self.size = self.get_size()

	def align_write(self, data, overwrite=False):
		"""Prepares self.pool.data for writing, handling alignment according to type of data"""
		# if overwrite:
		# 	# write at old data_offset, but then check for size match
		# 	if isinstance(data, (bytes, bytearray, str)) and self.data_size != len(data):
		# 		logging.warning(f"Data size for overwritten pointer has changed from {self.data_size} to {len(data)}!")
		# 	self.data.seek(self.data_offset)
		# else:
		# seek to end of pool
		self.data.seek(0, 2)
		# check for alignment
		if isinstance(data, str):
			alignment = 1
		else:
			alignment = 16
		# logging.info(f"{type(data)} {data} alignment {alignment}")
		# write alignment to pool
		if alignment > 1:
			offset = self.data.tell()
			padding = (alignment - (offset % alignment)) % alignment
			if padding:
				self.data.write(b"\x00" * padding)
				# logging.debug(
				# 	f"Aligned pointer from {offset} to {self.data.tell()} with {padding} bytes, alignment = {alignment}")
		return self.data, self.data.tell()
		# return True

