"""The writer must overlap its writes and give up on a stalled volume.

Encoding a PNG costs milliseconds; the write can take seconds on network
storage. These tests fake a slow disk to check that writes overlap, that one
deadline covers the whole batch, and that abandoned writes throttle the next
call instead of piling up.
"""

import os
import sys
import time
import types
import tempfile
import pathlib
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

ROOT = pathlib.Path(tempfile.mkdtemp())
(ROOT / "output").mkdir()
sys.modules["folder_paths"] = types.SimpleNamespace(
	get_output_directory=lambda: str(ROOT / "output"),
	get_input_directory=lambda: str(ROOT / "input"),
)
comfy = types.ModuleType("comfy")
cli = types.ModuleType("comfy.cli_args")
cli.args = types.SimpleNamespace(disable_metadata=False)
comfy.cli_args = cli
sys.modules["comfy"], sys.modules["comfy.cli_args"] = comfy, cli

import meta_writer as W
from meta_writer import MetaWriter

fails = []


def check(label, cond, extra=""):
	print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if extra else ""))
	if not cond:
		fails.append(label)


class FakeImage:
	"""Stands in for a torch tensor: .cpu().numpy() -> HxWx3 floats in 0..1."""

	def __init__(self, h=64, w=64):
		self._a = np.zeros((h, w, 3), dtype=np.float32)

	def cpu(self):
		return self

	def numpy(self):
		return self._a


def batch(n):
	return [FakeImage() for _ in range(n)]


real_write = W._write_png_atomic


def slow_write(delay, record=None):
	def _w(path, img, info, level):
		if record is not None:
			record.append(("start", time.monotonic()))
		time.sleep(delay)
		real_write(path, img, info, level)
		if record is not None:
			record.append(("end", time.monotonic()))
	return _w


def reset_slots():
	W._WRITE_SLOTS = threading.BoundedSemaphore(W._MAX_CONCURRENT_WRITES)


# ---------- normal operation ----------
w = MetaWriter()
out = w.save_images(batch(3), "sub", "img", "a prompt", "neg", id="the-id")
names = [r["filename"] for r in out["ui"]["images"]]
check("writes every image in the batch", names == ["img_00000.png", "img_00001.png", "img_00002.png"], names)
check("all files exist on disk",
      all((ROOT / "output" / "sub" / n).is_file() for n in names))
check("returns the id", out["result"] == ("the-id",), out["result"])

single = w.save_images(batch(1), "sub", "solo", "p", "n")
check("single image gets an unsuffixed name",
      single["ui"]["images"][0]["filename"] == "solo.png")

# ---------- writes overlap ----------
marks = []
W._write_png_atomic = slow_write(0.4, marks)
try:
	t0 = time.monotonic()
	w.save_images(batch(4), "sub", "par", "p", "n", save_timeout=30)
	elapsed = time.monotonic() - t0
finally:
	W._write_png_atomic = real_write
check("4 slow writes overlap rather than queue", elapsed < 4 * 0.4 * 0.75, f"{elapsed:.2f}s vs {4*0.4:.1f}s serial")
starts = sorted(t for kind, t in marks if kind == "start")
check("they really start concurrently", (starts[-1] - starts[0]) < 0.3, f"spread {starts[-1]-starts[0]:.2f}s")

# ---------- the deadline covers the whole batch ----------
W._write_png_atomic = slow_write(5)
try:
	t0 = time.monotonic()
	try:
		w.save_images(batch(2), "sub", "slow", "p", "n", save_timeout=1)
		check("stalled write raises", False, "no exception")
	except RuntimeError as e:
		took = time.monotonic() - t0
		check("stalled write raises", "did not finish within" in str(e), str(e)[:60])
		check("gives up at the deadline, not per image", took < 2.5, f"{took:.2f}s")
finally:
	W._write_png_atomic = real_write

# Two writes were abandoned and still hold two of the four slots. A fresh call
# whose writes are quick should simply use the remaining capacity, not be refused.
t0 = time.monotonic()
out = w.save_images(batch(3), "sub", "partial", "p", "n", save_timeout=10)
check("partial saturation still lets fast writes through", len(out["ui"]["images"]) == 3)
check("and does not wait on the stuck ones", time.monotonic() - t0 < 3, f"{time.monotonic()-t0:.2f}s")

# Now jam every slot: four writes abandoned at once.
W._write_png_atomic = slow_write(6)
try:
	try:
		w.save_images(batch(4), "sub", "jam", "p", "n", save_timeout=1)
	except RuntimeError:
		pass  # expected: they are abandoned, still holding all four slots
finally:
	W._write_png_atomic = real_write

t0 = time.monotonic()
try:
	w.save_images(batch(1), "sub", "after", "p", "n", save_timeout=1)
	check("fully saturated slots refuse new work", False, "expected a refusal")
except RuntimeError as e:
	check("fully saturated slots refuse new work", "write slot" in str(e), str(e)[:70])
	check("and refuses promptly", time.monotonic() - t0 < 2.5, f"{time.monotonic()-t0:.2f}s")

# ---------- recovery once the stragglers finish ----------
time.sleep(7)
reset_slots()
out = w.save_images(batch(2), "sub", "recovered", "p", "n", save_timeout=30)
check("recovers once the volume frees up", len(out["ui"]["images"]) == 2)

# ---------- a failing write is reported, not swallowed ----------
def boom(path, img, info, level):
	raise OSError("disk on fire")

W._write_png_atomic = boom
try:
	w.save_images(batch(1), "sub", "bad", "p", "n", save_timeout=5)
	check("write errors surface", False, "no exception")
except RuntimeError as e:
	check("write errors surface", "disk on fire" in str(e), str(e)[:60])
finally:
	W._write_png_atomic = real_write

reset_slots()
check("no partial .tmp files left behind",
      not list((ROOT / "output" / "sub").glob("*.tmp")),
      [p.name for p in (ROOT / "output" / "sub").glob("*.tmp")])

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
