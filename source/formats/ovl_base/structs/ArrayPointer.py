# START_GLOBALS
import logging

from generated.array import Array
from generated.formats.ovl_base.structs.Pointer import Pointer
# END_GLOBALS


class ArrayPointer(Pointer):

	"""
	a pointer to an array in an ovl memory layout
	"""

# START_CLASS

	def set_defaults(self):
		super(ArrayPointer, self).set_defaults()
		self.data = Array(self.context, 0, None, (self.arg,), self.template, True)

	def read_template(self, stream):
		if self.template:
			try:
				self.data = Array.from_stream(stream, self.context, 0, None, (self.arg,), self.template)
			except:
				logging.exception(f"Could not read array of '{self.template.__name__}'")
				self.data = None

	@classmethod
	def from_xml(cls, target, elem, prop, arg, template):
		"""Creates object for parent object 'target', from parent element elem."""
		# create Pointer instance
		instance = cls(target.context, arg, template, set_default=False)
		# check if the pointer holds data
		sub = elem.find(f'./{prop}')
		if sub is None:
			# An ABSENT array element must be a NULLPTR, not an empty array
			# _from_xml(instance, ()) builds a zero-length Array, which is not
			# None, so has_data is True, so write_ptr writes zero bytes, sets
			# target_offset = None and STILL attaches a fragment: a relocation
			# pointing at the end of the pool where retail has none at all
			# The asymmetry is real - a pointer that DID have a link reads back
			# as a present (possibly empty) element and must keep its dangling
			# relocation; one that had NO link emits no element and must stay a
			# nullptr. Conflating them inflates dangling fragment counts
			if arg:
				logging.warning(f"Missing array '{prop}' on XML element '{elem.tag}' for count {arg}")
			instance.data = None
			return instance
		raw = sub.get("raw")
		if raw is not None:
			# A null pointer carrying non-zero raw words (see Pointer.to_xml's
			# not-has_data branch, which ArrayPointer inherits since it does not
			# override to_xml). Pointer.from_xml already treats this as a
			# nullptr; ArrayPointer.from_xml did not, so a dangling array
			# pointer - real retail data, never resolvable - came back with
			# has_data True. write_ptr then gave it a live relocation retail
			# never had, and on reload the sibling count field (itself
			# untouched leftover retail bytes, harmless while the pointer
			# stayed null) sized a real array read: the count-corruption hang
			instance.data = None
			pi, off = raw.split(",")
			instance.pool_index, instance.data_offset = int(pi), int(off)
			return instance
		ref = sub.get("ref")
		if ref is not None:
			# a reference to a shared target defined elsewhere (see Pointer.to_xml)
			cls.pool_type_from_xml(sub, instance)
			instance.data = None
			instance.alias_ref = ref
			return instance
		cls._from_xml(instance, sub)
		cls.pool_type_from_xml(sub, instance)
		if sub.get("id") is not None:
			instance.share_id = sub.get("id")
		return instance

	@classmethod
	def _to_xml(cls, instance, elem, debug):
		"""Assigns data self to xml elem"""
		if callable(getattr(instance.template, "_to_xml_array", None)):
			instance.template._to_xml_array(instance.data, elem, debug)
			return
		# assert isinstance(instance.data, Array)
		try:
			Array._to_xml(instance.data, elem, debug)
		except:
			logging.warning(f"Could not convert array '{instance.data}' to XML")

	@classmethod
	def _from_xml(cls, instance, elem):
		if callable(getattr(instance.template, "_from_xml_array", None)):
			instance.data = instance.template._from_xml_array(None, elem)
			return
		arr = Array(instance.context, 0, None, (len(elem)), instance.template, set_default=False)
		instance.data = Array._from_xml(arr, elem)
		return instance
