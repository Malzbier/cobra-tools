# START_GLOBALS
from generated.formats.datastreams.imports import name_type_map
import generated.formats.datastreams.versions as versions

# END_GLOBALS


class DataStreamsRoot(MemStruct):

	"""
	JWE1 16 bytes
	"""

# START_CLASS

	def get_ptr_template(self, prop):
		"""`layer_list`'s element struct differs between games.

		PC2 uses a 56-byte record with FOUR pointers (name, type, location, curve);
		the 48-byte JWE1 record has three and no location. Reading PC2 with the JWE1
		layout desyncs after the first entry, so every later record is garbage.

		Only PC2 is measured, so PC2 is the only game switched. Everything else keeps
		the previous struct and previous behaviour - the 48-byte record is unverified
		for the other titles too, but changing them without data would be guessing.

		Note this hook is only reached because datastreams.xml leaves `layer_list`
		without a template: MemStruct.read_ptrs consults get_ptr_template only when
		the XML template is empty.
		"""
		if prop == "layer_list":
			if versions.is_pc2(self.context):
				return name_type_map["DataStreamsSettingsPC2"]
			return name_type_map["DataStreamsSettings"]
