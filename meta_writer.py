#!/usr/bin/env python3
import json
import os
import re
import threading
import uuid

from PIL import Image
from PIL.PngImagePlugin import PngInfo
import numpy as np

import folder_paths
from comfy.cli_args import args


# Hard ceiling on disk I/O for one image. Slower than this and we abort so
# ComfyUI's prompt queue can move on rather than hang.
_SAVE_TIMEOUT_SECONDS = 30.0

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


def _write_with_timeout(write_fn, args_tuple, timeout: float, label: str) -> None:
	"""Run `write_fn(*args_tuple)` in a daemon thread; wait at most `timeout`
	seconds. Raises RuntimeError on timeout. Re-raises any exception the
	worker hit. Returns normally on success."""
	exc_box: list[BaseException] = []
	done = threading.Event()

	def _worker():
		try:
			write_fn(*args_tuple)
		except BaseException as e:  # noqa: BLE001  catch absolutely everything
			exc_box.append(e)
		finally:
			done.set()

	t = threading.Thread(target=_worker, name=f"MetaWriter-{label}", daemon=True)
	t.start()
	if not done.wait(timeout):
		raise RuntimeError(
			f"MetaWriter: disk write for {label} did not complete within "
			f"{timeout:.0f}s; aborting so ComfyUI can move on. The orphaned "
			f"write thread will continue in the background and exit with the "
			f"ComfyUI process."
		)
	if exc_box:
		raise exc_box[0]


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

		results = []

		for idx, image in enumerate(images):
			if len(images) == 1:
				save_name = f"{base_stem}.png"
			else:
				save_name = f"{base_stem}_{idx:05}.png"

			save_path = os.path.join(out_dir, save_name)

			# Convert tensor → uint8 PNG
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

			# Save with a hard timeout so a stalled filesystem can't block the
			# ComfyUI prompt queue forever. Any failure (timeout, permission,
			# disk full, PIL encode error, …) raises RuntimeError out of this
			# node; ComfyUI catches it, logs it, and moves on to the next prompt.
			try:
				_write_with_timeout(
					_write_png_atomic,
					(save_path, img, metadata, self.compress_level),
					_SAVE_TIMEOUT_SECONDS,
					label=save_name,
				)
			except RuntimeError:
				raise
			except BaseException as e:  # noqa: BLE001
				raise RuntimeError(
					f"[MetaWriter] failed to save {save_path}: "
					f"{type(e).__name__}: {e}"
				) from e

			results.append(
				{
					"filename": save_name,
					"subfolder": filename_prefix,
					"type": self.type,
				}
			)

		print(f"[MetaWriter] saved {len(results)} image(s) id={image_id}")

		return {"ui": {"images": results}, "result": (image_id,)}
