import json, os, sys, time, types, tempfile, pathlib, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import meta_reader as M
from meta_reader import MetaReader, read_record, sort_entries

fails = []
def check(label, cond, extra=""):
	print(("PASS " if cond else "FAIL ") + label + ("  " + str(extra) if extra else ""))
	if not cond: fails.append(label)

def expect_error(label, fn, *needles):
	try:
		fn()
		check(label, False, "no error raised")
	except Exception as e:
		msg = str(e)
		check(label, all(n in msg for n in needles), msg[:90])

# ---- fake ComfyUI roots ----
ROOT = pathlib.Path(tempfile.mkdtemp())
OUT, IN = ROOT / "output", ROOT / "input"
(OUT / "jsons_none").mkdir(parents=True)
(OUT / "_IMAGES_OUT_").mkdir()
IN.mkdir()
M.folder_paths = types.SimpleNamespace(
	get_output_directory=lambda: str(OUT),
	get_input_directory=lambda: str(IN),
)

d = OUT / "jsons_none"
def w(name, obj, age=0, where=None):
	p = (where or d) / name
	p.write_text(json.dumps(obj))
	t = time.time() - age
	os.utime(p, (t, t))
	return p

w("aaa-111.json", {"id": "aaa-111", "prompt": "oldest cat"}, age=300)
w("bbb-222.json", {"id": "bbb-222", "prompt": "middle dog"}, age=200)
w("ccc-333.json", {"id": "ccc-333", "prompt": "newest bird in the rain"}, age=100)
w("ddd-444.json", {"id": "ddd-444", "request": {"prompt": "legacy fox", "negativePrompt": "blurry"}}, age=50)
(d / "broken.json").write_text("{not json at all")
w("empty.json", {"id": "empty", "prompt": ""})

R = MetaReader()

# ---------- previously covered behaviour ----------
ents = R._list_candidates(str(d), False, "newest")
check("newest-first sort", ents[0][0].name == "empty.json", ents[0][0].name)
check("oldest-first sort", R._list_candidates(str(d), False, "oldest")[0][0].name == "aaa-111.json")
check("name sort", R._list_candidates(str(d), False, "name")[0][0].name == "aaa-111.json")

ok = True
for i in range(40):
	p, rec = R._pick_and_load(ents, "random", 0, random.Random(i), f"n{i}")
	if not rec["prompt"] or "broken" in p or "empty" in p: ok = False
check("random never returns broken/promptless", ok)

bad = [i for i, (p, _) in enumerate(ents) if p.name in ("broken.json", "empty.json")]
p, rec = R._pick_and_load(ents, "fixed", bad[0], random.Random(1), "fx")
check("fixed steps past bad file", bool(rec["prompt"]), os.path.basename(p))

rec = read_record(json.loads((d / "ddd-444.json").read_text()))
check("legacy request.prompt", rec["prompt"] == "legacy fox")
check("legacy negativePrompt", rec["negative"] == "blurry")

MetaReader._cursors.clear()
a1, _ = R._pick_and_load(ents, "incremental", 0, random.Random(), "nodeA")
b1, _ = R._pick_and_load(ents, "incremental", 0, random.Random(), "nodeB")
a2, _ = R._pick_and_load(ents, "incremental", 0, random.Random(), "nodeA")
check("two nodes keep separate cursors", a1 == b1 and a2 != a1)

MetaReader._decks.clear()
good = {p.name for p, _ in ents if p.name not in ("broken.json", "empty.json")}
seen = [os.path.basename(R._pick_and_load(ents, "random_no_repeat", 0, random.Random(7), "dk")[0]) for _ in good]
check("no_repeat covers all before repeat", set(seen) == good and len(seen) == len(set(seen)), seen)

check("id fast-path by stem", os.path.basename(R._find_by_id(ents, "bbb-222")) == "bbb-222.json")
w("zzz-renamed.json", {"id": "hidden-999", "prompt": "renamed file"})
ents2 = R._list_candidates(str(d), False, "newest")
check("id scan fallback", os.path.basename(R._find_by_id(ents2, "hidden-999")) == "zzz-renamed.json")

sub = d / "sub"; sub.mkdir()
(sub / "deep.json").write_text(json.dumps({"id": "deep", "prompt": "nested prompt"}))
check("recursive finds subfolder",
      len(R._list_candidates(str(d), True, "newest")) == len(R._list_candidates(str(d), False, "newest")) + 1)

p1, _ = R._pick_and_load(ents, "random", 0, random.Random(42), "s1")
p2, _ = R._pick_and_load(ents, "random", 0, random.Random(42), "s2")
check("same seed -> same pick", p1 == p2)

# ---------- folder resolution ----------
check("resolve root", R._resolve_folder("output", "") == OUT)
check("resolve root/child", R._resolve_folder("output/jsons_none", "") == d)
check("resolve input root", R._resolve_folder("input", "") == IN)
check("override wins", R._resolve_folder("output", str(d)) == d)
check("legacy absolute path", R._resolve_folder(str(d), "") == d)
expect_error("empty folder errors", lambda: R._resolve_folder("", ""), "No folder selected")

# ---------- combo contents ----------
choices = MetaReader._folder_choices()
check("folder combo lists roots + children",
      "output" in choices and "output/jsons_none" in choices and "input" in choices, choices)
it = MetaReader.INPUT_TYPES()
check("pick_mode has manual", "manual" in it["required"]["pick_mode"][0])
check("reference has preview flags",
      it["required"]["reference"][1]["image_upload"] is True
      and it["required"]["reference"][1]["image_folder"] == "output")
# The frontend only scans REQUIRED inputs for the image-preview flag, taking the
# first combo that carries it. Guard that placement so it can't regress.
hit = next((k for k, v in it["required"].items()
            if isinstance(v[0], list) and isinstance(v[1], dict) and v[1].get("image_upload")), None)
check("preview attaches to reference (required)", hit == "reference", hit)
check("hidden unique_id", it["hidden"] == {"unique_id": "UNIQUE_ID"})

# INPUT_TYPES must never throw, even with no ComfyUI at all
saved = M.folder_paths
M.folder_paths = None
try:
	it_none = MetaReader.INPUT_TYPES()
	check("INPUT_TYPES survives no ComfyUI", it_none["required"]["folder"][0] == [M._EMPTY_FOLDER])
except Exception as e:
	check("INPUT_TYPES survives no ComfyUI", False, e)
M.folder_paths = saved

# ---------- images, preview items, manual pick ----------
try:
	from PIL import Image
	from PIL.PngImagePlugin import PngInfo
	HAVE_PIL = True
except ImportError:
	HAVE_PIL = False

if HAVE_PIL:
	info = PngInfo()
	info.add_text("gen_meta", json.dumps({"id": "png-1", "prompt": "a woman in the rain", "negative": "ugly"}))
	info.add_text("workflow", json.dumps({"nodes": []}))
	Image.new("RGB", (8, 8)).save(d / "png-1.png", pnginfo=info)
	Image.new("RGB", (8, 8)).save(d / "stray.png")

	refs = MetaReader._reference_choices()
	check("reference combo lists images only",
	      "jsons_none/png-1.png" in refs and not any(r.endswith(".json") for r in refs), refs[:4])

	item = R._result_item(str(d / "png-1.png"))
	check("result item for image", item == {"filename": "png-1.png", "subfolder": "jsons_none", "type": "output"}, item)
	check("no result item for json", R._result_item(str(d / "aaa-111.json")) is None)
	check("no result item outside roots", R._result_item("/tmp/nowhere/x.png") is None)

	path, rec = R._load_manual("jsons_none/png-1.png")
	check("manual loads chosen file", rec["prompt"] == "a woman in the rain", rec["prompt"])

	expect_error("manual missing file", lambda: R._load_manual("jsons_none/nope.png"), "not found", "Press R")
	expect_error("manual unreadable errors by name", lambda: R._load_manual("jsons_none/stray.png"),
	             "stray.png", "no readable metadata")
	expect_error("manual with nothing selected", lambda: R._load_manual(""), "no reference image is selected")

	out = R.extract(True, "output/jsons_none", "manual", reference="jsons_none/png-1.png", unique_id="1")
	check("manual extract returns preview", "ui" in out and out["ui"]["images"][0]["filename"] == "png-1.png")
	check("manual extract result", out["result"][0] == "a woman in the rain" and out["result"][6] == "png-1")
else:
	print("SKIP image tests (no Pillow)")

# ---------- prompt filter + cache ----------
ents3 = R._list_candidates(str(d), False, "newest")
kept = R._apply_prompt_filter(ents3, "rain")
check("prompt_filter narrows", all("rain" in read_record(M.load_json_data(str(p)))["prompt"].lower() for p, _ in kept) and kept)
check("prompt_filter passthrough when empty", R._apply_prompt_filter(ents3, "") is ents3)
expect_error("prompt_filter no match errors", lambda: R._apply_prompt_filter(ents3, "zzzznope"), "matched none")

# Prompt text must never reach disk: the cache file may hold directory
# listings, but never the "entries" namespace that carries prompts.
import json as _json
cf = OUT / ".metareader_cache.json"
if cf.is_file():
	_blob = _json.loads(cf.read_text())
	check("no prompt text persisted to disk", "entries" not in _blob, list(_blob))
	_raw = cf.read_text()
	check("no prompt strings in cache file", "oldest cat" not in _raw and "woman in the rain" not in _raw)
else:
	check("no prompt text persisted to disk", True, "no cache file")
calls = {"n": 0}
real_load = M.load_json_data
def counting(path):
	calls["n"] += 1
	return real_load(path)
M.load_json_data = counting
R._apply_prompt_filter(ents3, "rain")
M.load_json_data = real_load
check("cache avoids re-reading unchanged files", calls["n"] == 0, f"{calls['n']} reads")

# ---------- end to end ----------
out = R.extract(False, "output/jsons_none", "random", input_prompt="just me")
check("load_reference off passes through", out["result"][0] == "just me" and len(out["result"][7]) == 36)
out = R.extract(True, "output/jsons_none", "fixed", input_prompt="PREFIX", fixed_index=0, sort_by="oldest", unique_id="7")
check("prefix is prepended", out["result"][0].startswith("PREFIX "), out["result"][0])
out = R.extract(True, "output/jsons_none", "random", prompt_filter="rain", unique_id="9")
check("extract honours prompt_filter", "rain" in out["result"][0].lower(), out["result"][0])

# ---------- a bad reference must never stop a generation ----------
import inspect

spec = inspect.getfullargspec(MetaReader.VALIDATE_INPUTS)
check("VALIDATE_INPUTS takes **kwargs (ComfyUI then skips combo validation)",
      spec.varkw is not None, spec)
check("VALIDATE_INPUTS accepts anything",
      MetaReader.VALIDATE_INPUTS(reference="(no images found)", folder="nope") is True)

def result_of(**kw):
    """extract() must always return a result tuple, never raise."""
    out = R.extract(**kw)
    assert "result" in out, out
    return out["result"]

# The reported bug: random mode with a stale/garbage reference value.
res = result_of(load_reference=True, folder="output/jsons_none", pick_mode="random",
                reference=M._EMPTY_REFERENCE, input_prompt="base", unique_id="v1")
check("random mode ignores a garbage reference", res[0].startswith("base"), res[0][:40])

# Manual mode pointed at the sentinel: degrades instead of raising.
res = result_of(load_reference=True, folder="output/jsons_none", pick_mode="manual",
                reference=M._EMPTY_REFERENCE, input_prompt="base", unique_id="v2")
check("manual with no selection -> input_prompt, no raise", res == ("base", "", "", "", "", "", "", res[7]), res[:2])

# Folder that does not exist.
res = result_of(load_reference=True, folder="output/does_not_exist", pick_mode="random",
                input_prompt="base", unique_id="v3")
check("missing folder -> no raise", res[0] == "base")

# Folder with no usable files at all.
import tempfile as _tf
empty_dir = pathlib.Path(_tf.mkdtemp())
res = result_of(load_reference=True, folder="output/jsons_none", pick_mode="random",
                folder_override=str(empty_dir), input_prompt="base", unique_id="v4")
check("empty folder -> no raise", res[0] == "base")

# Filters that match nothing.
res = result_of(load_reference=True, folder="output/jsons_none", pick_mode="random",
                prompt_filter="zzzznope", input_prompt="base", unique_id="v5")
check("prompt_filter matching nothing -> no raise", res[0] == "base")
res = result_of(load_reference=True, folder="output/jsons_none", pick_mode="random",
                id_filter="no-such-id", input_prompt="base", unique_id="v6")
check("id_filter not found -> no raise", res[0] == "base")

# With no input_prompt at all the output is simply empty, never an exception.
res = result_of(load_reference=True, folder="output/does_not_exist", pick_mode="random",
                input_prompt="", unique_id="v7")
check("nothing loadable and no input -> empty string", res[0] == "" and res[1] == "")
check("run_id still produced on the failure path", len(res[7]) == 36)

print()
print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
