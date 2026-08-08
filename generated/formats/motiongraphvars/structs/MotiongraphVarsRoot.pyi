from generated.formats.motiongraphvars.structs.MotiongraphVarRef import MotiongraphVarRef
from generated.formats.ovl_base.structs.ArrayPointer import ArrayPointer
from generated.formats.ovl_base.structs.MemStruct import MemStruct


class MotiongraphVarsRoot(MemStruct):
    count: int
    vars: ArrayPointer[MotiongraphVarRef]

    def __init__(self, context: object, arg: int = 0, template: object = None, set_default: bool = True) -> None: ...
