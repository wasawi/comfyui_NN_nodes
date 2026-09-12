"""NN Nodes — a personal ComfyUI node pack.

Metadata Reader / Writer keep a trail from any generated image back to the one
that inspired it: the writer embeds the prompt, its earlier drafts and a lineage
id into every saved PNG, and the reader picks an earlier file back out of a
folder to seed a new generation.

Wildcard Raw Text recovers the unprocessed wildcard prompt from an Impact
wildcard node, which those nodes never expose as an output.
"""

from .meta_reader import MetaReader
from .meta_writer import MetaWriter
from .wildcard_raw_text import WildcardRawText, WildcardRawTextById

NODE_CLASS_MAPPINGS = {
	"MetaReader": MetaReader,
	"MetaWriter": MetaWriter,
	"WildcardRawText": WildcardRawText,
	"WildcardRawTextById": WildcardRawTextById,
}

NODE_DISPLAY_NAME_MAPPINGS = {
	"MetaReader": "Metadata Reader",
	"MetaWriter": "Metadata Writer",
	"WildcardRawText": "Wildcard Raw Text",
	"WildcardRawTextById": "Wildcard Raw Text (by node id)",
}

# Serves web/ at /extensions/<this folder name>/ — nothing references the folder
# name directly, so the pack keeps working under any clone directory.
WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
