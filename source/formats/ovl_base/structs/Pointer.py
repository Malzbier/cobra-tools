# START_GLOBALS
import struct
import xml.etree.ElementTree as ET
import logging

import numpy as np

from generated.array import Array
from generated.base_struct import BaseStruct
from generated.formats.base.basic import ZString
from generated.formats.ovl_base.basic import ZStringObfuscated

ZERO = b"\x00"
# these attributes present on the MemStruct will not be stored on the XML
SKIPS = ("_context", "arg", "name", "io_start", "io_size", "template")
POOL_TYPE = "pool_type"
DTYPE = "dtype"
XML_STR = "xml_string"
DEPENDENCY_TAG = "dependency"


def link_key(link):
	"""Hashable key for a (MemPool, offset) link.

	MemPools are unhashable by value, so key by pool index. The offset is
	legitimately None: OvlFile.load_pointers rewrites any fragment whose target
	is the END of a pool to None ("empty pointer"), and read_template already
	special-cases it. A naive int() raises there, and MemStructLoader.collect
	swallows the exception -- so it does not fail loudly, it silently truncates
	the read partway through the root struct.
	"""
	pool, off = link
	return (pool.i, -1 if off is None else int(off))


def share_id(context, link):
	"""Stable small integer id per shared target, allocated on first use.

	Deliberately NOT reset per file: (pool, offset) is unique across the whole
	OVL, and every loader's collect() runs during load, long before extract()
	calls to_xml -- so a per-file registry would only ever hold the last file's
	ids.
	"""
	reg = getattr(context, "share_ids", None)
	if reg is None:
		reg = context.share_ids = {}
	key = link_key(link)
	sid = reg.get(key)
	if sid is None:
		sid = reg[key] = str(len(reg) + 1)
	return sid


def bind_alias(ptr, rel, children, pool, loader, hit):
	"""Point `ptr` at an already-written block without re-writing it.

	Deliberately NOT loader.attach_frag_to_ptr: that resets
	loader.stack[(pool, offset)] = {}, discarding the children the DEFINITION
	recorded for this very target. We want the relocation and the child entry,
	and nothing else.
	"""
	ptr.target_pool, ptr.target_offset = hit
	children[rel] = hit
	pool.offset_2_link[ptr.io_start] = hit
	loader.fragments.add(((pool, ptr.io_start), hit))


def drain_pending_aliases(loader):
	"""Resolve aliases whose definition was written after them."""
	pend = getattr(loader, "pending_aliases", None)
	if not pend:
		return 0
	while pend:
		rest, progress = [], 0
		for item in pend:
			ptr, rel, children, pool, ref = item
			hit = loader.alias_targets.get(ref)
			if hit is None:
				rest.append(item)
			else:
				bind_alias(ptr, rel, children, pool, loader, hit)
				progress += 1
		pend = rest
		if not progress:
			break
	loader.pending_aliases = []
	if pend:
		logging.warning(f"{loader.name}: {len(pend)} alias refs had no definition")
	return len(pend)

# END_GLOBALS


class Pointer(BaseStruct):

	"""
	a pointer in an ovl memory layout
	"""

# START_CLASS

	# When the target serialises to zero bytes, should the relocation be dropped
	# entirely? False here because retail really does ship "empty pointers" whose
	# target is the end of a pool. Subclasses that cannot have one set it True -
	# see write_ptr
	SKIP_EMPTY_TARGET = False

	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		# set to -1 here so that read_ptr doesn't get a wrong frag by chance if the entry has not been read -> get at 0
		self.io_start = -1
		self.target_offset = -1
		self.pool_index = 0
		self.data_offset = 0
		self.data = None
		self.frag = None
		self.link = None
		self.src_pool = None
		self.target_pool = None
		self.pool_type = None
		if set_default:
			self.set_defaults()

	@property
	def has_data(self):
		"""Returns True if it has data"""
		# return bool(self.data)
		return self.data is not None

	def read_ptr(self, pool):
		"""Looks up the address of the pointer, checks if a frag points to pointer and reads the data at its address as
		the specified template."""
		self.src_pool = pool
		# find the frag entry with matching link_ptr.data_offset
		link = pool.offset_2_link.get(self.io_start, None)
		# pointer may be a nullptr, so ignore
		if not link:
			# print("is a nullptr")
			return
		# it is a dependency
		if isinstance(link, str):
			# store dependency name
			self.data = link
		else:
			# now read an instance of template class at the offset
			self.target_pool, self.target_offset = link
			self.link = link
			# we are now (potentially) in a new pool
			self.pool_type = self.target_pool.type
			stream = self.target_pool.stream_at(self.target_offset)
			# this is a rather hacky implementation for motiongraph
			if hasattr(self.context, "recursion"):
				if self.template and link in self.context.recursion:
					data = self.context.recursion[link]
					# activity names must be allowed to be reused
					if isinstance(data, str):
						self.data = data
					else:
						# anything else could break from recursion during printing
						self.data = None
					# The cache cannot tell a CYCLE (must be broken) from DAG
					# SHARING (must be preserved), and setting structs to None
					# handles the first by silently destroying the second: a null
					# pointer emits no relocation, so every second-and-later
					# reference to a shared target was dropped on write, along
					# with its whole subtree. Retail aq_doors01 has 22 targets
					# with more than one incoming pointer (57 redundant edges)
					# `data` semantics are left EXACTLY as they were - that is
					# the point, so nothing re-walks - and we only record WHERE
					# this pointed, so to_xml can emit a reference to it
					self.alias_of = link
					shared = getattr(self.context, "shared_links", None)
					if shared is None:
						shared = self.context.shared_links = set()
					shared.add(link_key(link))
					return
			self.read_template(stream)
			if hasattr(self.context, "recursion"):
				if self.template:
					self.context.recursion[link] = self.data

	def read_template(self, stream):
		if self.template:
			if self.target_offset is not None:
				self.data = self.template.from_stream(stream, self.context, self.arg)
				self.infer_array()
				self.read_block_tail(stream)
			else:
				self.data = None

	def read_block_tail(self, stream):
		"""Some structs occupy a block LARGER than themselves, the remainder
		being a typed array that nothing points at directly.

		A struct opts in by defining `block_tail_template()` returning
		(element class, element size). Because nothing references the tail, it is
		not a block of its own - calc_size_map folds it into the owner's block -
		so a reader that stops after the struct silently drops it, along with
		every pointer inside it.
		"""
		if not isinstance(self.data, BaseStruct):
			return
		spec = getattr(type(self.data), "block_tail_template", None)
		if spec is None:
			return
		pool, off = self.target_pool, self.target_offset
		if pool is None or off is None:
			return
		blk = pool.size_map.get(off)
		unit = int(getattr(self.data, "io_size", 0) or 0)
		if blk is None or unit <= 0:
			return
		extra = int(blk) - unit
		tail_cls, elem_size = spec()
		if extra <= 0 or elem_size <= 0 or extra % elem_size:
			return
		n = extra // elem_size
		self.tail_data = Array.from_stream(pool.stream_at(off + unit), self.context,
										   self.arg, None, (n,), tail_cls)
		self.tail_count = n

	def infer_array(self):
		"""A plain Pointer can target an ARRAY, with no count field anywhere.

		An ArrayPointer targets element 0 only - elements 1..n-1 are not
		relocation targets - so retail's whole array is ONE block and
		calc_size_map sizes it as n x unit. Where the count lives in no field,
		the block size is the only record of it: reading a single element leaves
		the tail unparsed, and the writer then emits one element where retail had
		several, dropping every relocation in the tail.

		Only applies to plain Pointer: ArrayPointer and ForEachPointer already
		carry an explicit count.
		"""
		if type(self) is not Pointer or not isinstance(self.data, BaseStruct):
			return
		# ONLY for formats that opt in, which today means motiongraph - keyed on
		# the same context marker its loader already sets for cycle-breaking
		# Without this guard the inference runs on EVERY format, and any struct
		# whose block merely HAPPENS to be an exact multiple of its own size is
		# reinterpreted as an array. Measured: injecting a motiongraph wiped the
		# texel name on all 21 .tex entries of HOL_Band, which then made the file
		# unloadable - get_texel() builds "/.texel", registers a loader during
		# load_pointers' iteration, and load dies with "dictionary changed size
		# during iteration"
		if not hasattr(self.context, "recursion"):
			return
		pool, off = self.target_pool, self.target_offset
		if pool is None or off is None:
			return
		blk = pool.size_map.get(off)
		unit = int(getattr(self.data, "io_size", 0) or 0)
		if blk is None or unit <= 0:
			return
		blk = int(blk)
		if blk % unit or blk // unit < 2:
			return
		# The size dividing evenly is NOT evidence that the block is an array of
		# this type - it happens by coincidence often enough to do real damage
		# Validate against the element's DECLARED POINTER OFFSETS: in a genuine
		# array every relocation must land where that struct actually has a
		# pointer. Having just read one element, those offsets are simply its
		# pointers' io_start
		#   MRFMember2 (72 B, pointers at 0/8/16/24/48/64) over a 360-byte block:
		#     relocations %72 = {0,8,24,32,48,56} - 32 and 56 are impossible
		#   CurveData (16 B, one pointer at +8) over a 96-byte block:
		#     relocations %16 = {0,8} - 0 is impossible
		# Both were being inferred anyway, producing structs that read a float
		# 1.0 as a count and ran off the end of the pool
		# Note this deliberately does NOT require every element to look alike:
		# a null pointer emits no relocation at all, which is entirely normal
		try:
			valid = {int(p.io_start) - int(self.data.io_start)
					 for p, _f, _a in type(self.data).get_instances_recursive(self.data, Pointer)}
			rel = {(int(l) - int(off)) % unit
				   for l, _r, _e in pool.get_ptrs_in_struct(int(off), blk)}
		except Exception:
			# The validation this guards is the whole point of infer_array - see
			# the comment above. An exception here means the check could not run,
			# NOT that it passed, so the only safe response is to refuse the
			# inference, the same as an explicit mismatch would
			logging.exception(f"pool {pool.i} offset {off}: array-inference "
							  f"validation raised, refusing to infer")
			return
		if not rel <= valid:
			return
		self.data = Array.from_stream(pool.stream_at(off), self.context, self.arg,
									  None, (blk // unit,), self.template)
		self.multi_count = blk // unit

	def write_ptr_all(self, parent_memstruct, children, f_name, loader, pool):
		# when an array is entered
		# locates the read address, attaches the frag entry, and reads the template as ptr.data
		offset = self.io_start
		rel_offset = offset - parent_memstruct.io_start
		ref = getattr(self, "alias_ref", None)
		if ref is not None:
			# point at the already-written definition rather than writing a copy
			hit = getattr(loader, "alias_targets", {}).get(ref)
			if hit is None:
				# Definition not written yet. Field order makes that unlikely -
				# read, to_xml and write_ptrs all traverse depth-first in the
				# same order - but unlikely is not impossible, so defer
				loader.pending_aliases.append((self, rel_offset, children, pool, ref))
			else:
				bind_alias(self, rel_offset, children, pool, loader, hit)
			return
		# logging.debug(f"Pointer {f_name}, has_data {ptr.has_data} at {ptr.io_start}, relative {rel_offset}")
		# when it's a pointer in an array, f_name is the array index
		if isinstance(f_name, str) and isinstance(self.data, str) and DEPENDENCY_TAG in f_name:
			if self.data:
				# loader.dependencies[ptr.data] = (pool, offset)
				loader.dependencies.append((self.data, (pool, offset)))
				pool.offset_2_link[offset] = self.data
		elif self.has_data:
			# General write-side counterpart to the alias_ref branch above, which
			# only fires on the XML round-trip: a second pointer to the SAME
			# Python object binds to where the first one wrote, rather than
			# writing (and, if self.data cycles back to this pointer, recursing
			# into) a second copy. The read path's context.recursion cache already
			# makes shared targets the same object after a binary load, so object
			# identity is ground truth here - for structs. NOT for strings:
			# CPython interns the empty string (and may intern short ones), so
			# id() equality does not mean "same source block" for str data
			# a measured motiongraph carries four DISTINCT empty-string blocks, one per group
			# of activities; all sixteen pointers at them read as the one
			# interned '' object, which merged the blocks AND orphaned every
			# ref="N" whose id="N" definition was swallowed by the early return
			# below - exactly 21 dropped relocations. A string's identity is
			# therefore the block it was READ from (alias_of on a repeat
			# reference, link on the definition); XML-loaded strings have
			# neither and share through alias_ref/share_id alone
			registry = getattr(loader, "write_registry", None)
			if isinstance(self.data, str):
				src = getattr(self, "alias_of", None) or self.link
				reg_key = ("str",) + link_key(src) if isinstance(src, tuple) else None
			else:
				reg_key = id(self.data)
			hit = registry.get(reg_key) if (registry is not None and reg_key is not None) else None
			if hit is not None:
				bind_alias(self, rel_offset, children, pool, loader, hit[1])
				# a definition bound here still DEFINES its id: without this,
				# every ref="N" pointing at it stays pending and its relocation
				# is silently dropped by drain_pending_aliases
				sid = getattr(self, "share_id", None)
				if sid is not None:
					loader.alias_targets[sid] = hit[1]
				return
			self.write_ptr(loader, pool)
			if registry is not None and reg_key is not None:
				registry[reg_key] = (self.data, (self.target_pool, self.target_offset))
			# store relative offset from this memstruct
			children[rel_offset] = (self.target_pool, self.target_offset)
			# keep writing pointers in ptr.data
			for memstruct in parent_memstruct.structs_from_ptr(self):
				memstruct.write_ptrs(loader, self.target_pool)
			sid = getattr(self, "share_id", None)
			# target_offset None is a VALID target, not a failed write: write_ptr
			# sets it to None when the struct wrote no bytes, meaning "points at
			# the end of the pool", and retail genuinely has such fragments
			# Requiring it to be non-None here silently refuses to record those
			# definitions, so their aliases can never bind and their relocations
			# are dropped - worth exactly one dangling fragment per file
			if sid is not None and self.target_pool is not None:
				loader.alias_targets[sid] = (self.target_pool, self.target_offset)

	def write_ptr(self, loader, src_pool):
		# when generated from XML, the pool type is stored as metadata
		# it's not stored in binary, so for those, keep the root pool type
		if self.pool_type is None:
			self.pool_type = src_pool.type
		self.target_pool = loader.get_pool(self.pool_type)
		# Point at an identical string already in this pool rather than copying it
		# - but only for formats whose in-memory graph does NOT record block
		# identity. Formats that do (INTERN_STRINGS = False, the motiongraph
		# family) express genuine sharing through alias_of/alias_ref and the
		# write_registry, and retail motiongraphs genuinely ship several SEPARATE
		# blocks holding equal text - a measured graph has four distinct
		# 'AnimationActivity' blocks with four referrers each. Merging those by
		# value here destroyed that structure on a null round trip: 15 blocks and
		# 21 of 708 relocations lost, reproducibly, with the XML entirely correct
		str_key = None
		if isinstance(self.data, str) and self.template in (ZString, ZStringObfuscated) \
				and getattr(loader, "INTERN_STRINGS", True):
			cache = getattr(self.target_pool, "write_str_cache", None)
			if cache is not None:
				str_key = self.data
				hit = cache.get(str_key)
				if hit is not None:
					self.target_offset = hit
					loader.attach_frag_to_ptr(src_pool, self.io_start,
											  self.target_pool, self.target_offset)
					return
		# usually we add a pointer for empty arrays
		# assert self.has_data
		# seek to end, set data_offset, write
		stream, self.target_offset = self.target_pool.align_write(self.data)
		# if bytes have been set (usually manually), don't ask, just write
		if isinstance(self.data, (bytes, bytearray)):
			stream.write(self.data)
		else:
			try:
				assert self.template is not None
				if self.data is None:
					logging.info(f"Can't write None for class {self.template}")
				elif isinstance(self.data, (Array, np.ndarray)):
					Array.to_stream(self.data, stream, self.context, dtype=self.template)
				else:
					self.template.to_stream(self.data, stream, self.context)
			except TypeError:
				raise TypeError(f"Failed to write pointer data {self.data} type: {type(self.data)} as {self.template}")
			except struct.error:
				raise TypeError(f"Failed to write pointer data {self.data} type: {type(self.data)} as {self.template}")
		# A typed tail (see read_block_tail) must be written back IMMEDIATELY after
		# the struct so it lands inside the same block, exactly as retail has it
		tail = getattr(self, "tail_data", None)
		if tail is not None and len(tail):
			Array.to_stream(tail, stream, self.context, dtype=type(tail[0]))
		# nothing has been written, so this is an "empty pointer"
		if self.target_offset == stream.tell():
			# Retail keeps the write cursor as an empty pointer's address, but only
			# inside an ARRAY TAIL element; an empty pointer in a struct that is a
			# block of its own is aimed at the end of the pool. Surveyed, not assumed
			# (research/probe_empty_ptr_census.py, 12 PC2 assets): all 16 resolved
			# empty pointers sit in a tail element and all 60 dangling ones sit at a
			# block start, with no field, struct type or format on only one side of
			# that split. The tail case is what makes a zero-count ModelInfo's four
			# array pointers resolve to where the NEXT element's arrays were written,
			# instead of coming back dangling
			if not getattr(self, "in_array_tail", False):
				self.target_offset = None
			if self.SKIP_EMPTY_TARGET:
				self.target_offset = None
				# Retail has NO relocation here, not an empty one. A ForEachPointer
				# whose every element serialises to zero bytes has nothing to point
				# at - FgmHeader.name_foreach_textures on a material with no
				# textures is the case that surfaced it: TextureData's only field,
				# dependency_name, is conditional on dtype == 8, so on an all-RGBA
				# material every element writes zero bytes. Attaching a fragment
				# regardless added exactly one dangling pointer to 18 corpus files,
				# all of them (4,3,3,0,0) -> (4,4,3,1,0)
				#
				# Deliberately NOT the default for Pointer. An "empty pointer"
				# whose target is the END of a pool is something retail genuinely
				# ships - load_pointers rewrites those to None, and link_key
				# special-cases them - so suppressing them wholesale would delete
				# relocations that are really there
				return
		else:
			# only store these if the pointer had valid data
			self.target_pool.offsets.add(self.target_offset)
			# store size in size_map
			self.target_pool.size_map[self.target_offset] = self.target_pool.data.tell() - self.target_offset
			# only register a block that was really written
			if str_key is not None:
				self.target_pool.write_str_cache[str_key] = self.target_offset
		# the data has been written, now store the links
		loader.attach_frag_to_ptr(src_pool, self.io_start, self.target_pool, self.target_offset)

	@classmethod
	def to_xml(cls, elem, prop, instance, arg, template, debug):
		"""Adds this struct to 'elem', recursively"""
		link = getattr(instance, "alias_of", None)
		if link is not None:
			# A second-or-later reference to a shared target: emit a REFERENCE,
			# not a copy, so the write path can point at the one definition
			sub = ET.SubElement(elem, prop)
			cls.pool_type_to_xml(sub, instance, debug)
			sub.set("ref", share_id(instance.context, link))
			# An aliased ArrayPointer has no children to count, but the struct's
			# sibling count field is derived from len(<element>) on load, so the
			# count must be carried explicitly or it silently becomes 0
			if arg:
				sub.set("refcount", str(int(arg)))
			# A string alias ALSO keeps its value. Aliasing must dedup the WRITE,
			# not hide the VALUE: polymorphic pointers take their template from a
			# sibling string (Activity.data from Activity.data_type), so emitting
			# a bare ref leaves get_ptr_template returning None on load and the
			# entire subtree is dropped. Strings are leaves; carrying the text
			# costs nothing structurally
			if isinstance(instance.data, str):
				sub.text = instance.data
			return
		if not instance.has_data:
			# A NULL pointer can still carry non-zero raw words. A Pointer is 8
			# bytes on disk (pool_index + data_offset) and those are never
			# serialised, because the relocation table is the truth for any
			# pointer that HAS a relocation. But retail leaves non-zero values
			# there on pointers with NO relocation, and emitting nothing at all
			# brings them back as zeros
			pi = int(getattr(instance, "pool_index", 0) or 0)
			off = int(getattr(instance, "data_offset", 0) or 0)
			if pi or off:
				ET.SubElement(elem, prop).set("raw", f"{pi},{off}")
			return
		# only create the sub-element if the pointer has data
		sub = ET.SubElement(elem, prop)
		cls.pool_type_to_xml(sub, instance, debug)
		# xml string
		if prop == XML_STR:
			sub.append(ET.fromstring(instance.data))
		else:
			cls._to_xml(instance, sub, debug)
		# mark the DEFINITION so aliases above can reference it
		shared = getattr(instance.context, "shared_links", None)
		if shared and isinstance(instance.link, tuple) and link_key(instance.link) in shared:
			sub.set("id", share_id(instance.context, instance.link))

	@classmethod
	def pool_type_to_xml(cls, elem, instance, debug):
		"""Sets the pool type of instance to elem's attrib"""
		if instance.link and isinstance(instance.link, tuple):
			pool = instance.target_pool
			if debug:
				elem.set("_address", f"{pool.i} | {instance.target_offset}")
				elem.set("_size", f"{pool.size_map.get(instance.target_offset, -1)}")
			cls._set_pool_type(elem, pool.type, instance.template)
		elif hasattr(instance, POOL_TYPE):
			if instance.pool_type is not None:
				cls._set_pool_type(elem, instance.pool_type, instance.template)

	@staticmethod
	def _set_pool_type(elem, pool_type, template):
		"""Set the pool type, unless it is obvious"""
		# if template not in (ZString, ZStringObfuscated):
		if pool_type != 2:
			elem.set(POOL_TYPE, f"{pool_type}")

	@classmethod
	def _to_xml(cls, instance, elem, debug):
		"""Assigns data self to xml elem"""
		n = getattr(instance, "multi_count", None)
		if n and isinstance(instance.data, Array):
			# Element count inferred from the block size (see infer_array). It is
			# NOT re-derivable when loading the XML back, because the block does
			# not exist yet at that point, so it has to survive in the XML
			elem.set("multi", str(n))
			Array._to_xml(instance.data, elem, debug)
			return
		# catch Zstr Pointers and dependencies (template=None)
		if isinstance(instance.data, str):
			elem.text = instance.data
		else:
			if instance.template is not None and instance.has_data:
				instance.template._to_xml(instance.data, elem, debug)
		# a typed tail sharing this block: emit it as its own sub-element so the
		# count and every nested pointer survive the round-trip
		tail = getattr(instance, "tail_data", None)
		if tail is not None and len(tail):
			sub = ET.SubElement(elem, "block_tail")
			sub.set("count", str(len(tail)))
			Array._to_xml(tail, sub, debug)

	@classmethod
	def pool_type_from_xml(cls, elem, instance):
		if POOL_TYPE in elem.attrib:
			instance.pool_type = int(elem.attrib[POOL_TYPE])
			# logging.debug(f"Set pool type {instance.pool_type} for pointer {elem.tag}")
		else:
			instance.pool_type = 2

	@classmethod
	def from_xml(cls, target, elem, prop, arg, template):
		"""Creates object for parent object 'target', from parent element elem."""
		# create Pointer instance
		instance = cls(target.context, arg, template, set_default=False)
		# print(f"ptr instance.from_xml {instance.template}")
		# check if the pointer holds data
		sub = elem.find(f'./{prop}')
		raw = sub.get("raw") if sub is not None else None
		if raw is not None:
			# null pointer carrying non-zero raw words (see to_xml). Handled here
			# rather than further down so an ArrayPointer never reaches the
			# empty-Array path and grows a spurious relocation
			instance.data = None
			pi, off = raw.split(",")
			instance.pool_index, instance.data_offset = int(pi), int(off)
			return instance
		ref = sub.get("ref") if sub is not None else None
		if ref is not None:
			# a reference to a shared target defined elsewhere in this file
			cls.pool_type_from_xml(sub, instance)
			# keep a string alias's value; struct aliases stay None so nothing
			# tries to write a second copy of the target
			instance.data = (sub.text or "") if template in (ZString, ZStringObfuscated) else None
			instance.alias_ref = ref
			return instance
		if sub is not None and sub.get("id") is not None:
			# this is the DEFINITION that aliases will point at
			instance.share_id = sub.get("id")
		if sub is None:
			# An ABSENT element is how to_xml writes a NULL pointer: its
			# not-has_data branch emits nothing at all (bar the raw-words case
			# handled above). So this is the writer's own encoding round-tripping
			# back, not a defect, and warning here fired on cobra's own output -
			# 92 times per scenery motiongraph, burying real diagnostics
			# ArrayPointer takes the same view and only warns when a non-zero
			# count contradicts the absence; a plain Pointer has no such witness,
			# so this stays at debug rather than becoming conditional
			#
			# Reference deliberately keeps the WARNING: its to_xml always emits
			# an element, so there a missing one really is anomalous
			logging.debug(f"Missing sub-element '{prop}' on XML element '{elem.tag}'")
			# we absolutely do need to create the instance so that the structure of the parent struct remains intact
			instance.data = None
		else:
			# store the pointer's pool type
			cls.pool_type_from_xml(sub, instance)
			# process the pointer's data
			if prop == XML_STR:
				instance.data = ET.tostring(sub[0], encoding="unicode").replace("\t", "").replace("\n", "")
			else:
				cls._from_xml(instance, sub)
		# print(f"after ptr instance.from_xml {instance.template}")
		return instance

	@classmethod
	def _from_xml(cls, instance, elem):
		try:
			multi = elem.get("multi")
			if multi and instance.template is not None:
				# an array whose element count was inferred from the block size
				# (see infer_array) - rebuild it from the element's children, the
				# way ArrayPointer._from_xml does
				arr = Array(instance.context, instance.arg, None,
							(len(elem),), instance.template, set_default=False)
				instance.data = Array._from_xml(arr, elem)
				instance.multi_count = int(multi)
				return instance
			if instance.template is None:
				if DEPENDENCY_TAG in elem.tag:
					if elem.text and elem.text != "None":
						logging.debug(f"Setting dependency {type(instance).__name__}.data = {elem.text}")
						instance.data = elem.text
				return
			elif instance.template in (ZString, ZStringObfuscated):
				# A PRESENT BUT EMPTY element (<some_string />) must round-trip as
				# an EMPTY STRING, not as "no pointer at all". Leaving data None
				# makes has_data False, so write_ptr_all skips the pointer and
				# neither the relocation NOR its 1-byte target block is written -
				# yet retail genuinely has fragments pointing at empty strings
				instance.data = elem.text if elem.text else ""
			else:
				instance.data = instance.template(instance.context, instance.arg, None)
				instance.template._from_xml(instance.data, elem)
				# restore a typed tail that shares this block
				sub = elem.find("./block_tail")
				if sub is not None:
					spec = getattr(type(instance.data), "block_tail_template", None)
					if spec is not None:
						tail_cls, _elem_size = spec()
						arr = Array(instance.context, instance.arg, None,
									(len(sub),), tail_cls, set_default=False)
						instance.tail_data = Array._from_xml(arr, sub)
						instance.tail_count = len(sub)
			return instance
		except:
			logging.exception(f"Error on ptr {elem} {elem.attrib}")
			# raise
