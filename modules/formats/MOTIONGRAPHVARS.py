from generated.formats.motiongraphvars.structs.MotiongraphVarsRoot import MotiongraphVarsRoot
from modules.formats.BaseFormat import MemStructLoader


class MotiongraphvarsLoader(MemStructLoader):
	target_class = MotiongraphVarsRoot
	extension = ".motiongraphvars"
	# Same rule as MotiongraphLoader: this reader records block identity (see
	# collect below), so string sharing is reproduced from that identity rather
	# than merged by value on write
	INTERN_STRINGS = False

	def collect(self):
		# An enum variable's `enum_name` points at the SAME string as its
		# `var_name`, so the file is a DAG. Pointer aliasing only engages when the
		# context carries a `recursion` dict, and nothing guarantees a motiongraph
		# was collected first - so create our own, or the two pointers write two
		# copies of the string and the block count comes out one too high
		self.context.recursion = {}
		super().collect()
