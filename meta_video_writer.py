#!/usr/bin/env python3
"""Metadata Video Writer: the Metadata Writer's gen_meta record, for videos.

The video itself is written by ComfyUI's own code — the same video.save_to()
call its Save Video node makes — so encoding, audio, containers and the
prompt / workflow tags behave exactly as they do there.
"""
import json
import os
import re
import threading
import time
import uuid

from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.cli_args import args


# Ceiling on one save, encoding included. A long video can take a minute or
# more to encode and write, so this is far looser than the image writer's.
_SAVE_TIMEOUT_SECONDS = 300.0

# Same backpressure as the image writer: a Python thread cannot be killed, so an
# abandoned save keeps its slot until it genuinely ends, and stragglers throttle
# the next save instead of piling up. Video encodes are heavy, hence only two.
_MAX_CONCURRENT_SAVES = 2
_SAVE_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_SAVES)

# Container tag and PNG tEXt key holding our metadata record.
METADATA_KEY = "gen_meta"

# The choices ComfyUI's Save Video offers.
FORMATS = ["auto", "mp4", "mkv", "webm"]
CODECS = ["auto", "h264", "av1"]


def _safe_filename_stem(raw: str) -> str:
	"""Restrict an arbitrary input string to a filesystem-safe stem.

	Strips path separators and exotic characters; clamps length so very long
	inputs don't trip per-filesystem name limits.
	"""
	cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", (raw or "").strip())
	cleaned = cleaned.strip(".")
	if not cleaned:
		return "video"
	return cleaned[:200]


def _numbered(stem: str, n: int, ext: str) -> str:
	return f"{stem}.{ext}" if n == 0 else f"{stem}_{n}.{ext}"


def _claim_name(tmp_path: str, directory: str, stem: str, ext: str,
                keep_free=()) -> str:
	"""Give tmp_path its final name — stem.ext, else stem_1.ext, stem_2.ext… —
	without ever replacing an existing file. Returns the name used.

	os.link refuses a name that is already taken, so claiming it is atomic even
	when two saves choose the same name at once. `keep_free` lists extensions
	whose same-numbered name must also be unused (the first-frame PNG), so a
	video and its frame always share a number.
	"""
	n = 0
	while True:
		name = _numbered(stem, n, ext)
		target = os.path.join(directory, name)
		if any(os.path.lexists(os.path.join(directory, _numbered(stem, n, other)))
		       for other in keep_free):
			n += 1
			continue
		try:
			os.link(tmp_path, target)
		except FileExistsError:
			n += 1
			continue
		except OSError:
			# No hard links on this volume (some network or FAT drives). Take the
			# name with an exclusive create — atomic, so a concurrent save can't
			# take it too — then move the file onto that empty placeholder. A
			# check-then-rename leaves a window in which rename silently replaces
			# a file that appeared in between.
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
			return name
		os.remove(tmp_path)
		return name


def _write_first_frame(video_path: str, directory: str, stem: str,
                       pnginfo) -> str:
	"""Save the video's first frame as a PNG carrying the same metadata."""
	import av  # ships with ComfyUI

	with av.open(video_path) as container:
		frame = next(container.decode(video=0))
		image = frame.to_image()
	tmp = os.path.join(directory, f".{stem}.{uuid.uuid4().hex[:8]}.tmp.png")
	try:
		image.save(tmp, format="PNG", pnginfo=pnginfo, compress_level=4)
		return _claim_name(tmp, directory, stem, "png")
	finally:
		if os.path.exists(tmp):
			os.remove(tmp)


def _save_files(video, directory, stem, format_name, codec_name, crf,
                metadata, frame_pnginfo):
	"""Encode the video through ComfyUI, then name it (and its first frame).

	Returns (video file name, frame file name or None). Caller catches.
	"""
	from comfy_api.latest import Types  # imported here so an older ComfyUI
	                                     # can still load the rest of the pack

	os.makedirs(directory, exist_ok=True)
	ext = Types.VideoContainer.get_extension(format_name)
	# a hidden temporary name that still ends in the real extension: MP4 only
	# keeps custom tags such as gen_meta when the path ends in .mp4
	tmp = os.path.join(directory, f".{stem}.{uuid.uuid4().hex[:8]}.tmp.{ext}")
	try:
		video.save_to(
			tmp,
			format=Types.VideoContainer(format_name),
			codec=Types.VideoCodec(codec_name),
			metadata=metadata,
			crf=crf,
		)
		keep_free = ("png",) if frame_pnginfo is not None else ()
		video_name = _claim_name(tmp, directory, stem, ext, keep_free)
		frame_name = None
		if frame_pnginfo is not None:
			frame_name = _write_first_frame(
				os.path.join(directory, video_name), directory,
				os.path.splitext(video_name)[0], frame_pnginfo)
		return video_name, frame_name
	finally:
		if os.path.exists(tmp):
			os.remove(tmp)


def _spawn_save(save_fn, args_tuple, label: str):
	"""Run save_fn(*args_tuple) on a daemon thread that already holds a save
	slot. Returns (done_event, result_box, exception_box); the caller decides
	how long to wait. The slot is released only when the thread genuinely
	finishes, so an abandoned save still counts against the cap.
	"""
	result_box: list = []
	exc_box: list = []
	done = threading.Event()

	def _worker():
		try:
			result_box.append(save_fn(*args_tuple))
		except BaseException as e:  # noqa: BLE001  catch absolutely everything
			exc_box.append(e)
		finally:
			done.set()
			_SAVE_SLOTS.release()

	threading.Thread(target=_worker, name=f"MetaVideoWriter-{label}", daemon=True).start()
	return done, result_box, exc_box


class MetaVideoWriter:
	"""Saves a video with prompt metadata embedded under `gen_meta`.

	Same record as the Metadata Writer: prompt, negative, up to three
	intermediate prompts, id and parentId. ComfyUI's prompt and workflow are
	stored alongside, so dropping the video onto ComfyUI loads its workflow.
	"""

	def __init__(self):
		self.output_dir = folder_paths.get_output_directory()
		self.type = "output"

	@classmethod
	def INPUT_TYPES(cls):
		return {
			"required": {
				"video": ("VIDEO", {"tooltip": "The video to save, e.g. from Create Video."}),
				"filename_prefix": ("STRING", {"default": "ComfyUI"}),
				"filename": (
					"STRING",
					{
						"default": "video",
						"tooltip": (
							"Base filename without extension; falls back to the id when\n"
							"empty. An existing file is never replaced: _1, _2, … is\n"
							"added instead."
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
						"tooltip": "Video id. A UUID is generated when left empty.",
					},
				),
				"parentId": (
					"STRING",
					{
						"default": "",
						"tooltip": "Optional id of the reference image; omitted when empty.",
					},
				),
				"format": (
					FORMATS,
					{
						"default": "auto",
						"tooltip": (
							"Container, as in ComfyUI's Save Video. auto uses MP4,\n"
							"or WebM when the codec is AV1."
						),
					},
				),
				"codec": (
					CODECS,
					{
						"default": "auto",
						"tooltip": "auto uses H.264. WebM needs AV1.",
					},
				),
				"crf": (
					"FLOAT",
					{
						"default": -1.0,
						"min": -1.0,
						"max": 63.0,
						"step": 1.0,
						"tooltip": (
							"-1 = ComfyUI's default. Otherwise lower means higher\n"
							"quality and bigger files: H.264 0-51 (23 is typical),\n"
							"AV1 0-63."
						),
					},
				),
				"save_first_frame": (
					"BOOLEAN",
					{
						"default": True,
						"tooltip": (
							"Also save the first frame as a PNG with the same metadata,\n"
							"next to the video. The Metadata Reader can pick it as a\n"
							"reference, and it gives the video a thumbnail."
						),
					},
				),
				"save_timeout": (
					"INT",
					{
						"default": int(_SAVE_TIMEOUT_SECONDS),
						"min": 1,
						"max": 3600,
						"tooltip": (
							"Seconds to wait for the video to be encoded and written.\n"
							"On expiry this generation is abandoned so the queue\n"
							"moves on; the save continues in the background."
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
	FUNCTION = "save_video"
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

	def save_video(
		self,
		video,
		filename_prefix,
		filename,
		prompt,
		negative_prompt,
		intermediate_prompt_1="",
		intermediate_prompt_2="",
		intermediate_prompt_3="",
		id="",
		parentId="",
		format="auto",
		codec="auto",
		crf=-1.0,
		save_first_frame=True,
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

		# Resolved as ComfyUI's Save Video does.
		format_name = format
		codec_name = codec
		if format_name == "auto":
			format_name = "webm" if codec_name == "av1" else "mp4"
		crf_value = None if crf is None or float(crf) < 0 else float(crf)

		video_metadata = None
		frame_pnginfo = None
		if not args.disable_metadata:
			# as in Save Video; save_to() turns each value into JSON itself
			video_metadata = {METADATA_KEY: meta}
			if extra_pnginfo is not None:
				video_metadata.update(extra_pnginfo)
			if workflow_prompt is not None:
				video_metadata["prompt"] = workflow_prompt
		if save_first_frame:
			frame_pnginfo = PngInfo()
			if not args.disable_metadata:
				frame_pnginfo.add_text(METADATA_KEY, json.dumps(meta, ensure_ascii=False))
				if workflow_prompt is not None:
					frame_pnginfo.add_text("prompt", json.dumps(workflow_prompt))
				if extra_pnginfo is not None:
					for k, v in extra_pnginfo.items():
						frame_pnginfo.add_text(k, json.dumps(v))

		out_dir = os.path.join(self.output_dir, filename_prefix)
		# Sanitize the user-supplied filename stem so path separators or exotic
		# chars can't escape out_dir or trip the filesystem. The filename_prefix
		# is left alone — it may legitimately contain slashes for nested folders.
		stem = _safe_filename_stem(filename if (filename or "").strip() else image_id)

		deadline = time.monotonic() + max(1.0, float(save_timeout))

		def left():
			return max(0.0, deadline - time.monotonic())

		# Waiting for a slot is the backpressure: if earlier saves are still
		# stuck, this blocks and then trips the deadline instead of adding yet
		# another encode to a struggling machine or volume.
		if not _SAVE_SLOTS.acquire(timeout=left()):
			raise RuntimeError(
				f"[MetaVideoWriter] timed out after {save_timeout}s waiting for a "
				f"save slot — {_MAX_CONCURRENT_SAVES} earlier saves are still "
				f"unfinished."
			)
		done, result_box, exc_box = _spawn_save(
			_save_files,
			(video, out_dir, stem, format_name, codec_name, crf_value,
			 video_metadata, frame_pnginfo),
			label=stem,
		)
		if not done.wait(left()):
			raise RuntimeError(
				f"[MetaVideoWriter] {stem} was not saved within {save_timeout}s; "
				f"abandoning this generation so the queue can move on. The save "
				f"continues in the background and will throttle the next one "
				f"until it ends."
			)
		if exc_box:
			e = exc_box[0]
			raise RuntimeError(
				f"[MetaVideoWriter] failed to save {stem} in {out_dir}: "
				f"{type(e).__name__}: {e}"
			) from e

		video_name, frame_name = result_box[0]
		print(
			f"[MetaVideoWriter] saved {video_name}"
			+ (f" and {frame_name}" if frame_name else "")
			+ f" id={image_id}"
		)
		preview = [{"filename": video_name, "subfolder": filename_prefix, "type": self.type}]
		return {"ui": {"images": preview, "animated": (True,)}, "result": (image_id,)}
