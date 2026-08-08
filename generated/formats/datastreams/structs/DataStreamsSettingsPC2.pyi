from generated.formats.datastreams.structs.CurveDataPoint import CurveDataPoint
from generated.formats.ovl_base.structs.ArrayPointer import ArrayPointer
from generated.formats.ovl_base.structs.MemStruct import MemStruct
from generated.formats.ovl_base.structs.Pointer import Pointer


class DataStreamsSettingsPC2(MemStruct):
    z_0: int
    ds_name: Pointer[str]
    type: Pointer[str]
    z_1: int
    location: Pointer[str]
    count: int
    data: ArrayPointer[CurveDataPoint]

    def __init__(self, context: object, arg: int = 0, template: object = None, set_default: bool = True) -> None: ...
