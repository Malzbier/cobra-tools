# START_GLOBALS
from generated.formats.ovl_base.structs.MemStruct import MemStruct

# END_GLOBALS


class MRFMember2(MemStruct):

	"""
	72 bytes
	"""

# START_CLASS

	@classmethod
	def block_tail_template(cls):
		"""This struct's BLOCK can be larger than the struct itself.

		In `datastreamsonly.motiongraph` the target of
		MotiongraphHeader.first_non_transition_state is not 72 bytes but
		72 + N x 48:

			PR_Pirates  456 = 72 + 8 x 48
			FT_Jester   360 = 72 + 6 x 48

		The trailing records are Activity structs - verified from the relocation
		table, where each record's +0 resolves to the string "AnimationActivity"
		(all of them sharing ONE string, so it is also a DAG) and its +8 to a
		96-byte block, which is AnimationActivityData. Activity.get_ptr_template
		already resolves that polymorphically, so nothing else is needed to read
		them once they are reached.

		Nothing in the file points AT offset 72, so calc_size_map merges the whole
		thing into a single block and a reader that stops after the struct never
		sees the tail. Those Activity subtrees were the entire remaining corpus
		deficit: 4 of 100 graphs, losing ~half their blocks each.

		Returns (element class, element size) or None.
		"""
		# imported lazily: ovl_base must not depend on a specific format
		from generated.formats.motiongraph.imports import name_type_map
		return name_type_map["Activity"], 48
