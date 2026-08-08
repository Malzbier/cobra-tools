from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.sceneryanimchoices.imports import name_type_map


class SceneryAnimChoicesRoot(MemStruct):

	"""
	16 bytes. The dropdown of animations offered on an animated scenery part.
	"""

	__name__ = 'SceneryAnimChoicesRoot'


	def __init__(self, context, arg=0, template=None, set_default=True):
		super().__init__(context, arg, template, set_default=False)
		self.count = name_type_map['Uint'](self.context, 0, None)
		self.pad = name_type_map['Uint'](self.context, 0, None)
		self.choices = name_type_map['ArrayPointer'](self.context, self.count, name_type_map['AnimChoice'])
		if set_default:
			self.set_defaults()

	@classmethod
	def _get_attribute_list(cls):
		yield from super()._get_attribute_list()
		yield 'choices', name_type_map['ArrayPointer'], (None, name_type_map['AnimChoice']), (False, None), (None, None)
		yield 'count', name_type_map['Uint'], (0, None), (False, None), (None, None)
		yield 'pad', name_type_map['Uint'], (0, None), (False, None), (None, None)

	@classmethod
	def _get_filtered_attribute_list(cls, instance, include_abstract=True):
		yield from super()._get_filtered_attribute_list(instance, include_abstract)
		yield 'choices', name_type_map['ArrayPointer'], (instance.count, name_type_map['AnimChoice']), (False, None)
		yield 'count', name_type_map['Uint'], (0, None), (False, None)
		yield 'pad', name_type_map['Uint'], (0, None), (False, None)
