from generated.formats.motiongraph.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class BlendRecord(MemStruct):

	"""
	72 bytes. The target of MRFMember1.ptr_0 when lua_method is
	MotionGraph.BlendToState -- a blend descriptor. Blocks hold several of
	them: 216 B (3 records) almost everywhere, 864 B (12) in HOL_Band.
	
	Decoded by diffing the only FOUR distinct contents in the whole corpus;
	with so few distinct values the bytes that VARY are exactly the fields.
	
	`curve` is the one that matters and was nearly missed. It reads as two
	zero uint32s in every graph where the blend curve is absent, which is
	every graph in the original sample -- so it was first typed as padding.
	In HOL_Band six of the twelve records have it populated, and typing those
	bytes as ints left six live pointers undeclared: cobra walked past 6
	CurveData blocks and their 6 CurveDataPoint arrays, losing exactly 12 of
	the file's 249 blocks on every round-trip.
	
	`tier` is 16.16 fixed point and only ever 100 / 10 / 1000.
	"""

	__name__ = 'BlendRecord'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.zero_00 = name_type_map['Uint'](self.context, 0, None)
		self.zero_01 = name_type_map['Uint'](self.context, 0, None)
		self.zero_04 = name_type_map['Uint'](self.context, 0, None)
		self.zero_05 = name_type_map['Uint'](self.context, 0, None)
		self.zero_06 = name_type_map['Uint'](self.context, 0, None)
		self.zero_07 = name_type_map['Uint'](self.context, 0, None)
		self.blend_time = name_type_map['Float'](self.context, 0, None)
		self.zero_09 = name_type_map['Uint'](self.context, 0, None)
		self.zero_10 = name_type_map['Uint'](self.context, 0, None)
		self.zero_11 = name_type_map['Uint'](self.context, 0, None)
		self.zero_12 = name_type_map['Uint'](self.context, 0, None)
		self.zero_13 = name_type_map['Uint'](self.context, 0, None)
		self.tier = name_type_map['Uint'](self.context, 0, None)
		self.zero_15 = name_type_map['Uint'](self.context, 0, None)
		self.zero_16 = name_type_map['Uint'](self.context, 0, None)
		self.zero_17 = name_type_map['Uint'](self.context, 0, None)
		self.curve = name_type_map['Pointer'](self.context, 0, name_type_map['CurveData'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'zero_00', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_01', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'curve', name_type_map['Pointer'], (0, name_type_map['CurveData']), (False, None), (None, None)
		yield 'zero_04', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_05', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_06', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_07', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'blend_time', name_type_map['Float'], (0, None), (False, None), (None, None)
		yield 'zero_09', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_10', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_11', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_12', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_13', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'tier', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_15', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_16', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'zero_17', name_type_map['Uint'], (0, None), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'zero_00', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_01', name_type_map['Uint'], (0, None), (False, None)
		yield 'curve', name_type_map['Pointer'], (0, name_type_map['CurveData']), (False, None)
		yield 'zero_04', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_05', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_06', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_07', name_type_map['Uint'], (0, None), (False, None)
		yield 'blend_time', name_type_map['Float'], (0, None), (False, None)
		yield 'zero_09', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_10', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_11', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_12', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_13', name_type_map['Uint'], (0, None), (False, None)
		yield 'tier', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_15', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_16', name_type_map['Uint'], (0, None), (False, None)
		yield 'zero_17', name_type_map['Uint'], (0, None), (False, None)
