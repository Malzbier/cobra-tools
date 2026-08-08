from generated.formats.sceneryanimchoices.structs.SceneryAnimChoicesRoot import SceneryAnimChoicesRoot
from modules.formats.BaseFormat import MemStructLoader


class SceneryAnimChoicesLoader(MemStructLoader):
	target_class = SceneryAnimChoicesRoot
	extension = ".sceneryanimchoices"
