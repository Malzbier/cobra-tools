import logging
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.motiongraph.imports import name_type_map

from generated.formats.motiongraph.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class MRFMember1(MemStruct):

	"""
	72 bytes / 144 bytes ?
	"""

	__name__ = 'MRFMember1'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.count_0 = name_type_map['Uint64'](self.context, 0, None)
		self.dtype = name_type_map['Uint64'](self.context, 0, None)
		self.num_children = name_type_map['Uint64'](self.context, 0, None)
		self.count_4 = name_type_map['Uint64'](self.context, 0, None)
		self.lua_method = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		self.ptr_0 = name_type_map['Pointer'](self.context, 0, None)
		self.motiongraph_vars = name_type_map['Pointer'](self.context, self.dtype, name_type_map['MotiongraphVars'])
		self.children = name_type_map['ArrayPointer'](self.context, self.num_children, name_type_map['MRFChild'])
		self.id = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'lua_method', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'count_0', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'ptr_0', name_type_map['Pointer'], (0, None), (False, None), (None, None)
		yield 'motiongraph_vars', name_type_map['Pointer'], (None, name_type_map['MotiongraphVars']), (False, None), (None, None)
		yield 'dtype', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'num_children', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'children', name_type_map['ArrayPointer'], (None, name_type_map['MRFChild']), (False, None), (None, None)
		yield 'count_4', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'id', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'lua_method', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'count_0', name_type_map['Uint64'], (0, None), (False, None)
		yield 'ptr_0', name_type_map['Pointer'], (0, None), (False, None)
		yield 'motiongraph_vars', name_type_map['Pointer'], (instance.dtype, name_type_map['MotiongraphVars']), (False, None)
		yield 'dtype', name_type_map['Uint64'], (0, None), (False, None)
		yield 'num_children', name_type_map['Uint64'], (0, None), (False, None)
		yield 'children', name_type_map['ArrayPointer'], (instance.num_children, name_type_map['MRFChild']), (False, None)
		yield 'count_4', name_type_map['Uint64'], (0, None), (False, None)
		yield 'id', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)

	def get_ptr_template(self, prop):
		"""ptr_0 is POLYMORPHIC, selected by the sibling `lua_method` string.

		Same idiom as Activity.data_type -> Activity.data. `lua_method` is
		declared before `ptr_0`, so it is already populated when the template is
		resolved.

		Measured over the corpus with no exceptions:

			MotionGraph.StateOutput   -> a State (40 B)
			MotionGraph.BlendToState  -> BlendRecord[] (72 B each; 216 B is the
									     common case, 864 B in HOL_Band)
			every dtype=7 decision node -> ptr_0 is NOT A POINTER AT ALL

		That last case is the important one. On a decision node the word cobra
		reads as the pointer's `pool_index` is really the index into the
		generated `lua_results` Lua table, with the offset always 0:

			VariableResultBool  ptr_0 = 1,0  ->  t[1]
			EndDecisionScope    ptr_0 = 2,0  ->  t[2]   (shared by all six of them)
			VariableResultEnum  ptr_0 = 3,0  ->  t[3]

		which is why three table entries can serve twelve nodes. Returning None
		here leaves it unresolved rather than inventing a target; declaring
		`State` unconditionally (as the format did before) made every
		BlendToState node read its 72-byte records as a 40-byte State.
		"""
		if prop != "ptr_0":
			return
		lua_method = self.lua_method.data
		if lua_method == "MotionGraph.StateOutput":
			return name_type_map["State"]
		if lua_method == "MotionGraph.BlendToState":
			return name_type_map["BlendRecord"]
		logging.debug(f"ptr_0 on {lua_method} is a lua_results index, not a pointer")

