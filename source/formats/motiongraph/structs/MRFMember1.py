# START_GLOBALS
import logging
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.motiongraph.imports import name_type_map

# END_GLOBALS


class MRFMember1(MemStruct):

	"""
	72 bytes
	"""

# START_CLASS

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
