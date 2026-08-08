from generated.formats.motiongraph.structs.CurveData import CurveData
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.ovl_base.structs.Pointer import Pointer


class BlendRecord(MemStruct):
    zero_00: int
    zero_01: int
    curve: Pointer[CurveData]
    zero_04: int
    zero_05: int
    zero_06: int
    zero_07: int
    blend_time: float
    zero_09: int
    zero_10: int
    zero_11: int
    zero_12: int
    zero_13: int
    tier: int
    zero_15: int
    zero_16: int
    zero_17: int

    def __init__(self, context: object, arg: int = 0, template: object = None, set_default: bool = True) -> None: ...
