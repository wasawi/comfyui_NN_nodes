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


real_write = W._write_png_tmp


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
check("writes every image in the batch", names == ["img.png", "img_1.png", "img_2.png"], names)
check("all files exist on disk",
      all((ROOT / "output" / "sub" / n).is_file() for n in names))
check("returns the id", out["result"] == ("the-id",), out["result"])

single = w.save_images(batch(1), "sub", "solo", "p", "n")
check("single image gets an unsuffixed name",
      single["ui"]["images"][0]["filename"] == "solo.png")

# ---------- writes overlap ----------
marks = []
W._write_png_tmp = slow_write(0.4, marks)
try:
	t0 = time.monotonic()
	w.save_images(batch(4), "sub", "par", "p", "n", save_timeout=30)
	elapsed = time.monotonic() - t0
finally:
	W._write_png_tmp = real_write
check("4 slow writes overlap rather than queue", elapsed < 4 * 0.4 * 0.75, f"{elapsed:.2f}s vs {4*0.4:.1f}s serial")
starts = sorted(t for kind, t in marks if kind == "start")
check("they really start concurrently", (starts[-1] - starts[0]) < 0.3, f"spread {starts[-1]-starts[0]:.2f}s")

# ---------- the deadline covers the whole batch ----------
W._write_png_tmp = slow_write(5)
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
	W._write_png_tmp = real_write

# Two writes were abandoned and still hold two of the four slots. A fresh call
# whose writes are quick should simply use the remaining capacity, not be refused.
t0 = time.monotonic()
out = w.save_images(batch(3), "sub", "partial", "p", "n", save_timeout=10)
check("partial saturation still lets fast writes through", len(out["ui"]["images"]) == 3)
check("and does not wait on the stuck ones", time.monotonic() - t0 < 3, f"{time.monotonic()-t0:.2f}s")

# Now jam every slot: four writes abandoned at once.
W._write_png_tmp = slow_write(6)
try:
	try:
		w.save_images(batch(4), "sub", "jam", "p", "n", save_timeout=1)
	except RuntimeError:
		pass  # expected: they are abandoned, still holding all four slots
finally:
	W._write_png_tmp = real_write

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

W._write_png_tmp = boom
try:
	w.save_images(batch(1), "sub", "bad", "p", "n", save_timeout=5)
	check("write errors surface", False, "no exception")
except RuntimeError as e:
	check("write errors surface", "disk on fire" in str(e), str(e)[:60])
finally:
	W._write_png_tmp = real_write

reset_slots()

# ---------- an existing file is never replaced ----------
import io
import errno
import contextlib

from PIL import Image as _PILImage

NAMES = ROOT / "output" / "names"
NAMES.mkdir(parents=True, exist_ok=True)


class MarkedImage(FakeImage):
	"""A fake image whose pixel value identifies it after it is written."""

	def __init__(self, k):
		super().__init__(8, 8)
		self._a[:] = (k + 0.5) / 255.0


def marked(n, first=0):
	return [MarkedImage(first + k) for k in range(n)]


def pixel(name):
	with _PILImage.open(NAMES / name) as im:
		return int(np.array(im)[0, 0, 0])


def quiet_save(images, name, **kw):
	with contextlib.redirect_stdout(io.StringIO()):
		return MetaWriter().save_images(images, "names", name, "p", "n", **kw)


def filenames(out):
	return [r["filename"] for r in out["ui"]["images"]]


(NAMES / "keep.png").write_bytes(b"SENTINEL existing image")
out = quiet_save(batch(1), "keep")
check("a taken name is never replaced", (NAMES / "keep.png").read_bytes() == b"SENTINEL existing image")
check("the new image moves to _1", filenames(out) == ["keep_1.png"], filenames(out))

for old in ("run.png", "run_1.png", "run_2.png"):
	(NAMES / old).write_bytes(b"old")
out = quiet_save(batch(1), "run")
check("increments past every taken number", filenames(out) == ["run_3.png"], filenames(out))
check("none of the taken files changed",
      all((NAMES / old).read_bytes() == b"old" for old in ("run.png", "run_1.png", "run_2.png")))

got = [filenames(quiet_save(batch(1), "rep"))[0] for _ in range(3)]
check("saving one name repeatedly keeps incrementing", got == ["rep.png", "rep_1.png", "rep_2.png"], got)

(NAMES / "bat.png").write_bytes(b"old")
out = quiet_save(marked(3, 10), "bat")
check("a batch into a taken name skips it", filenames(out) == ["bat_1.png", "bat_2.png", "bat_3.png"], filenames(out))
check("and keeps its images in order", [pixel(f) for f in filenames(out)] == [10, 11, 12],
      [pixel(f) for f in filenames(out)])


def reversed_delays(tmp, img, info, level):
	v = int(np.array(img)[0, 0, 0])
	time.sleep(max(0.0, (23 - v) * 0.15))  # the first image finishes last
	real_write(tmp, img, info, level)


W._write_png_tmp = reversed_delays
try:
	out = quiet_save(marked(3, 20), "ord", save_timeout=30)
finally:
	W._write_png_tmp = real_write
check("order holds even when later images finish first",
      filenames(out) == ["ord.png", "ord_1.png", "ord_2.png"]
      and [pixel(f) for f in filenames(out)] == [20, 21, 22],
      (filenames(out), [pixel(f) for f in filenames(out)]))


def many(name, workers=4, per=2):
	results = []

	def go():
		results.append(MetaWriter().save_images(batch(per), "names", name, "p", "n", save_timeout=30))

	with contextlib.redirect_stdout(io.StringIO()):
		threads = [threading.Thread(target=go) for _ in range(workers)]
		for t in threads:
			t.start()
		for t in threads:
			t.join()
	return [f for out in results for f in filenames(out)]


got = many("race")
check("concurrent saves of one name never collide", len(got) == 8 and len(set(got)) == 8, got)
check("every one of them is on disk", all((NAMES / f).is_file() for f in got))

real_link = W.os.link


def no_link(*a, **k):
	raise OSError(errno.EPERM, "hard links not supported here")


W.os.link = no_link
try:
	(NAMES / "nolink.png").write_bytes(b"old")
	out = quiet_save(batch(1), "nolink")
	check("without hard links a taken name is still skipped",
	      filenames(out) == ["nolink_1.png"] and (NAMES / "nolink.png").read_bytes() == b"old",
	      filenames(out))
	got = many("nolinkrace")
	check("and concurrent saves still never collide", len(got) == 8 and len(set(got)) == 8, got)
finally:
	W.os.link = real_link

real_replace = W.os.replace


def bad_replace(*a, **k):
	raise OSError(errno.EIO, "simulated failure")


W.os.link, W.os.replace = no_link, bad_replace
try:
	try:
		quiet_save(batch(1), "broken")
		check("a failed move is reported", False, "no exception")
	except RuntimeError:
		check("a failed move is reported", True)
	check("and leaves no empty placeholder behind", not (NAMES / "broken.png").exists())
finally:
	W.os.link, W.os.replace = real_link, real_replace

for k in range(50):
	(NAMES / ("deep.png" if k == 0 else f"deep_{k}.png")).write_bytes(b"old")
calls = {"n": 0}


def counting_link(*a, **k):
	calls["n"] += 1
	return real_link(*a, **k)


W.os.link = counting_link
try:
	first = filenames(quiet_save(batch(1), "deep"))[0]
	before = calls["n"]
	second = filenames(quiet_save(batch(1), "deep"))[0]
	second_calls = calls["n"] - before
finally:
	W.os.link = real_link
check("the first save finds the next free number", first == "deep_50.png", first)
check("a repeat save does not re-probe from zero", second == "deep_51.png" and second_calls == 1,
      (second, second_calls))

time.sleep(0.5)
leftovers = [p.name for d in ("sub", "names") for p in (ROOT / "output" / d).iterdir()
             if p.name.endswith(".tmp")]
check("no partial .tmp files left behind", not leftovers, leftovers)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
