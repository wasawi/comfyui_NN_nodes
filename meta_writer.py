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


def _write_png_atomic(full_path: str, img: Image.Image,
                      pnginfo: PngInfo, compress_level: int) -> None:
	"""Save img as PNG to full_path via tmp + os.replace. Caller catches."""
	parent = os.path.dirname(full_path)
	if parent:
		os.makedirs(parent, exist_ok=True)
	tmp = f"{full_path}.{uuid.uuid4().hex[:8]}.tmp"
	try:
		# Explicit format=PNG; otherwise PIL infers from the .tmp suffix and
		# raises ValueError("unknown file extension: .tmp").
		img.save(tmp, format="PNG", pnginfo=pnginfo, compress_level=compress_level)
		os.replace(tmp, full_path)
	finally:
		if os.path.exists(tmp):
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
							"empty. Batch index is appended if needed."
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
		for idx, image in enumerate(images):
			save_name = f"{base_stem}.png" if len(images) == 1 else f"{base_stem}_{idx:05}.png"

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

			staged.append((save_name, os.path.join(out_dir, save_name), img, metadata))

		pending = []
		for save_name, save_path, img, metadata in staged:
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
				_write_png_atomic,
				(save_path, img, metadata, self.compress_level),
				label=save_name,
			)
			pending.append((save_name, save_path, done, exc_box))

		for save_name, save_path, done, exc_box in pending:
			if not done.wait(left()):
				stuck = [n for n, _, d, _ in pending if not d.is_set()]
				raise RuntimeError(
					f"[MetaWriter] {len(stuck)} write(s) did not finish within "
					f"{save_timeout}s ({', '.join(stuck)}); abandoning this "
					f"generation so the queue can move on. They continue in the "
					f"background and will throttle the next save until they end."
				)
			if exc_box:
				e = exc_box[0]
				raise RuntimeError(
					f"[MetaWriter] failed to save {save_path}: "
					f"{type(e).__name__}: {e}"
				) from e
		results = [
			{"filename": name, "subfolder": filename_prefix, "type": self.type}
			for name, _, _, _ in pending
		]

		print(f"[MetaWriter] saved {len(results)} image(s) id={image_id}")

		return {"ui": {"images": results}, "result": (image_id,)}
