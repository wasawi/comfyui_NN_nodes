import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wildcard_raw_text import (
	WildcardRawText,
	WildcardRawTextById,
	resolve_wildcard_text,
)

fails = []


def check(label, cond, extra=""):
	print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if extra else ""))
	if not cond:
		fails.append(label)


RAW = "a portrait of __person__ in __place__"

# node 5 feeds node 9 (this node) directly
direct = {
	"5": {"class_type": "ImpactWildcardProcessor",
	      "inputs": {"wildcard_text": RAW, "populated_text": "a portrait of Ada in Paris"}},
	"9": {"class_type": "WildcardRawText", "inputs": {"processed_text": ["5", 0]}},
}

# node 5 -> reroute 7 -> concat 8 -> this node
indirect = {
	"5": {"class_type": "ImpactWildcardEncode", "inputs": {"wildcard_text": RAW}},
	"7": {"class_type": "Reroute", "inputs": {"": ["5", 0]}},
	"8": {"class_type": "StringConcat", "inputs": {"a": ["7", 0], "b": "suffix"}},
	"9": {"class_type": "WildcardRawText", "inputs": {"processed_text": ["8", 0]}},
}

check("direct link resolves", resolve_wildcard_text(direct, "5") == RAW)
check("walks back through the chain", resolve_wildcard_text(indirect, "8") == RAW)
check("missing node -> None", resolve_wildcard_text(direct, "404") is None)
check("no wildcard upstream -> None",
      resolve_wildcard_text({"1": {"class_type": "Foo", "inputs": {}}}, "1") is None)
check("None prompt -> None", resolve_wildcard_text(None, "1") is None)

# A cycle must terminate rather than spin to the depth limit.
cycle = {
	"1": {"class_type": "A", "inputs": {"x": ["2", 0]}},
	"2": {"class_type": "B", "inputs": {"y": ["1", 0]}},
}
check("cycle terminates", resolve_wildcard_text(cycle, "1") is None)

# Chain longer than MAX_DEPTH gives up instead of recursing forever.
deep = {str(i): {"class_type": "Pass", "inputs": {"v": [str(i + 1), 0]}} for i in range(1, 20)}
deep["20"] = {"class_type": "ImpactWildcardProcessor", "inputs": {"wildcard_text": RAW}}
check("respects MAX_DEPTH", resolve_wildcard_text(deep, "1") is None)

# End to end. This is the regression guard: the original code called a class name
# that no longer existed, the exception was swallowed, and raw came back "".
raw, processed = WildcardRawText().doit("populated!", prompt=direct, unique_id="9")
check("doit returns the raw wildcard text", raw == RAW, repr(raw))
check("doit passes processed_text through", processed == "populated!")

raw2, _ = WildcardRawText().doit("p", prompt=indirect, unique_id="9")
check("doit resolves through intermediates", raw2 == RAW, repr(raw2))

# Unlinked / broken input must degrade quietly, not raise.
unlinked = {"9": {"class_type": "WildcardRawText", "inputs": {"processed_text": "typed"}}}
raw3, proc3 = WildcardRawText().doit("p", prompt=unlinked, unique_id="9")
check("unlinked input -> empty, no raise", raw3 == "" and proc3 == "p")
raw4, _ = WildcardRawText().doit("p", prompt=None, unique_id="9")
check("missing prompt -> empty, no raise", raw4 == "")

# By-id variant
check("by id finds text", WildcardRawTextById().doit("5", prompt=direct) == (RAW,))
check("by id tolerates whitespace", WildcardRawTextById().doit("  5 ", prompt=direct) == (RAW,))
check("by id missing node -> empty", WildcardRawTextById().doit("404", prompt=direct) == ("",))
check("by id no prompt -> empty", WildcardRawTextById().doit("5", prompt=None) == ("",))

# Wiring contract
it = WildcardRawText.INPUT_TYPES()
check("asks for hidden PROMPT + UNIQUE_ID",
      it["hidden"] == {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"})
check("processed_text is forceInput", it["required"]["processed_text"][1]["forceInput"] is True)
check("no EA branding in category",
      "EA" not in WildcardRawText.CATEGORY and "EA" not in WildcardRawTextById.CATEGORY)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
