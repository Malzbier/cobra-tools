from generated.formats.motiongraphvars.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class MotiongraphVarDef(MemStruct):

	"""
	48 bytes
	
	`kind` selects how the variable is driven: 0 = bool, 2 = trigger,
	3 = enum. For an enum, `enum_name` names the .enumnamer holding the
	member names - and it targets the SAME string as `var_name`, so the file
	is a DAG rather than a tree and needs pointer aliasing to round-trip
	without writing the string twice.
	"""

	__name__ = 'MotiongraphVarDef'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.kind = name_type_map['Uint64'](self.context, 0, None)
		self.zero_16 = name_type_map['Uint'](self.context, 0, None)
		self.one_f = name_type_map['Float'].from_value(1.0)
		self.ten = name_type_map['Uint64'].from_value(10)
		self.default_value = name_type_map['Int'].from_value(-1)
		self.zero_36 = name_type_map['Uint'](self.context, 0, None)
		self.var_name = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		self.enum_name = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'var_name', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'kind', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'zero_16', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'one_f', name_type_map['Float'], (0, None), (False, 1.0), (None, None)
		yield 'ten', name_type_map['Uint64'], (0, None), (False, 10), (None, None)
		yield 'default_value', name_type_map['Int'], (0, None), (False, -1), (None, None)
		yield 'zero_36', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'enum_name', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'var_name', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'kind', name_type_map['Uint64'], (0, None), (False, None)
		yield 'zero_16', name_type_map['Uint'], (0, None), (False, None)
		yield 'one_f', name_type_map['Float'], (0, None), (False, 1.0)
		yield 'ten', name_type_map['Uint64'], (0, None), (False, 10)
		yield 'default_value', name_type_map['Int'], (0, None), (False, -1)
		yield 'zero_36', name_type_map['Uint'], (0, None), (False, None)
		yield 'enum_name', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
