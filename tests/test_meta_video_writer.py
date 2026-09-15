"""The video writer: gen_meta in real videos, written through ComfyUI's own code.

Unlike the other suites this one needs ComfyUI itself — its Python (torch, av)
and its source tree (comfy_api), because the point is to exercise the exact
video.save_to() that ComfyUI's Save Video uses:

	COMFYUI_DIR=/path/to/ComfyUI /path/to/comfyui/python tests/test_meta_video_writer.py
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from fractions import Fraction

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
COMFY = os.path.expanduser(os.environ.get("COMFYUI_DIR", ""))

try:
	import av
	import torch
	from PIL import Image
except ImportError:
	print("SKIP: run with ComfyUI's Python (needs av, torch and Pillow)")
	sys.exit(0)
if not COMFY or not os.path.isdir(os.path.join(COMFY, "comfy_api")):
	print("SKIP: set COMFYUI_DIR to your ComfyUI folder (the one containing comfy_api)")
	sys.exit(0)
sys.path.insert(1, COMFY)  # after the pack, so its modules win

ROOT = pathlib.Path(tempfile.mkdtemp())
(ROOT / "output").mkdir()

import folder_paths  # noqa: E402  ComfyUI's real module, pointed at a temp dir
folder_paths.get_output_directory = lambda: str(ROOT / "output")
from comfy.cli_args import args as comfy_args  # noqa: E402
from comfy_api.latest import InputImpl, Types  # noqa: E402

import meta_video_writer as V  # noqa: E402
from meta_video_writer import MetaVideoWriter  # noqa: E402

fails = []


def check(label, cond, extra=""):
	print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if extra else ""))
	if not cond:
		fails.append(label)


def make_video(frames=6, w=64, h=48, fps=8):
	images = torch.zeros((frames, h, w, 3))
	images[:, :, : w // 2, 0] = 1.0  # left half red, so the first frame isn't blank
	return InputImpl.VideoFromComponents(
		Types.VideoComponents(images=images, frame_rate=Fraction(fps)))


def container_tags(path):
	with av.open(str(path)) as container:
		return {k.lower(): v for k, v in container.metadata.items()}


PROMPT = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "graph text"}}}
EXTRA = {"workflow": {"nodes": [], "links": []}}
OUT = ROOT / "output" / "vids"
W = MetaVideoWriter()


def save(**kw):
	base = dict(video=make_video(), filename_prefix="vids", filename="clip",
	            prompt="FINAL prompt 女孩", negative_prompt="blurry",
	            workflow_prompt=PROMPT, extra_pnginfo=EXTRA)
	base.update(kw)
	return W.save_video(**base)


def leftovers():
	return sorted(p.name for p in OUT.iterdir() if ".tmp." in p.name) if OUT.exists() else []


# ---------- an MP4 with its first frame ----------

out = save(id="abc", intermediate_prompt_1="draft one", intermediate_prompt_3="  ")
check("returns the id", out["result"] == ("abc",), out["result"])
check("previews the video like Save Video",
      out["ui"] == {"images": [{"filename": "clip.mp4", "subfolder": "vids", "type": "output"}],
                    "animated": (True,)}, out["ui"])
check("video written", (OUT / "clip.mp4").is_file())
check("first frame written", (OUT / "clip.png").is_file())
check("no temporary files left", leftovers() == [], leftovers())

expected = {"id": "abc", "prompt": "FINAL prompt 女孩", "negative": "blurry",
            "intermediate_prompt_1": "draft one"}
tags = container_tags(OUT / "clip.mp4")
check("mp4 carries gen_meta", json.loads(tags.get("gen_meta", "null")) == expected, tags.get("gen_meta"))
check("mp4 carries ComfyUI's prompt", json.loads(tags.get("prompt", "null")) == PROMPT)
check("mp4 carries the workflow", json.loads(tags.get("workflow", "null")) == EXTRA["workflow"])

with Image.open(OUT / "clip.png") as frame:
	check("frame carries gen_meta", json.loads(frame.info.get("gen_meta", "null")) == expected)
	check("frame carries prompt and workflow", "prompt" in frame.info and "workflow" in frame.info)
	check("frame is the video's size", frame.size == (64, 48), frame.size)
	r, g, b = frame.convert("RGB").getpixel((4, 24))
	check("frame shows the first frame's content", r > 180 and g < 80, (r, g, b))

# ---------- never replaces ----------

first_size = (OUT / "clip.mp4").stat().st_size
out2 = save(id="second")
check("a second save gets _1", out2["ui"]["images"][0]["filename"] == "clip_1.mp4", out2["ui"])
check("its frame shares the number", (OUT / "clip_1.png").is_file())
check("the first video is untouched",
      json.loads(container_tags(OUT / "clip.mp4")["gen_meta"])["id"] == "abc"
      and (OUT / "clip.mp4").stat().st_size == first_size)
out3 = save(id="third")
check("a third save gets _2", out3["ui"]["images"][0]["filename"] == "clip_2.mp4")

(OUT / "lonely.png").write_bytes(b"an unrelated image with the same stem")
out4 = save(filename="lonely")
check("a clashing frame name moves both to _1",
      out4["ui"]["images"][0]["filename"] == "lonely_1.mp4" and (OUT / "lonely_1.png").is_file())
check("the unrelated png is untouched", (OUT / "lonely.png").read_bytes().startswith(b"an unrelated"))

# Volumes without hard links fall back to an exclusive create; that path must
# refuse a taken name just as firmly.
import errno

_real_link = V.os.link


def _no_link(*a, **k):
	raise OSError(errno.EPERM, "hard links not supported here")


V.os.link = _no_link
try:
	(OUT / "clashy.png").write_bytes(b"an unrelated image")
	out5 = save(filename="clashy")
	check("without hard links a clash still moves to _1",
	      out5["ui"]["images"][0]["filename"] == "clashy_1.mp4"
	      and (OUT / "clashy.png").read_bytes() == b"an unrelated image", out5["ui"])
	check("and its frame still shares the number", (OUT / "clashy_1.png").is_file())
finally:
	V.os.link = _real_link

# ---------- formats and options ----------

out = save(filename="web", codec="av1")
check("auto + av1 makes WebM", out["ui"]["images"][0]["filename"] == "web.webm", out["ui"])
tags = container_tags(OUT / "web.webm")
check("webm carries gen_meta", json.loads(tags.get("gen_meta", "null")).get("prompt") == "FINAL prompt 女孩",
      sorted(tags))

out = save(filename="mkv", format="mkv", crf=30)
check("mkv with a crf", (OUT / "mkv.mkv").is_file() and "gen_meta" in container_tags(OUT / "mkv.mkv"))

save(filename="noframe", save_first_frame=False)
check("no frame when switched off", (OUT / "noframe.mp4").is_file() and not (OUT / "noframe.png").exists())

out = save(filename="", id="")
generated = out["result"][0]
check("empty id and filename name the video after a new UUID",
      len(generated) == 36 and (OUT / f"{generated}.mp4").is_file(), generated)

comfy_args.disable_metadata = True
try:
	save(filename="private")
	check("--disable-metadata leaves the video untagged", "gen_meta" not in container_tags(OUT / "private.mp4"))
	with Image.open(OUT / "private.png") as frame:
		check("--disable-metadata leaves the frame untagged", "gen_meta" not in frame.info)
finally:
	comfy_args.disable_metadata = False

try:
	save(filename="bad", format="webm", codec="h264")
	check("webm with h264 is refused", False)
except RuntimeError as e:
	check("webm with h264 is refused", "WebM" in str(e), e)
check("a failed save leaves no temporary file", leftovers() == [], leftovers())
check("a failed save leaves no video", not any(OUT.glob("bad*")))

# ---------- volumes without hard links ----------

real_link = V.os.link


def no_links(*_a, **_k):
	raise PermissionError("hard links not supported here")


V.os.link = no_links
try:
	save(filename="nolink")
	save(filename="nolink")
	check("without hard links it still never replaces",
	      (OUT / "nolink.mp4").is_file() and (OUT / "nolink_1.mp4").is_file())
finally:
	V.os.link = real_link

# ---------- the deadline ----------

real_save = V._save_files


def slow_save(*a, **k):
	time.sleep(4)
	return real_save(*a, **k)


V._save_files = slow_save
start = time.monotonic()
try:
	save(filename="slow", save_timeout=1)
	check("a slow save trips the deadline", False)
except RuntimeError as e:
	check("a slow save trips the deadline", "within 1s" in str(e), e)
check("and gives up promptly", time.monotonic() - start < 2.5, time.monotonic() - start)
V._save_files = real_save
time.sleep(4.5)  # let the abandoned save finish and hand back its slot
check("the abandoned save still completes", (OUT / "slow.mp4").is_file())
V._SAVE_SLOTS = threading.BoundedSemaphore(V._MAX_CONCURRENT_SAVES)

print()
if fails:
	print(f"{len(fails)} FAILED: {fails}")
	sys.exit(1)
print("all passed")
