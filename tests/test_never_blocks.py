"""The reader must never stop a generation.

Whatever is wrong — a reference that scrolled out of the capped combo list, a
file that was deleted mid-session, a nonsense widget value — extract() returns a
result tuple. It never raises, because raising refuses the whole prompt.
"""

import json
import os
import sys
import tempfile
import pathlib
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import meta_reader as M
from meta_reader import MetaReader

fails = []


def check(label, cond, extra=""):
	print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if extra else ""))
	if not cond:
		fails.append(label)


ROOT = pathlib.Path(tempfile.mkdtemp())
OUT, IN = ROOT / "output", ROOT / "input"
BIG = OUT / "busy_folder"
BIG.mkdir(parents=True)
IN.mkdir()
M.folder_paths = types.SimpleNamespace(
	get_output_directory=lambda: str(OUT),
	get_input_directory=lambda: str(IN),
)

try:
	from PIL import Image
	from PIL.PngImagePlugin import PngInfo
except ImportError:
	print("SKIP: Pillow unavailable")
	sys.exit(0)

# More images than the per-folder cap, so the oldest fall out of the combo list —
# exactly what happens to a busy output folder while generations keep running.
COUNT = M._PER_FOLDER_LIMIT + 20
made = []
for i in range(COUNT):
	info = PngInfo()
	info.add_text("gen_meta", json.dumps({"id": f"img-{i:04d}", "prompt": f"prompt number {i}"}))
	f = BIG / f"img-{i:04d}.png"
	Image.new("RGB", (4, 4)).save(f, pnginfo=info)
	os.utime(f, (1000 + i, 1000 + i))  # oldest first
	made.append(f)

refs = MetaReader._reference_choices()
check("combo is capped", len(refs) == M._PER_FOLDER_LIMIT, len(refs))

oldest_rel = f"busy_folder/{made[0].name}"
check("the oldest image has fallen out of the combo", oldest_rel not in refs)

R = MetaReader()


def result_of(**kw):
	out = R.extract(**kw)
	assert isinstance(out, dict) and "result" in out, out
	assert len(out["result"]) == len(MetaReader.RETURN_TYPES), out
	return out["result"]


# The reported case: selected image is no longer listed, but still on disk.
res = result_of(load_reference=True, folder="output/busy_folder", pick_mode="manual",
                reference=oldest_rel, input_prompt="", unique_id="a")
check("out-of-list but on-disk reference still loads", res[0] == "prompt number 0", res[0])

# Now it really is gone.
made[0].unlink()
res = result_of(load_reference=True, folder="output/busy_folder", pick_mode="manual",
                reference=oldest_rel, input_prompt="", unique_id="b")
check("deleted reference -> empty prompt, no raise", res[0] == "", repr(res[0]))
check("deleted reference -> empty negative", res[1] == "")
check("deleted reference -> run_id still issued", len(res[7]) == 36)

# Automatic modes must not care about the reference widget at all.
for mode in ("random", "random_no_repeat", "fixed", "incremental", "decremental"):
	res = result_of(load_reference=True, folder="output/busy_folder", pick_mode=mode,
	                reference=oldest_rel, input_prompt="", unique_id=f"m-{mode}")
	check(f"{mode} ignores the dead reference", res[0].startswith("prompt number"), res[0][:24])

# ---- fuzz: nothing here may raise ----
corrupt = BIG / "corrupt.png"
corrupt.write_bytes(b"not a png at all")
a_file = BIG / "img-0005.png"

hostile = [
	dict(load_reference=True, folder="output/busy_folder", pick_mode="manual", reference=""),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="manual", reference=M._EMPTY_REFERENCE),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="manual", reference="../../../etc/passwd"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="manual", reference="busy_folder/corrupt.png"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="manual", reference="no/such/dir/x.png"),
	dict(load_reference=True, folder="", pick_mode="random"),
	dict(load_reference=True, folder="output/nonexistent", pick_mode="random"),
	dict(load_reference=True, folder="input", pick_mode="random"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="not_a_mode"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="fixed", fixed_index=10**9),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", sort_by="bogus"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", seed=2**63),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", prompt_filter="zzz-no-match"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", id_filter="nope"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", folder_override=str(a_file)),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", folder_override="/root/forbidden"),
	dict(load_reference=True, folder="output/busy_folder", pick_mode="random", input_prompt=None),
	dict(load_reference=True, folder=None, pick_mode=None),
	dict(load_reference=False, folder=None, pick_mode=None),
]
raised = []
for i, kw in enumerate(hostile):
	try:
		out = R.extract(unique_id=f"f{i}", **kw)
		assert isinstance(out, dict) and "result" in out
		assert all(isinstance(v, str) for v in out["result"])
	except Exception as e:
		raised.append(f"#{i} {type(e).__name__}: {e}")
check(f"none of the {len(hostile)} hostile inputs raise", not raised, raised[:3])

# ---- the reference widget is irrelevant unless manual mode is actually chosen ----
GARBAGE = [
	"", M._EMPTY_REFERENCE, "busy_folder/deleted.png", "no/such/file.png",
	"../../etc/passwd", "not even a path", None,
]

for g in GARBAGE:
	res = result_of(load_reference=False, folder="output/busy_folder",
	                pick_mode="manual", reference=g, input_prompt="typed",
	                unique_id=f"off-{g}")
	if res != ("typed", "", "", "", "", "", "", res[7]):
		check(f"load_reference off ignores reference={g!r}", False, res[:2])
		break
else:
	check(f"load_reference off ignores all {len(GARBAGE)} bad reference values", True)

for g in GARBAGE:
	bad = False
	for mode in ("random", "random_no_repeat", "fixed", "incremental", "decremental"):
		res = result_of(load_reference=True, folder="output/busy_folder",
		                pick_mode=mode, reference=g, input_prompt="",
		                unique_id=f"auto-{mode}-{g}")
		if not res[0].startswith("prompt number"):
			check(f"{mode} ignores reference={g!r}", False, res[0][:30]); bad = True; break
	if bad:
		break
else:
	check("every automatic mode ignores every bad reference value", True)

# extract() must also cope if the argument never arrives at all.
out = R.extract(load_reference=True, folder="output/busy_folder", pick_mode="random",
                unique_id="no-ref-kwarg")
check("extract works with no reference argument at all",
      out["result"][0].startswith("prompt number"), out["result"][0][:24])
out = R.extract(load_reference=False, folder="output/busy_folder", pick_mode="manual",
                input_prompt="typed", unique_id="no-ref-kwarg-off")
check("manual + load_reference off + no reference argument",
      out["result"][0] == "typed", out["result"][0])

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
