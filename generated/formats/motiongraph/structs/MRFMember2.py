from generated.formats.ovl_base.structs.MemStruct import MemStruct

from generated.formats.motiongraph.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class MRFMember2(MemStruct):

	"""
	72 bytes
	only used if transition is in 'id'
	"""

	__name__ = 'MRFMember2'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.curve_length = name_type_map['Float'].from_value(-1.0)
		self.count_3_c = name_type_map['Int'](self.context, 0, None)
		self.num_activities = name_type_map['Uint64'](self.context, 0, None)
		self.count_6_a = name_type_map['Short'](self.context, 0, None)
		self.count_6_b = name_type_map['Short'](self.context, 0, None)
		self.count_6_c = name_type_map['Int'](self.context, 0, None)
		self.transition = name_type_map['Pointer'](self.context, 0, name_type_map['Transition'])
		self.curve = name_type_map['Pointer'](self.context, 0, name_type_map['CurveData'])
		self.trigger = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		self.activities = name_type_map['Pointer'](self.context, self.num_activities, name_type_map['ActivityReference'])
		self.other_mrf = name_type_map['Pointer'](self.context, 0, name_type_map['MRFMember2'])
		self.id = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'transition', name_type_map['Pointer'], (0, name_type_map['Transition']), (False, None), (None, None)
		yield 'curve', name_type_map['Pointer'], (0, name_type_map['CurveData']), (False, None), (None, None)
		yield 'trigger', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'activities', name_type_map['Pointer'], (None, name_type_map['ActivityReference']), (False, None), (None, None)
		yield 'curve_length', name_type_map['Float'], (0, None), (False, -1.0), (None, None)
		yield 'count_3_c', name_type_map['Int'], (0, None), (False, None), (None, None)
		yield 'num_activities', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'other_mrf', name_type_map['Pointer'], (0, name_type_map['MRFMember2']), (False, None), (None, None)
		yield 'count_6_a', name_type_map['Short'], (0, None), (False, None), (None, None)
		yield 'count_6_b', name_type_map['Short'], (0, None), (False, None), (None, None)
		yield 'count_6_c', name_type_map['Int'], (0, None), (False, None), (None, None)
		yield 'id', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'transition', name_type_map['Pointer'], (0, name_type_map['Transition']), (False, None)
		yield 'curve', name_type_map['Pointer'], (0, name_type_map['CurveData']), (False, None)
		yield 'trigger', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'activities', name_type_map['Pointer'], (instance.num_activities, name_type_map['ActivityReference']), (False, None)
		yield 'curve_length', name_type_map['Float'], (0, None), (False, -1.0)
		yield 'count_3_c', name_type_map['Int'], (0, None), (False, None)
		yield 'num_activities', name_type_map['Uint64'], (0, None), (False, None)
		yield 'other_mrf', name_type_map['Pointer'], (0, name_type_map['MRFMember2']), (False, None)
		yield 'count_6_a', name_type_map['Short'], (0, None), (False, None)
		yield 'count_6_b', name_type_map['Short'], (0, None), (False, None)
		yield 'count_6_c', name_type_map['Int'], (0, None), (False, None)
		yield 'id', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)

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

