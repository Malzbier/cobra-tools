from generated.formats.datastreams.imports import name_type_map
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class DataStreamsSettingsPC2(MemStruct):

	"""
	PC2 56 bytes - FOUR pointers, measured not guessed.
	
	Reading PC2 with the 48-byte JWE1 layout desyncs after the first entry: each
	later entry starts 8 bytes early, so the same string turns up as name_a of one
	entry and name_b of the next, and curve data decodes to nonsense such as
	x=7.18e+22 with sub_curve_type=24931.
	
	Evidence for 56:
	* block size fits align16(56*count) for all 74 PC2 .datastreams entries
	* relocation offsets modulo 56 are SHARP at +8/+16/+32/+48, with counts
	exactly equal to the entry count; modulo 48 they smear over six residues
	* decoding at 56 yields coherent events, e.g. SF_LaserScanner gives
	LaserScanner_Scan_stop / AudioEvent / Arm_Close with a sane 3-point curve
	
	Same shape as the motiongraph's DataStreamResourceData - name, type, placement,
	curve - which is why the Type and Location vocabularies match it.
	"""

	__name__ = 'DataStreamsSettingsPC2'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.z_0 = name_type_map['Uint64'](self.context, 0, None)
		self.z_1 = name_type_map['Uint64'](self.context, 0, None)
		self.count = name_type_map['Uint64'](self.context, 0, None)
		self.ds_name = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		self.type = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		self.location = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		self.data = name_type_map['ArrayPointer'](self.context, self.count, name_type_map['CurveDataPoint'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'z_0', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'ds_name', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'type', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'z_1', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'location', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'count', name_type_map['Uint64'], (0, None), (False, None), (None, None)
		yield 'data', name_type_map['ArrayPointer'], (None, name_type_map['CurveDataPoint']), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'z_0', name_type_map['Uint64'], (0, None), (False, None)
		yield 'ds_name', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'type', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'z_1', name_type_map['Uint64'], (0, None), (False, None)
		yield 'location', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'count', name_type_map['Uint64'], (0, None), (False, None)
		yield 'data', name_type_map['ArrayPointer'], (instance.count, name_type_map['CurveDataPoint']), (False, None)
