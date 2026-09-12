"""Recover the *unprocessed* wildcard text from an Impact wildcard node.

ImpactWildcardProcessor and ImpactWildcardEncode never expose their raw
`wildcard_text` as an output — only the populated result. But `wildcard_text`
is an ordinary required widget, so it travels to the backend inside the API
prompt, and Impact's server hook only rewrites `populated_text` and `mode`.
These nodes ask for the hidden PROMPT dict, walk the link on their input back
to the source node, and read `wildcard_text` straight out of it.

    ImpactWildcardProcessor ──(populated text)──▶ Wildcard Raw Text
                                                    ├─ raw_wildcard_text
                                                    └─ processed_text
"""

import logging

WILDCARD_CLASSES = ("ImpactWildcardProcessor", "ImpactWildcardEncode")
MAX_DEPTH = 8


def resolve_wildcard_text(prompt, node_id, depth=0, seen=None):
	"""Walk back from node_id until a wildcard node is found.

	Intermediate nodes (reroutes, string concat, ShowText…) are traversed by
	following any input that looks like a link, i.e. a [node_id, slot] pair.
	Returns None when nothing is found within MAX_DEPTH.
	"""
	if prompt is None or depth > MAX_DEPTH:
		return None

	# Graphs can contain cycles through muted or bypassed branches; without this
	# the walk would revisit the same node until it hit the depth limit.
	seen = set() if seen is None else seen
	key = str(node_id)
	if key in seen:
		return None
	seen.add(key)

	node = prompt.get(key)
	if node is None:
		return None

	if node.get("class_type") in WILDCARD_CLASSES:
		return node.get("inputs", {}).get("wildcard_text", "")

	for value in node.get("inputs", {}).values():
		if isinstance(value, list) and len(value) == 2:
			found = resolve_wildcard_text(prompt, value[0], depth + 1, seen)
			if found is not None:
				return found
	return None


class WildcardRawText:
	@classmethod
	def INPUT_TYPES(cls):
		return {
			"required": {
				"processed_text": ("STRING", {"forceInput": True}),
			},
			"hidden": {
				"prompt": "PROMPT",
				"unique_id": "UNIQUE_ID",
			},
		}

	RETURN_TYPES = ("STRING", "STRING")
	RETURN_NAMES = ("raw_wildcard_text", "processed_text")
	FUNCTION = "doit"
	CATEGORY = "Prompt"
	DESCRIPTION = (
		"Connect the STRING output of an ImpactWildcardProcessor (or "
		"ImpactWildcardEncode) here. Outputs the raw wildcard prompt "
		"(with __wildcards__ intact) alongside the populated text."
	)

	@classmethod
	def IS_CHANGED(cls, *args, **kwargs):
		# The hidden PROMPT is not part of the cache key, so force re-execution.
		# Cheap node; the cost is that anything downstream also re-runs.
		return float("nan")

	def doit(self, processed_text, prompt=None, unique_id=None):
		raw = ""
		try:
			link = prompt[str(unique_id)]["inputs"]["processed_text"]
			if isinstance(link, list):
				raw = resolve_wildcard_text(prompt, link[0]) or ""
			else:
				# Input was not linked (shouldn't happen with forceInput).
				logging.warning("[WildcardRawText] processed_text is not linked.")
		except Exception as e:
			logging.warning(f"[WildcardRawText] could not resolve wildcard_text: {e}")

		return (raw, processed_text)


class WildcardRawTextById:
	"""Fallback variant: no wiring, just type the node id of the wildcard node.

	Useful when the wildcard node's output is already consumed elsewhere and you
	don't want an extra link, or when the path back to it passes through nodes
	that drop links (muted branches, etc.).
	"""

	@classmethod
	def INPUT_TYPES(cls):
		return {
			"required": {
				"wildcard_node_id": ("STRING", {"default": "", "multiline": False}),
			},
			"hidden": {"prompt": "PROMPT"},
		}

	RETURN_TYPES = ("STRING",)
	RETURN_NAMES = ("raw_wildcard_text",)
	FUNCTION = "doit"
	CATEGORY = "Prompt"

	@classmethod
	def IS_CHANGED(cls, *args, **kwargs):
		return float("nan")

	def doit(self, wildcard_node_id, prompt=None):
		node = (prompt or {}).get(str(wildcard_node_id).strip())
		if node is None:
			logging.warning(f"[WildcardRawTextById] node {wildcard_node_id} not in prompt")
			return ("",)
		return (node.get("inputs", {}).get("wildcard_text", ""),)
