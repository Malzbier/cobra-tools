from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.motiongraph.imports import name_type_map

from generated.formats.motiongraph.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class Something(MemStruct):

	"""
	16 bytes
	"""

	__name__ = 'Something'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.unk = name_type_map['Uint64'](self.context, 0, None)
		self.ptr = name_type_map['Pointer'](self.context, 0, None)
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'ptr', name_type_map['Pointer'], (0, None), (False, None), (None, None)
		yield 'unk', name_type_map['Uint64'], (0, None), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'ptr', name_type_map['Pointer'], (0, None), (False, None)
		yield 'unk', name_type_map['Uint64'], (0, None), (False, None)

	def get_ptr_template(self, prop):
		"""`ptr` targets a MotiongraphVar.

		The field is commented "usually empty" in the format definition, and that
		is exactly why it was never typed -- but when it IS populated, leaving it
		untyped drops the target and every string hanging off it.
		"""
		if prop == "ptr":
			return name_type_map["MotiongraphVar"]

