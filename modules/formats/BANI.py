import os
import struct

from generated.formats.bani import BanisInfoHeader
from generated.formats.bani.structs.BanisRoot import BanisRoot
from generated.formats.bani.structs.BaniRoot import BaniRoot
from modules.formats.BaseFormat import MemStructLoader, MimeVersionedLoader
from modules.helpers import as_bytes


class BaniLoader(MimeVersionedLoader):
	extension = ".bani"
	target_class = BaniRoot
	can_extract = False

	# def collect(self):
	# 	super().collect()
	# 	print(self.header)

	def create(self, file_path):
		pass

	def create_header(self, data, banis_loader):
		self.header = data
		self.write_memory_data()
		pool, _ = self.root_ptr
		# delete old link if it exists
		self.delete_frag(pool, self.header.banis.io_start, *banis_loader.root_ptr)
		self.attach_frag_to_ptr(pool, self.header.banis.io_start, *banis_loader.root_ptr)
		self.header.banis.link = banis_loader.root_ptr
		# print(self.fragments)


class BanisLoader(MimeVersionedLoader):
	extension = ".banis"
	target_class = BanisRoot

	# BanisRoot's four pointer fields (since mime_version 7) all target the bulk
	# keyframe data: up to ~59MB / 4.9M keyframes for a single asset (Guests)
	# Parsing that into python/numpy objects via the generic read_ptrs() walk is
	# what made loading some assets take 10+ minutes; every target is preserved
	# as an opaque byte blob instead (see _read_ptr_raw), never interpreted, so
	# the round-trip is byte-identical by construction regardless of size. Safe
	# because none of BaniData/BaniBones/Keys (bani.xml) contain pointers of
	# their own, so nothing downstream is lost by not parsing them
	OPAQUE_PTR_FIELDS = ("bani_data", "bones_foreach_bani_data", "bones_2_foreach_bani_data", "keys")

	def collect(self):
		pool, offset = self.root_ptr
		stream = pool.stream_at(offset)
		self.header = self.target_class.from_stream(stream, self.context)
		for f_name in self.OPAQUE_PTR_FIELDS:
			ptr = getattr(self.header, f_name, None)
			if ptr is not None:
				self._read_ptr_raw(ptr, pool)

	@staticmethod
	def _read_ptr_raw(ptr, pool):
		"""Resolve ptr's target as raw, unparsed bytes instead of an instance of
		ptr.template. Pointer.write_ptr already writes bytes/bytearray data
		straight back out ("if bytes have been set (usually manually), don't
		ask, just write"), so no further plumbing is needed to round-trip it."""
		ptr.src_pool = pool
		link = pool.offset_2_link.get(ptr.io_start)
		if not link:
			# nullptr, leave ptr.data at its default (None)
			return
		if isinstance(link, str):
			# a dependency, not a struct target
			ptr.data = link
			return
		ptr.target_pool, ptr.target_offset = link
		ptr.link = link
		ptr.pool_type = ptr.target_pool.type
		if ptr.target_offset is None:
			# empty pointer, targets the end of a pool
			return
		size = ptr.target_pool.size_map.get(ptr.target_offset, 0)
		ptr.data = ptr.target_pool.get_at(ptr.target_offset, size)

	def validate(self):
		self.extra_loaders = []
		for loader in self.ovl.loaders.values():
			if loader.ext == ".bani":
				if self.root_ptr == loader.header.banis.link:
					self.extra_loaders.append(loader)
		self.extra_loaders.sort(key=lambda bani: bani.name)

	def extract(self, out_dir):
		name = self.name
		if not self.data_entry:
			raise AttributeError(f"No data entry for {name}")
		buffers = self.data_entry.buffer_datas
		if len(buffers) != 1:
			raise AttributeError(f"Wrong amount of buffers for {name}")
		out_path = out_dir(name)
		out_paths = [out_path, ]
		with open(out_path, 'wb') as stream:
			stream.write(struct.pack("<II", self.mime_version, len(self.extra_loaders)))
			for bani in self.extra_loaders:
				stream.write(as_bytes(os.path.splitext(bani.name)[0]))
				bani.header.to_stream(bani.header, stream, bani.header.context)
			self.header.to_stream(self.header, stream, self.header.context)
			# the keys themselves
			if self.mime_version < 7:
				stream.write(buffers[0])
			else:
				# 16 bytes in buffer: 2F 00 00 00 8B 12 00 00 7B 3E 07 00 3B 68 D4 02
				# PC2 banis buffer, as ints:
				# 47 - 4747 - 474747 - 47474747
				# collect() now preserves keys.data as an opaque byte blob (see
				# OPAQUE_PTR_FIELDS), so write it verbatim instead of assuming a
				# parsed Keys struct
				keys_data = self.header.keys.data
				if isinstance(keys_data, (bytes, bytearray)):
					stream.write(keys_data)
				else:
					keys_data.to_stream(keys_data, stream, self.header.context)

		return out_paths

	def create(self, file_path):
		with open(file_path, 'rb') as stream:
			banis = BanisInfoHeader.from_stream(stream, self.context)
			self.header = banis.data
			keys = stream.read()
		self.write_memory_data()
		self.extra_loaders = []
		for bani in banis.anims:
			bani_name = f"{bani.name}.bani"
			bani_loader = self.ovl.create_file(f"dummy_dir/{bani_name}", bani_name)
			bani_loader.create_header(bani.data, self)
			self.extra_loaders.append(bani_loader)
		self.create_data_entry((keys,))


