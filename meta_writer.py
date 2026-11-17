#!/usr/bin/env python3
import json
import os
import re
import threading
import time
import uuid

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import numpy as np

import folder_paths
from comfy.cli_args import args


# Ceiling on disk I/O for one save_images call — the whole batch, not each
# image, so a batch of four can't stall the queue for four times as long.
_SAVE_TIMEOUT_SECONDS = 30.0

# Writes run concurrently: encoding a PNG costs tens of milliseconds while the
# write can take seconds on network storage, so overlapping them is nearly all
# win. The cap matters more than the parallelism though — a Python thread cannot
# be killed, so an abandoned write keeps running after its timeout. Holding a
# slot for the thread's real lifetime means those stragglers throttle the next
# call instead of accumulating without limit, which is how a slow volume turns
# into a hung queue.
_MAX_CONCURRENT_WRITES = 4
_WRITE_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_WRITES)

# PNG tEXt key holding our metadata blob.
METADATA_KEY = "gen_meta"


def _safe_filename_stem(raw: str) -> str:
	"""Restrict an arbitrary input string to a filesystem-safe stem.

	Strips path separators and exotic characters; clamps length so very long
	inputs don't trip per-filesystem name limits.
	"""
	cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (raw or "").strip())
	cleaned = cleaned.strip(".")
	if not cleaned:
		return "image"
	return cleaned[:200]


# Next number to try per (directory, stem). Saving the same name repeatedly would
# otherwise re-probe image.png, image_1.png, … from zero on every run — one
# filesystem round trip per existing file, which is slow on network storage.
# Process-local: the first save after a restart probes once from zero.
_NEXT_NUMBER: dict = {}
_NUMBER_LOCK = threading.Lock()


def _numbered(stem: str, n: int, ext: str) -> str:
	return f"{stem}.{ext}" if n == 0 else f"{stem}_{n}.{ext}"


def _claim_name(tmp_path: str, directory: str, stem: str, ext: str,
                start: int = 0) -> tuple:
	"""Give tmp_path its final name — stem.ext, else stem_1.ext, stem_2.ext… —
	without ever replacing an existing file. Returns (name, number).

	Same naming as the video writer. os.link refuses a name that is taken, so
	the claim is atomic even when another save picks the same name at once.
	"""
	n = start
	while True:
		name = _numbered(stem, n, ext)
		target = os.path.join(directory, name)
		try:
			os.link(tmp_path, target)
		except FileExistsError:
			n += 1
			continue
		except OSError:
			# No hard links on this volume (some network mounts). Take the name
			# with an exclusive create instead — still atomic, still ours alone —
			# then move the image onto that empty placeholder.
			try:
				fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
			except FileExistsError:
				n += 1
				continue
			os.close(fd)
			try:
				os.replace(tmp_path, target)
			except BaseException:
				try:
					os.remove(target)
				except OSError:
					pass
				raise
			return name, n
		os.remove(tmp_path)
		return name, n


def _write_png_tmp(tmp_path: str, img: Image.Image, pnginfo: PngInfo,
                   compress_level: int) -> None:
	# Explicit format=PNG; PIL would otherwise infer it from the .tmp suffix.
	img.save(tmp_path, format="PNG", pnginfo=pnginfo, compress_level=compress_level)


def _save_one(directory, stem, img, pnginfo, compress_level,
              idx, claimed, numbers, names, deadline) -> None:
	"""Write one image of a batch, then give it the next free name. Caller catches."""
	tmp = None
	try:
		os.makedirs(directory, exist_ok=True)
		# Hidden, and ending in .tmp rather than .png, so the reader never lists
		# a half-written file.
		tmp = os.path.join(directory, f".{stem}.{uuid.uuid4().hex[:8]}.png.tmp")
		_write_png_tmp(tmp, img, pnginfo, compress_level)

		# Keep the batch in order, but never wait past the deadline: a stuck
		# neighbour must not strand this image unsaved.
		if idx:
			claimed[idx - 1].wait(max(0.0, deadline - time.monotonic()))
		prev = numbers[idx - 1] if idx else None
		start = prev + 1 if prev is not None else 0
		key = (directory, stem)
		with _NUMBER_LOCK:
			start = max(start, _NEXT_NUMBER.get(key, 0))
		name, n = _claim_name(tmp, directory, stem, "png", start)
		numbers[idx] = n
		names[idx] = name
		with _NUMBER_LOCK:
			_NEXT_NUMBER[key] = max(_NEXT_NUMBER.get(key, 0), n + 1)
	finally:
		claimed[idx].set()
		if tmp and os.path.exists(tmp):
			try:
				os.remove(tmp)
			except OSError:
				pass


def _spawn_write(write_fn, args_tuple, label: str):
	"""Run write_fn(*args_tuple) on a daemon thread that already holds a write
	slot. Returns (done_event, exception_box); the caller decides how long to
	wait. The slot is released only when the thread genuinely finishes, so an
	abandoned write still counts against the concurrency cap.

	Daemon threads on purpose: ThreadPoolExecutor joins its workers at exit,
	which would hang ComfyUI's shutdown behind a stuck network write.
	"""
	exc_box: list = []
	done = threading.Event()

	def _worker():
		try:
			write_fn(*args_tuple)
		except BaseException as e:  # noqa: BLE001  catch absolutely everything
			exc_box.append(e)
		finally:
			done.set()
			_WRITE_SLOTS.release()

	threading.Thread(target=_worker, name=f"MetaWriter-{label}", daemon=True).start()
	return done, exc_box


class MetaWriter:
	"""Saves images as PNG with prompt metadata embedded under `gen_meta`.

	Generation parameters (seed, steps, cfg, sampler, checkpoint, …) are not
	duplicated here: the full ComfyUI workflow is embedded alongside, and that
	is the record they are recovered from.
	"""

	def __init__(self):
		self.output_dir = folder_paths.get_output_directory()
		self.type = "output"
		self.compress_level = 4

	@classmethod
	def INPUT_TYPES(cls):
		return {
			"required": {
				"images": ("IMAGE",),
				"filename_prefix": ("STRING", {"default": "ComfyUI"}),
				"filename": (
					"STRING",
					{
						"default": "image",
						"tooltip": (
							"Base filename without extension; falls back to the id when\n"
							"empty. An existing file is never replaced: a taken name\n"
							"moves to _1, _2, … and a batch numbers its images the same way."
						),
					},
				),
				"prompt": (
					"STRING",
					{
						"default": "",
						"multiline": True,
						"tooltip": "The prompt that actually ran.",
					},
				),
				"negative_prompt": ("STRING", {"default": "", "multiline": True}),
			},
			"optional": {
				"intermediate_prompt_1": (
					"STRING",
					{
						"default": "",
						"multiline": True,
						"tooltip": "Earlier prompt draft. Omitted from the metadata when empty.",
					},
				),
				"intermediate_prompt_2": ("STRING", {"default": "", "multiline": True}),
				"intermediate_prompt_3": ("STRING", {"default": "", "multiline": True}),
				"id": (
					"STRING",
					{
						"default": "",
						"tooltip": "Image id. A UUID is generated when left empty.",
					},
				),
				"parentId": (
					"STRING",
					{
						"default": "",
						"tooltip": "Optional id of the reference image; omitted when empty.",
					},
				),
				"save_timeout": (
					"INT",
					{
						"default": int(_SAVE_TIMEOUT_SECONDS),
						"min": 1,
						"max": 600,
						"tooltip": (
							"Seconds to wait for the whole batch to reach disk.\n"
							"On expiry this generation is abandoned so the queue\n"
							"moves on; the writes continue in the background."
						),
					},
				),
			},
			"hidden": {
				# Bound to ComfyUI's PROMPT under a non-colliding parameter name,
				# since the `prompt` widget above already claims that name.
				"workflow_prompt": "PROMPT",
				"extra_pnginfo": "EXTRA_PNGINFO",
				# This node's own id (e.g. "22:45" inside a subgraph), recorded
				# as gen_meta["node"] so a saved file says which writer made it.
				"unique_id": "UNIQUE_ID",
			},
		}

	RETURN_TYPES = ("STRING",)
	RETURN_NAMES = ("id",)
	FUNCTION = "save_images"
	OUTPUT_NODE = True
	CATEGORY = "Metadata"

	def _build_metadata(
		self,
		prompt,
		negative_prompt,
		intermediate_prompt_1,
		intermediate_prompt_2,
		intermediate_prompt_3,
		image_id,
		parent_id,
		node_id=None,
	) -> dict:
		meta = {"id": image_id}

		parent = (parent_id or "").strip()
		if parent:
			meta["parentId"] = parent

		meta["prompt"] = str(prompt or "")
		meta["negative"] = str(negative_prompt or "")

		intermediates = (
			intermediate_prompt_1,
			intermediate_prompt_2,
			intermediate_prompt_3,
		)
		for idx, text in enumerate(intermediates, start=1):
			text = str(text or "").strip()
			if text:
				meta[f"intermediate_prompt_{idx}"] = text

		# Absent for old callers: the key is then omitted, as before.
		if node_id is not None and str(node_id).strip():
			meta["node"] = str(node_id).strip()

		return meta

	def save_images(
		self,
		images,
		filename_prefix,
		filename,
		prompt,
		negative_prompt,
		intermediate_prompt_1="",
		intermediate_prompt_2="",
		intermediate_prompt_3="",
		id="",
		parentId="",
		save_timeout=int(_SAVE_TIMEOUT_SECONDS),
		workflow_prompt=None,
		extra_pnginfo=None,
		unique_id=None,
	):
		image_id = (id or "").strip() or str(uuid.uuid4())

		meta = self._build_metadata(
			prompt,
			negative_prompt,
			intermediate_prompt_1,
			intermediate_prompt_2,
			intermediate_prompt_3,
			image_id,
			parentId,
			unique_id,
		)
		meta_json = json.dumps(meta, ensure_ascii=False)

		out_dir = os.path.join(self.output_dir, filename_prefix)

		# Sanitize the user-supplied filename stem so path separators or exotic
		# chars can't escape out_dir or trip the filesystem. The filename_prefix
		# is left alone — it may legitimately contain slashes for nested folders.
		base_stem = _safe_filename_stem(filename if (filename or "").strip() else image_id)

		deadline = time.monotonic() + max(1.0, float(save_timeout))

		def left():
			return max(0.0, deadline - time.monotonic())

		# Encode everything first. This is the cheap half — tens of milliseconds
		# per image — and doing it up front means the writes, which are the slow
		# half on network storage, all overlap.
		staged = []
		for image in images:
			i = 255.0 * image.cpu().numpy()
			img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))

			metadata = PngInfo()
			if not args.disable_metadata:
				metadata.add_text(METADATA_KEY, meta_json)

				# Standard ComfyUI metadata — the sole record of the generation
				# parameters, and what makes the PNG reloadable as a workflow.
				if workflow_prompt is not None:
					metadata.add_text("prompt", json.dumps(workflow_prompt))
				if extra_pnginfo is not None:
					for k, v in extra_pnginfo.items():
						metadata.add_text(k, json.dumps(v))

			staged.append((img, metadata))

		# A file is only named once it is fully written, because a taken name is
		# never replaced — it moves on to the next number. Each image waits for
		# the one before it to take its number, so a batch keeps its order.
		count = len(staged)
		claimed = [threading.Event() for _ in range(count)]
		numbers: list = [None] * count
		names: list = [None] * count

		pending = []
		for idx, (img, metadata) in enumerate(staged):
			label = f"{base_stem}[{idx}]"
			# Waiting for a slot is the backpressure: if earlier writes are
			# still stuck, this blocks and then trips the deadline instead of
			# adding yet another thread to a struggling volume.
			if not _WRITE_SLOTS.acquire(timeout=left()):
				raise RuntimeError(
					f"[MetaWriter] timed out after {save_timeout}s waiting for a "
					f"write slot — {_MAX_CONCURRENT_WRITES} earlier writes are "
					f"still unfinished. The output volume is not keeping up."
				)
			done, exc_box = _spawn_write(
				_save_one,
				(out_dir, base_stem, img, metadata, self.compress_level,
				 idx, claimed, numbers, names, deadline),
				label=label,
			)
			pending.append((label, done, exc_box))

		for label, done, exc_box in pending:
			if not done.wait(left()):
				stuck = [lb for lb, d, _ in pending if not d.is_set()]
				raise RuntimeError(
					f"[MetaWriter] {len(stuck)} write(s) did not finish within "
					f"{save_timeout}s ({', '.join(stuck)}); abandoning this "
					f"generation so the queue can move on. They continue in the "
					f"background and will throttle the next save until they end."
				)
			if exc_box:
				e = exc_box[0]
				raise RuntimeError(
					f"[MetaWriter] failed to save {label} in {out_dir}: "
					f"{type(e).__name__}: {e}"
				) from e

		moved = [
			(_numbered(base_stem, i, "png"), got)
			for i, got in enumerate(names)
			if got != _numbered(base_stem, i, "png")
		]
		if moved:
			print(
				f"[MetaWriter] {len(moved)} name(s) already taken; saved under the "
				f"next free number instead: "
				+ ", ".join(f"{want} -> {got}" for want, got in moved)
			)

		results = [
			{"filename": name, "subfolder": filename_prefix, "type": self.type}
			for name in names
		]

		print(f"[MetaWriter] saved {len(results)} image(s) id={image_id}")

		return {"ui": {"images": results}, "result": (image_id,)}
