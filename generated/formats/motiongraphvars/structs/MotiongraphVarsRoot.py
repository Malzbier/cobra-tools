from generated.formats.motiongraphvars.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class MotiongraphVarsRoot(MemStruct):

	"""
	16 bytes. The variables a motiongraph can branch on.
	
	NOT an enumnamer, although the loader used to say "probably same layout".
	Both formats open with a uint64 count, so that guess read the count
	correctly by luck and the body entirely wrong, then wrote a ZStringList
	where a pointer array belongs. Measured before this: 9 of 70 files
	round-tripped, and those 9 were exactly the 9 EMPTY ones - every file
	actually declaring a variable lost a block and two fragments.
	"""

	__name__ = 'MotiongraphVarsRoot'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.count = name_type_map['Uint64'](self.context, 0, None)
		self.vars = name_type_map['ArrayPointer'](self.context, self.count, name_type_map['MotiongraphVarRef'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'count', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'vars', name_type_map['ArrayPointer'], (None, name_type_map['MotiongraphVarRef']), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'count', name_type_map['Uint64'], (0, None), (False, None)
		yield 'vars', name_type_map['ArrayPointer'], (instance.count, name_type_map['MotiongraphVarRef']), (False, None)
