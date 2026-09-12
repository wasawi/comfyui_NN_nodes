#!/usr/bin/env python3
import os
import re
import json
import random
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
	from PIL import Image
	HAVE_PIL = True
except Exception:
	HAVE_PIL = False

try:
	import folder_paths
except Exception:
	folder_paths = None


# Inline LoRA tags: <lora:name:0.8>
LORA_TAG_RE = re.compile(
	r"<\s*lora\s*:\s*([^:>]+?)\s*(?:\:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)))?\s*>",
	re.IGNORECASE,
)

MEDIA_SUFFIXES = (".json", ".png", ".jpg", ".jpeg")
# Only these can show a preview, so only these go in the manual combo.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")

SORT_MODES = ["newest", "oldest", "name"]

PICK_MODES = ["random", "random_no_repeat", "fixed", "incremental", "decremental", "manual"]

ROOTS = ("output", "input")

# Candidates to try before giving up. A folder nearly always holds a few files
# that carry no prompt (stray screenshots, PNGs from other nodes); without this
# a single one of them would fail the whole queue.
_MAX_PICK_ATTEMPTS = 8

# Entries offered in the manual combo. The list is searchable by typing, but a
# very long one still bogs the UI down; anything older is reachable via id_filter.
_REFERENCE_LIMIT = 1500
# ...and no more than this from any one folder. A flat newest-first cap lets
# whichever folder was written to most recently crowd out every other one.
_PER_FOLDER_LIMIT = 150
# Runaway guard only. It must stay well above a realistic library: os.walk
# visits directories in arbitrary order, so a limit that actually binds doesn't
# trim the oldest files, it silently drops whichever folders were visited last.
_SCAN_LIMIT = 50000

_EMPTY_REFERENCE = "(no images found)"
_EMPTY_FOLDER = "(no folders found)"


def _strip_lora_tags(prompt: str) -> str:
	"""Remove <lora:...> tags so they don't reach a text encoder as literal text."""
	clean = LORA_TAG_RE.sub(" ", prompt or "")
	return re.sub(r"\s{2,}", " ", clean).strip()


def _first(*values):
	"""First value that is neither None nor an empty string."""
	for v in values:
		if v is not None and v != "":
			return v
	return None


def natural_sort_key(s: str):
	return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", s)]


def sort_entries(entries: List[Tuple[Path, float]], sort_by: str) -> List[Tuple[Path, float]]:
	"""Order (path, mtime) pairs. Newest-first by default: filenames are UUIDs,
	so alphabetical order carries no meaning for a MetaWriter library."""
	if sort_by == "name":
		return sorted(entries, key=lambda e: natural_sort_key(e[0].name))
	if sort_by == "oldest":
		return sorted(entries, key=lambda e: e[1])
	return sorted(entries, key=lambda e: e[1], reverse=True)


def read_record(data: Dict[str, Any]) -> Dict[str, Any]:
	"""Pull the prompt fields out of one metadata dict.

	Understands the current flat schema written by MetaWriter alongside the
	older shapes from the previous node set: fields nested under "request",
	civitai payloads
	under "raw"."meta", and the legacy "promt" typo.
	"""
	if not isinstance(data, dict):
		raise ValueError(f"expected a JSON object, got {type(data).__name__}")

	request = data.get("request")
	if not isinstance(request, dict):
		request = {}
	raw = data.get("raw")
	raw_meta = raw.get("meta") if isinstance(raw, dict) else None
	if not isinstance(raw_meta, dict):
		raw_meta = {}

	prompt = _first(
		request.get("prompt"),
		data.get("prompt"),
		data.get("promt"),  # legacy typo
		raw_meta.get("prompt"),
	)
	negative = _first(
		request.get("negativePrompt"),
		data.get("negativePrompt"),
		data.get("negative"),
		data.get("negative_prompt"),
		raw_meta.get("negativePrompt"),
	)

	return {
		"prompt": str(prompt or ""),
		"negative": str(negative or ""),
		"intermediates": [
			str(data.get(f"intermediate_prompt_{i}", "") or "") for i in (1, 2, 3)
		],
		"id": str(data.get("id", "") or ""),
	}


# ----------------------- metadata loading (module level) -----------------------

# Our own tEXt keys, newest first. Checked before any generic scan so a
# ComfyUI workflow chunk can never be mistaken for the metadata record.
# "pw_json" is the tEXt key the previous node set used. It stays for reading
# an existing library; nothing writes it any more.
_PNG_META_KEYS = ("gen_meta", "pw_json")
# ComfyUI's own chunks: valid JSON, but never our metadata.
_PNG_IGNORED_KEYS = ("prompt", "workflow")

# EXIF tag constants (mirror those in har2image.py)
_EXIF_IFD_TAG = 0x8769           # ExifIFD pointer in main IFD
_EXIF_USER_COMMENT_TAG = 0x9286  # UserComment inside ExifIFD


def load_json_from_png(png_path: str) -> Dict[str, Any]:
	if not HAVE_PIL:
		raise RuntimeError("[MetaReader] Pillow not available; cannot read PNG metadata")
	with Image.open(str(png_path)) as im:
		info = getattr(im, "info", {}) or {}

	for key in _PNG_META_KEYS:
		v = info.get(key)
		if isinstance(v, str):
			try:
				return json.loads(v)
			except Exception as e:
				print(f"[MetaReader] {os.path.basename(png_path)}: {key} is not valid JSON: {e}")

	# Unknown producer: fall back to the first JSON object that isn't ComfyUI's.
	for k, v in info.items():
		if k in _PNG_IGNORED_KEYS or not isinstance(v, str):
			continue
		vs = v.strip()
		if not (vs.startswith("{") and vs.endswith("}")):
			continue
		try:
			return json.loads(vs)
		except Exception:
			continue

	raise RuntimeError(f"[MetaReader] Could not find embedded JSON in PNG: {png_path}")


def load_json_from_jpeg(jpeg_path: str) -> Dict[str, Any]:
	"""Read embedded JSON from a JPEG's EXIF UserComment field.

	Format matches what har2image.py writes: an 8-byte EXIF character-code
	prefix followed by UTF-8 JSON.
	"""
	if not HAVE_PIL:
		raise RuntimeError("[MetaReader] Pillow not available; cannot read JPEG metadata")
	with Image.open(str(jpeg_path)) as im:
		exif = im.getexif()
		exif_ifd = exif.get_ifd(_EXIF_IFD_TAG) or {}
		user_comment = exif_ifd.get(_EXIF_USER_COMMENT_TAG)

	if user_comment is None:
		raise RuntimeError(f"[MetaReader] No EXIF UserComment in JPEG: {jpeg_path}")
	if isinstance(user_comment, str):
		user_comment = user_comment.encode("latin-1")

	try:
		return json.loads(user_comment[8:].decode("utf-8"))
	except Exception as e:
		raise RuntimeError(
			f"[MetaReader] EXIF UserComment is not valid JSON in {jpeg_path}: {e}"
		)


def load_json_data(path: str) -> Dict[str, Any]:
	ext = os.path.splitext(path)[1].lower()
	if ext == ".json":
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	if ext == ".png":
		return load_json_from_png(path)
	if ext in (".jpg", ".jpeg"):
		return load_json_from_jpeg(path)
	raise RuntimeError(f"[MetaReader] Unsupported file extension: {path}")


# ----------------------- ComfyUI roots -----------------------


def _root_dir(root: str) -> Optional[Path]:
	"""Absolute path of ComfyUI's output/input directory, or None outside ComfyUI."""
	if folder_paths is None:
		return None
	try:
		if root == "input":
			return Path(folder_paths.get_input_directory())
		return Path(folder_paths.get_output_directory())
	except Exception:
		return None


def scan_files(root: Path, suffixes, limit: int = _SCAN_LIMIT) -> List[Tuple[Path, float]]:
	"""Walk root for files with the given suffixes, returning (path, mtime).

	os.walk with followlinks=False: a symlinked directory pointing back up the
	tree would otherwise walk forever.
	"""
	found: List[Tuple[Path, float]] = []
	for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
		dirnames[:] = [d for d in dirnames if not d.startswith(".")]
		for name in filenames:
			if not name.lower().endswith(suffixes):
				continue
			p = Path(dirpath) / name
			try:
				found.append((p, p.stat().st_mtime))
			except OSError:
				continue
			if len(found) >= limit:
				print(
					f"[MetaReader] scan hit the {limit} file ceiling under {root}; "
					f"some folders are missing from the list."
				)
				return found
	return found


# ----------------------- prompt cache -----------------------

_CACHE_VERSION = 1


def _cache_file() -> Optional[Path]:
	out = _root_dir("output")
	return (out / ".metareader_cache.json") if out else None


def _read_blob() -> Dict[str, Any]:
	p = _cache_file()
	if not p or not p.is_file():
		return {}
	try:
		with open(p, "r", encoding="utf-8") as f:
			blob = json.load(f)
	except Exception:
		return {}
	if not isinstance(blob, dict) or blob.get("version") != _CACHE_VERSION:
		return {}
	return blob


def _write_blob(blob: Dict[str, Any]) -> None:
	p = _cache_file()
	if not p:
		return
	tmp = p.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
	try:
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(blob, f)
		os.replace(tmp, p)
	except Exception as e:
		print(f"[MetaReader] could not write cache: {e}")
		try:
			if tmp.exists():
				tmp.unlink()
		except OSError:
			pass


def _load_ns(name: str) -> Dict[str, Any]:
	got = _read_blob().get(name)
	return got if isinstance(got, dict) else {}


def _save_ns(name: str, value: Dict[str, Any]) -> None:
	# Read-modify-write: the prompt cache and the directory cache share one file
	# and are written from different code paths.
	blob = _read_blob()
	blob["version"] = _CACHE_VERSION
	blob[name] = value
	_write_blob(blob)


# Prompt text is deliberately NOT persisted. Prompts can be confidential client
# material, and the on-disk cache lives in the output directory, which gets
# synced between machines. This one is process-local: the first prompt_filter
# after a restart re-reads the files, and every later one is free.
_prompt_cache: Dict[str, Any] = {}


def load_cache() -> Dict[str, Any]:
	return _prompt_cache


def save_cache(entries: Dict[str, Any]) -> None:
	_prompt_cache.update(entries)


def load_dir_cache() -> Dict[str, Any]:
	return _load_ns("dirs")


def save_dir_cache(dirs: Dict[str, Any]) -> None:
	_save_ns("dirs", dirs)


def newest_images_in(folder: Path, limit: int) -> List[str]:
	"""Newest image filenames directly inside folder (no recursion)."""
	found: List[Tuple[str, float]] = []
	try:
		with os.scandir(folder) as it:
			for e in it:
				if not e.name.lower().endswith(IMAGE_SUFFIXES):
					continue
				try:
					found.append((e.name, e.stat().st_mtime))
				except OSError:
					continue
	except OSError:
		return []
	found.sort(key=lambda x: x[1], reverse=True)
	return [n for n, _ in found[:limit]]


def cached_prompts(
	entries: List[Tuple[Path, float]],
	cache: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], bool]:
	"""Map path -> {"prompt", "id"} for the given files, reading only those whose
	mtime changed since last time. Returns (map, dirty)."""
	cache = load_cache() if cache is None else cache
	out: Dict[str, Dict[str, Any]] = {}
	dirty = False
	for path, mtime in entries:
		key = str(path)
		hit = cache.get(key)
		if isinstance(hit, dict) and hit.get("mtime") == mtime:
			out[key] = hit
			continue
		entry = {"mtime": mtime, "prompt": "", "id": ""}
		try:
			rec = read_record(load_json_data(key))
			entry["prompt"] = rec["prompt"]
			entry["id"] = rec["id"]
		except Exception:
			pass  # unreadable: cached as empty so it isn't re-read every refresh
		cache[key] = entry
		out[key] = entry
		dirty = True
	return out, dirty


class MetaReader:
	"""Reads prompt metadata from a folder of .json / .png / .jpeg files.

	With load_reference off (the default) nothing is read at all: input_prompt
	passes straight through and the folder is never touched. With it on, one
	file is picked (manually, by id_filter, or per pick_mode) and its prompt is
	appended to input_prompt.

	Old-format files written by the previous node set are still
	understood, alongside the current flat schema written by MetaWriter.
	"""

	# Walk position and shuffle deck, keyed by node instance + folder so two
	# readers in one graph never share a cursor. Process-local: both reset when
	# ComfyUI restarts.
	_cursors: Dict[str, int] = {}
	_decks: Dict[str, List[str]] = {}

	# ----------------------- combo contents -----------------------

	@classmethod
	def _folder_choices(cls) -> List[str]:
		"""Each root plus its immediate child folders: output, output/jsons_none, …"""
		choices: List[str] = []
		for root in ROOTS:
			base = _root_dir(root)
			if base is None or not base.is_dir():
				continue
			choices.append(root)
			try:
				kids = [
					d.name for d in base.iterdir()
					if d.is_dir() and not d.name.startswith(".")
				]
			except OSError:
				kids = []
			choices.extend(f"{root}/{k}" for k in sorted(kids, key=natural_sort_key))
		return choices or [_EMPTY_FOLDER]

	@classmethod
	def _reference_choices(cls) -> List[str]:
		"""Images under the output root, newest first, as root-relative paths.

		Values stay plain paths because the frontend builds the preview by
		splitting the widget value on its last '/' — decorating the label with a
		prompt snippet would break the preview. Searching by prompt text is the
		prompt_filter widget's job instead.
		"""
		base = _root_dir("output")
		if base is None or not base.is_dir():
			return [_EMPTY_REFERENCE]

		# One layer only, mirroring the folder combo. A recursive walk of the whole
		# output tree is not viable here: an archive folder can hold hundreds of
		# thousands of files, and this runs on every frontend refresh.
		try:
			children = sorted(
				(d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")),
				key=lambda d: natural_sort_key(d.name),
			)
		except OSError:
			children = []

		dir_cache = load_dir_cache()
		dirty = False
		rels: List[str] = []

		for folder in [base] + children:
			if len(rels) >= _REFERENCE_LIMIT:
				break
			try:
				dir_mtime = folder.stat().st_mtime
			except OSError:
				continue

			# A directory's mtime moves when files are added or removed, so an
			# unchanged folder never needs its files stat'd again.
			key = str(folder)
			hit = dir_cache.get(key)
			if isinstance(hit, dict) and hit.get("mtime") == dir_mtime:
				names = hit.get("items") or []
			else:
				names = newest_images_in(folder, _PER_FOLDER_LIMIT)
				dir_cache[key] = {"mtime": dir_mtime, "items": names}
				dirty = True

			prefix = "" if folder == base else f"{folder.name}/"
			rels.extend(f"{prefix}{n}" for n in names[:_REFERENCE_LIMIT - len(rels)])

		if dirty:
			save_dir_cache(dir_cache)
		return rels or [_EMPTY_REFERENCE]

	@classmethod
	def INPUT_TYPES(cls):
		# A throw here would drop the node from the menu entirely, so every
		# filesystem lookup falls back to a placeholder instead.
		try:
			folders = cls._folder_choices()
		except Exception as e:
			print(f"[MetaReader] could not list folders: {e}")
			folders = [_EMPTY_FOLDER]
		try:
			references = cls._reference_choices()
		except Exception as e:
			print(f"[MetaReader] could not list reference images: {e}")
			references = [_EMPTY_REFERENCE]

		return {
			"required": {
				"load_reference": (
					"BOOLEAN",
					{
						"default": False,
						"tooltip": (
							"Off: no file is read, prompt = input_prompt verbatim.\n"
							"On: pick a file from the folder and append its prompt."
						),
					},
				),
				"folder": (
					folders,
					{
						"tooltip": (
							"Where the automatic pick modes look, as a ComfyUI root\n"
							"plus an optional child folder. Press R to pick up newly\n"
							"created folders. Ignored in manual mode."
						),
					},
				),
				"pick_mode": (
					PICK_MODES,
					{
						"default": "random",
						"tooltip": (
							"How to pick the file:\n"
							"• random           – random file each run\n"
							"• random_no_repeat – random, but every file is seen\n"
							"                     once before any repeats\n"
							"• fixed            – use fixed_index\n"
							"• incremental      – walk forward through the list\n"
							"• decremental      – walk backward through the list\n"
							"• manual           – use the reference image below"
						),
					},
				),
				"reference": (
					references,
					{
						"image_upload": True,
						"image_folder": "output",
						"tooltip": (
							"The file used when pick_mode is 'manual'; ignored otherwise.\n"
							"Images only — a .json has nothing to preview. To pick a\n"
							".json by hand, paste its id into id_filter instead.\n"
							"Type a folder name in the dropdown's filter box to\n"
							"narrow the list to that folder."
						),
					},
				),
			},
			"optional": {
				"input_prompt": (
					"STRING",
					{
						"default": "",
						"multiline": True,
						"tooltip": (
							"Base prompt. Passed through untouched when load_reference\n"
							"is off; the reference prompt is appended to it when on."
						),
					},
				),
				"id_filter": (
					"STRING",
					{
						"default": "",
						"multiline": False,
						"tooltip": (
							"If non-empty, pick the file whose data['id'] matches.\n"
							"Overrides pick_mode when found, and reaches any file in\n"
							"the folder including .json records."
						),
					},
				),
				"prompt_filter": (
					"STRING",
					{
						"default": "",
						"multiline": False,
						"tooltip": (
							"If non-empty, the automatic pick modes only consider\n"
							"files whose prompt contains this text (case-insensitive)."
						),
					},
				),
				"fixed_index": (
					"INT",
					{
						"default": 0,
						"min": 0,
						"max": 100000,
						"step": 1,
						"tooltip": (
							"Index into the sorted file list when pick_mode == 'fixed'.\n"
							"Clamped to valid range."
						),
					},
				),
				"sort_by": (
					SORT_MODES,
					{
						"default": "newest",
						"tooltip": (
							"Order of the file list, which fixed_index and the\n"
							"incremental/decremental walks run over:\n"
							"• newest – most recently modified first\n"
							"• oldest – oldest first\n"
							"• name   – natural filename order"
						),
					},
				),
				"recursive": (
					"BOOLEAN",
					{
						"default": False,
						"tooltip": "Include files in subfolders.",
					},
				),
				"seed": (
					"INT",
					{
						"default": 0,
						"min": 0,
						"max": 0xFFFFFFFFFFFFFFFF,
						"tooltip": (
							"Controls the random pick modes.\n"
							"0 = different every run; any other value repeats\n"
							"the same pick, so a run can be recreated."
						),
					},
				),
				"folder_override": (
					"STRING",
					{
						"default": "",
						"multiline": False,
						"tooltip": (
							"Absolute path to a library outside output/input.\n"
							"Wins over the folder combo when set; no preview."
						),
					},
				),
			},
			"hidden": {"unique_id": "UNIQUE_ID"},
		}

	RETURN_TYPES = (
		"STRING",  # prompt
		"STRING",  # negative
		"STRING",  # intermediate_prompt_1
		"STRING",  # intermediate_prompt_2
		"STRING",  # intermediate_prompt_3
		"STRING",  # json_filename
		"STRING",  # id
		"STRING",  # run_id
	)
	RETURN_NAMES = (
		"prompt",
		"negative",
		"intermediate_prompt_1",
		"intermediate_prompt_2",
		"intermediate_prompt_3",
		"json_filename",
		"id",
		"run_id",
	)

	FUNCTION = "extract"
	CATEGORY = "Metadata"

	# ----------------------- folder resolution -----------------------

	def _resolve_folder(self, folder: str, folder_override: str) -> Path:
		"""Turn the widget values into a directory.

		folder_override wins when set. Otherwise `folder` reads as
		"<root>[/<child>]"; a bare absolute path is still honoured so graphs
		saved before the combo existed keep working.
		"""
		override = (folder_override or "").strip()
		if override:
			return Path(override).expanduser()

		folder = (folder or "").strip()
		if not folder or folder == _EMPTY_FOLDER:
			raise RuntimeError("[MetaReader] No folder selected.")

		root, _, sub = folder.partition("/")
		if root in ROOTS:
			base = _root_dir(root)
			if base is None:
				raise RuntimeError(
					f"[MetaReader] ComfyUI's '{root}' directory is unavailable."
				)
			return (base / sub) if sub else base

		# Legacy absolute path stored by an older version of this node.
		return Path(folder).expanduser()

	def _result_item(self, path: str) -> Optional[Dict[str, str]]:
		"""Describe the picked file for ComfyUI's node preview, if it's an image
		living under a root the /view endpoint serves."""
		if not str(path).lower().endswith(IMAGE_SUFFIXES):
			return None
		p = Path(path)
		for root in ROOTS:
			base = _root_dir(root)
			if base is None:
				continue
			try:
				rel = p.resolve().relative_to(base.resolve())
			except (ValueError, OSError):
				continue
			return {
				"filename": rel.name,
				"subfolder": rel.parent.as_posix() if rel.parent != Path(".") else "",
				"type": root,
			}
		return None

	# ----------------------- file selection -----------------------

	def _list_candidates(
		self,
		folder,
		recursive: bool,
		sort_by: str,
	) -> List[Tuple[Path, float]]:
		folder_path = Path(folder or "").expanduser()
		if not folder_path.is_dir():
			raise RuntimeError(f"[MetaReader] Folder not found: {folder_path}")

		if recursive:
			entries = scan_files(folder_path, MEDIA_SUFFIXES)
		else:
			entries = []
			for p in folder_path.iterdir():
				if p.suffix.lower() not in MEDIA_SUFFIXES:
					continue
				try:
					st = p.stat()
				except OSError:
					continue
				if not p.is_file():
					continue
				entries.append((p, st.st_mtime))

		if not entries:
			raise RuntimeError(
				f"[MetaReader] No .json/.png/.jpg/.jpeg files found in folder: {folder_path}"
				+ (" (searched subfolders)" if recursive else "")
			)
		return sort_entries(entries, sort_by)

	def _apply_prompt_filter(
		self,
		entries: List[Tuple[Path, float]],
		needle: str,
	) -> List[Tuple[Path, float]]:
		"""Keep only files whose prompt contains `needle`, using the mtime-keyed
		cache so a big library isn't re-read on every run."""
		needle = needle.strip().lower()
		if not needle:
			return entries

		meta, _ = cached_prompts(entries)

		kept = [e for e in entries if needle in meta.get(str(e[0]), {}).get("prompt", "").lower()]
		if not kept:
			raise RuntimeError(
				f"[MetaReader] prompt_filter={needle!r} matched none of "
				f"{len(entries)} files."
			)
		print(f"[MetaReader] prompt_filter={needle!r} kept {len(kept)}/{len(entries)}")
		return kept

	def _next_cursor(self, node_key: str, step: int) -> int:
		cur = self.__class__._cursors.get(node_key, 0)
		self.__class__._cursors[node_key] = cur + step
		return cur

	def _deck_pop(self, node_key: str, paths: List[str], rng: random.Random) -> str:
		"""Draw from a shuffled deck, refilling once exhausted, so every file is
		seen before any repeats. Entries that vanished from the folder are
		skipped rather than handed back."""
		available = set(paths)
		deck = self.__class__._decks.get(node_key) or []
		while deck:
			cand = deck.pop()
			if cand in available:
				self.__class__._decks[node_key] = deck
				return cand

		deck = list(paths)
		rng.shuffle(deck)
		cand = deck.pop()
		self.__class__._decks[node_key] = deck
		print(f"[MetaReader] deck refilled ({len(paths)} files)")
		return cand

	def _pick_candidate(
		self,
		paths: List[str],
		pick_mode: str,
		fixed_index: int,
		attempt: int,
		rng: random.Random,
		node_key: str,
	) -> str:
		n = len(paths)
		mode = (pick_mode or "random").lower()

		if mode == "fixed":
			# attempt shifts along the list so a retry lands somewhere new.
			base = max(0, min(int(fixed_index), n - 1))
			return paths[(base + attempt) % n]
		if mode == "incremental":
			return paths[self._next_cursor(node_key, 1) % n]
		if mode == "decremental":
			return paths[self._next_cursor(node_key, -1) % n]
		if mode == "random_no_repeat":
			return self._deck_pop(node_key, paths, rng)
		return paths[rng.randrange(n)]

	def _candidate_stream(
		self,
		paths: List[str],
		pick_mode: str,
		fixed_index: int,
		rng: random.Random,
		node_key: str,
		budget: int,
	):
		"""Yield distinct candidate paths, picker's choice first.

		A random draw can name a file already tried; that costs a draw but not a
		slot of the budget, and once the draws are spent anything still untried is
		offered in list order. So a usable file is never left unexamined just
		because the dice repeated themselves.
		"""
		tried: set = set()
		for _ in range(budget * 4 + len(paths)):
			if len(tried) >= budget:
				return
			path = self._pick_candidate(
				paths, pick_mode, fixed_index, len(tried), rng, node_key
			)
			if path in tried:
				continue
			tried.add(path)
			yield path

		for path in paths:
			if len(tried) >= budget:
				return
			if path in tried:
				continue
			tried.add(path)
			yield path

	def _pick_and_load(
		self,
		entries: List[Tuple[Path, float]],
		pick_mode: str,
		fixed_index: int,
		rng: random.Random,
		node_key: str,
	) -> Tuple[str, Dict[str, Any]]:
		"""Pick a file and read it, stepping past any that turn out to be
		unreadable or promptless."""
		paths = [str(p) for p, _ in entries]
		budget = min(_MAX_PICK_ATTEMPTS, len(paths))
		skipped: List[str] = []

		for path in self._candidate_stream(
			paths, pick_mode, fixed_index, rng, node_key, budget
		):
			try:
				record = read_record(load_json_data(path))
			except Exception as e:
				skipped.append(f"{os.path.basename(path)} ({e})")
				continue
			if not record["prompt"]:
				skipped.append(f"{os.path.basename(path)} (no prompt)")
				continue

			if skipped:
				print(f"[MetaReader] skipped {len(skipped)}: {'; '.join(skipped)}")
			print(f"[MetaReader] pick_mode={pick_mode} → {os.path.basename(path)}")
			return path, record

		raise RuntimeError(
			f"[MetaReader] No usable file in {len(skipped)} tried. "
			f"Skipped: {'; '.join(skipped) if skipped else 'none'}"
		)

	def _load_manual(self, reference: str) -> Tuple[str, Dict[str, Any]]:
		"""Load the hand-picked reference.

		An explicit choice that can't be read is an error, not something to
		quietly step past: silently substituting another file would be far more
		confusing than saying which one failed.
		"""
		rel = (reference or "").strip()
		if not rel or rel == _EMPTY_REFERENCE:
			raise RuntimeError(
				"[MetaReader] pick_mode is 'manual' but no reference image is selected."
			)

		base = _root_dir("output")
		if base is None:
			raise RuntimeError("[MetaReader] ComfyUI's output directory is unavailable.")
		path = base / rel
		if not path.is_file():
			raise RuntimeError(
				f"[MetaReader] reference not found: {path}. Press R to refresh the list."
			)

		try:
			record = read_record(load_json_data(str(path)))
		except Exception as e:
			raise RuntimeError(f"[MetaReader] reference {rel!r} has no readable metadata: {e}")
		if not record["prompt"]:
			raise RuntimeError(f"[MetaReader] reference {rel!r} carries no prompt.")

		print(f"[MetaReader] pick_mode=manual → {rel}")
		return str(path), record

	def _find_by_id(
		self,
		entries: List[Tuple[Path, float]],
		id_filter: str,
	) -> Optional[str]:
		"""Locate a file by its data['id'].

		MetaWriter names files after the id, so the stem almost always matches
		and the whole folder never has to be opened and parsed.
		"""
		for p, _ in entries:
			if p.stem != id_filter:
				continue
			try:
				if read_record(load_json_data(str(p)))["id"] == id_filter:
					print(f"[MetaReader] id={id_filter!r} matched by filename → {p.name}")
					return str(p)
			except Exception:
				pass  # name matched but contents didn't; fall through to the scan
			break

		print(f"[MetaReader] scanning {len(entries)} files for id={id_filter!r}")
		for p, _ in entries:
			try:
				data = load_json_data(str(p))
			except Exception as e:
				print(f"[MetaReader] Skipping {p.name}: failed to load JSON: {e}")
				continue
			if str(data.get("id", "") or "") == id_filter:
				print(f"[MetaReader] Found matching id={id_filter!r} in file={p.name}")
				return str(p)
		return None

	# ----------------------- loading (instance wrappers) -----------------------

	def _load_json_from_png(self, png_path: str) -> Dict[str, Any]:
		return load_json_from_png(png_path)

	def _load_json_from_jpeg(self, jpeg_path: str) -> Dict[str, Any]:
		return load_json_from_jpeg(jpeg_path)

	def _load_json_data(self, path: str) -> Dict[str, Any]:
		return load_json_data(path)

	# ----------------------- main -----------------------

	@classmethod
	def IS_CHANGED(cls, *args, **kwargs):
		# Force re-execution each run; selection happens inside extract().
		return random.random()

	@classmethod
	def VALIDATE_INPUTS(cls, **kwargs):
		# Taking **kwargs makes ComfyUI skip its own input validation for this
		# node — execution.py guards those checks with `not validate_has_kwargs`.
		#
		# It matters because every combo here is built from the filesystem, so a
		# stored value can be entirely reasonable and still be missing from the
		# list this session: a folder emptied, a library that lives on another
		# machine, a selection the web extension narrowed. Refusing to queue the
		# prompt over that is the wrong trade, especially since the automatic
		# pick modes don't even read `reference`. A value that turns out to be
		# unusable is handled in extract(), which returns an empty prompt.
		return True

	def _acquire(
		self,
		folder: str,
		pick_mode: str,
		reference: str,
		id_filter: str,
		prompt_filter: str,
		fixed_index: int,
		sort_by: str,
		recursive: bool,
		seed: int,
		folder_override: str,
		unique_id,
	):
		"""Pick a reference file and read it. Raises on any failure; extract()
		turns that into an empty result rather than letting it stop a run."""
		mode = (pick_mode or "random").lower()
		id_filter = (id_filter or "").strip()

		if mode == "manual" and not id_filter:
			return self._load_manual(reference)
		else:
			resolved = self._resolve_folder(folder, folder_override)
			entries = self._list_candidates(resolved, recursive, sort_by)
			# Folder is part of the key so pointing the node elsewhere restarts
			# the walk instead of resuming at a position from the previous folder.
			node_key = f"{unique_id}:{resolved}"
			rng = random.Random(seed) if seed else random.Random()

			if id_filter:
				path = self._find_by_id(entries, id_filter)
				if not path:
					raise RuntimeError(
						f"[MetaReader] id_filter={id_filter!r} not found in {resolved} "
						f"(no file has data['id'] == that value)."
					)
				return path, read_record(load_json_data(path))
			else:
				entries = self._apply_prompt_filter(entries, prompt_filter)
				return self._pick_and_load(
					entries, mode, fixed_index, rng, node_key
				)

	def extract(
		self,
		load_reference: bool,
		folder: str,
		pick_mode: str,
		input_prompt: str = "",
		reference: str = "",
		id_filter: str = "",
		prompt_filter: str = "",
		fixed_index: int = 0,
		sort_by: str = "newest",
		recursive: bool = False,
		seed: int = 0,
		folder_override: str = "",
		unique_id=None,
	):
		run_id = str(uuid.uuid4())
		base_prompt = str(input_prompt or "")

		def without_reference(reason=None):
			if reason is not None:
				print(
					f"[MetaReader] no reference loaded ({reason}); "
					f"continuing with input_prompt only."
				)
			return {"result": (base_prompt, "", "", "", "", "", "", run_id)}

		if not load_reference:
			print("[MetaReader] load_reference off; passing input_prompt through.")
			return without_reference()

		try:
			path, record = self._acquire(
				folder, pick_mode, reference, id_filter, prompt_filter,
				fixed_index, sort_by, recursive, seed, folder_override, unique_id,
			)
		except Exception as e:
			# A missing folder, an unreadable file, a filter that matches nothing,
			# a combo value left over from another machine — none of it is worth
			# stopping a generation over. Warn and fall through with what we have.
			return without_reference(e)

		prompt_raw = record["prompt"]
		if base_prompt:
			prompt_raw = f"{base_prompt} {prompt_raw}".strip()

		clean_prompt = _strip_lora_tags(prompt_raw)
		negative = record["negative"]
		image_id = record["id"]
		json_filename = os.path.basename(path)

		print(
			f"[MetaReader] {json_filename} "
			f"prompt={clean_prompt[:80]!r} negative={negative[:80]!r} "
			f"id={image_id!r} run_id={run_id}"
		)

		result = (
			clean_prompt,
			negative,
			record["intermediates"][0],
			record["intermediates"][1],
			record["intermediates"][2],
			json_filename,
			image_id,
			run_id,
		)
		preview = self._result_item(path)
		if preview:
			return {"ui": {"images": [preview]}, "result": result}
		return {"result": result}
