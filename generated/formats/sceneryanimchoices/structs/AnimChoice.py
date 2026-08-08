from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.sceneryanimchoices.imports import name_type_map


class AnimChoice(MemStruct):

	"""
	16 bytes
	
	The label pointer targets the loc SYMBOL the dropdown displays, e.g.
	'[InfoPanel_AnimationType_MyProp_Default]'. The sibling
	*_loopanimselection.enumnamer holds the INTERNAL names instead
	(Default / Static / Idle / Sing).
	
	`index` is the value stored in a placed prop's save data, so the order of
	these entries is APPEND-ONLY for anyone authoring a part: inserting or
	reordering changes what already-placed props play.
	
	NOTE the field is called `label`, not `name`. BaseStruct's serialiser has
	SKIPS = ("_context", "arg", "name", "io_start", "io_size", "template"),
	so a field literally named `name` is silently dropped in BOTH directions.
	"""

	__name__ = 'AnimChoice'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.index = name_type_map['Uint'](self.context, 0, None)
		self.duration = name_type_map['Float'](self.context, 0, None)
		self.label = name_type_map['Pointer'](self.context, 0, name_type_map['ZString'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'label', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None), (None, None)
		yield 'index', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'duration', name_type_map['Float'], (0, None), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'label', name_type_map['Pointer'], (0, name_type_map['ZString']), (False, None)
		yield 'index', name_type_map['Uint'], (0, None), (False, None)
		yield 'duration', name_type_map['Float'], (0, None), (False, None)
