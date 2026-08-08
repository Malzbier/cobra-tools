# START_GLOBALS
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.motiongraph.imports import name_type_map

# END_GLOBALS


class Something(MemStruct):

	"""
	16 bytes
	"""

# START_CLASS

	def get_ptr_template(self, prop):
		"""`ptr` targets a MotiongraphVar.

		The field is commented "usually empty" in the format definition, and that
		is exactly why it was never typed -- but when it IS populated, leaving it
		untyped drops the target and every string hanging off it.
		"""
		if prop == "ptr":
			return name_type_map["MotiongraphVar"]
