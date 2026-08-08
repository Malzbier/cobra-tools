# START_GLOBALS
import logging
import xml.etree.ElementTree as ET

from generated.array import Array
from generated.formats.ovl_base.structs.Pointer import Pointer
from generated.formats.ovl_base.structs.ArrayPointer import ArrayPointer

FOREACH_MARK = "_foreach_"
# END_GLOBALS


class ForEachPointer(Pointer):

	"""
	a pointer to an array in an ovl memory layout
	"""

# START_CLASS

	# A foreach array whose every element writes zero bytes has nothing to point
	# at, and retail leaves the pointer null rather than shipping an empty one
	# See Pointer.write_ptr for why this is not the default for pointers at large
	SKIP_EMPTY_TARGET = True

	def read_template(self, stream):
		if self.template:
			if isinstance(self.arg, ArrayPointer):
				args = self.arg.data
			else:
				raise AttributeError(f"Unsupported arg {type(self.arg)} for ForEachPointer")
			self.data = Array(self.context, 0, None, (len(args)), self.template, set_default=False)
			# for i, arg in enumerate(args):
			# 	logging.debug(f"Argument {i} = {arg}, template {self.template}")
			self.data[:] = [self.template.from_stream(stream, self.context, arg) for arg in args]

	# @classmethod
	# def _to_xml(cls, instance, elem, debug):
	# 	"""Assigns data self to xml elem"""
	# 	Array._to_xml(instance.data, elem, debug)

	@classmethod
	def _from_xml(cls, instance, elem):
		# a zero element array is a nullptr on the XML path, so foreach nothing
		arg_data = instance.arg.data if instance.arg.data is not None else ()
		instance.data = Array(instance.context, arg_data, None, (len(elem)), instance.template, set_default=False)
		# need set_default to fix dtype according to each member of arg's input array
		instance.data[:] = [instance.template(instance.context, member, instance.template, set_default=True) for member in arg_data]
		for subelem, member in zip(elem, instance.data):
			member._from_xml(member, subelem)
		return instance

	@classmethod
	def to_xml(cls, elem, prop, instance, arg, template, debug):
		if instance.has_data:
			assert FOREACH_MARK in prop
			src_prop = prop.split(FOREACH_MARK)[1]
			sub = elem.find(f'./{src_prop}')
			for subelem, member in zip(sub, instance.data):
				member._to_xml(member, subelem, debug)

	@classmethod
	def from_xml(cls, target, elem, prop, arg, template):
		"""Creates object for parent object 'target', from parent element elem."""
		assert FOREACH_MARK in prop
		src_prop = prop.split(FOREACH_MARK)[1]
		sub = elem.find(f'./{src_prop}')
		instance = cls(target.context, arg, template, set_default=False)
		if sub is None:
			# logging.debug(f"Missing array '{prop}' on XML element '{elem.tag}'")
			cls._from_xml(instance, ())
		else:
			cls._from_xml(instance, sub)
			cls.pool_type_from_xml(sub, instance)
		return instance

