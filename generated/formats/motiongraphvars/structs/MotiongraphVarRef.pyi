from generated.formats.motiongraphvars.structs.MotiongraphVarDef import MotiongraphVarDef
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.ovl_base.structs.Pointer import Pointer


class MotiongraphVarRef(MemStruct):
    var: Pointer[MotiongraphVarDef]

    def __init__(self, context: object, arg: int = 0, template: object = None, set_default: bool = True) -> None: ...
