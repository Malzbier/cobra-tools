# START_GLOBALS
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.motiongraph.imports import name_type_map

# END_GLOBALS


class TransStruct(MemStruct):

	"""
	24 bytes
	"""

# START_CLASS

	def get_ptr_template(self, prop):
		"""`another_mrf_reference_2` targets an ARRAY OF MRFMember2.

		It was previously left untyped, which preserves the bytes but drops every
		relocation inside them -- so each element's `curve` pointer and the
		CurveData -> CurveDataPoint chain behind it were lost.

		Sizing it from a single asset does not generalise: across the corpus the
		targets are 144 / 216 / 648 / 936 bytes, i.e. 2, 3, 9 and 13 times
		MRFMember2's 72, and the larger ones DO carry relocations.
		"""
		if prop == "another_mrf_reference_2":
			return name_type_map["MRFMember2"]
