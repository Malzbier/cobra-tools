from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.ovl_base.structs.Pointer import Pointer


class MotiongraphVarDef(MemStruct):
    var_name: Pointer[str]
    kind: int
    zero_16: int
    one_f: float
    ten: int
    default_value: int
    zero_36: int
    enum_name: Pointer[str]

    def __init__(self, context: object, arg: int = 0, template: object = None, set_default: bool = True) -> None: ...
