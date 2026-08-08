from generated.formats.ovl_base.structs.ArrayPointer import ArrayPointer
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.sceneryanimchoices.structs.AnimChoice import AnimChoice


class SceneryAnimChoicesRoot(MemStruct):
    choices: ArrayPointer[AnimChoice]
    count: int
    pad: int

    def __init__(self, context: object, arg: int = 0, template: object = None, set_default: bool = True) -> None: ...
