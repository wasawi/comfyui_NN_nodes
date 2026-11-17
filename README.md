# NN Nodes

A personal ComfyUI node pack.

## Install

```sh
cd ComfyUI/custom_nodes
git clone <repo-url> comfyui_NN_nodes
```

Restart ComfyUI. No extra dependencies — everything imported (Pillow, numpy,
`folder_paths`, `comfy.cli_args`) already ships with ComfyUI.

## Nodes

Together, **Metadata Reader** and **Metadata Writer** leave a trail from any generated image back to the one that
inspired it. **Metadata Writer** embeds the executed prompt, its earlier drafts
and a lineage id into every PNG it saves; **Metadata Reader** picks an earlier
file back out of a folder and feeds its prompt into a new generation.

### Metadata Reader

Picks one `.json` / `.png` / `.jpg` from a folder and outputs its prompt.

- **pick_mode** — `random`, `random_no_repeat` (every file is seen once before
  any repeats), `fixed`, `incremental`, `decremental`, or `manual`.
- **folder** — a ComfyUI root plus an optional child folder (`output/my_refs`).
  The same value resolves correctly on different machines, so a workflow moves
  between a local install and a remote one without editing paths.
- **reference** — the file used by `manual` mode, with a thumbnail preview.
  Images only: a `.json` has nothing to show. In `manual` mode `folder` is
  ignored and JSON records are unreachable — use an automatic pick mode or
  `id_filter` for those.
- **prompt_filter** — restricts the automatic modes to matching prompts. Every
  word must appear somewhere in the prompt, in any order, so `cat girl` finds
  prompts holding both. Prompts are usually comma-separated tag lists, so the
  words of a phrase are rarely adjacent — quote a run (`"blue eyes"`) to demand
  them together. When nothing matches, the console gives a count per term, which
  separates a typo from a word your library simply doesn't use.
- **id_filter** — jumps straight to the file with a given `id`, reaching any
  file in the folder including `.json` records.
- **run_id** — a fresh UUID per execution. Wire it to the writer's `id` so the
  new image is named and recorded consistently; wire the reader's `id` to the
  writer's `parentId` to record what it came from.

Unreadable or promptless files are stepped over rather than failing the queue —
except in `manual` mode, where an explicit choice that cannot be read is
reported instead of being silently swapped for another file.

### Metadata Writer

Saves images as PNG with the metadata record embedded in a `gen_meta` text
chunk, alongside ComfyUI's own workflow chunk.

The record also carries `node`, the id of the writer node that saved the file (a string such as `"7"`, or `"22:45"` inside a subgraph), so parallel writers sharing a folder can be told apart. Files saved before it have no `node` key; the video writer records it too.

**Never replaces a file.** If `image.png` exists the image becomes
`image_1.png`, then `image_2.png`, … — the same numbering as the video writer.
A batch is numbered the same way and keeps its order, so three images into a
free name give `image.png`, `image_1.png`, `image_2.png`. Each image is written
under a hidden temporary name and only then claimed, with a hard link or, on
volumes without hard links, an exclusive create; both fail on a taken name, so
saves racing for one name can't clobber each other.

Writes run concurrently and share one deadline for the whole batch
(`save_timeout`, 30s by default). Encoding a PNG costs milliseconds while the
write can take seconds on network storage, so overlapping them is nearly all
win. At most four writes run at once: a Python thread cannot be killed, so a
write abandoned at the deadline keeps going, and holding its slot until it truly
ends means stragglers throttle the next save instead of accumulating. When the
deadline passes the generation is abandoned so the queue moves on.

### Metadata Video Writer

Saves a video with the same metadata record, in a `gen_meta` container tag next
to ComfyUI's `prompt` and `workflow` tags, so dropping the video onto ComfyUI
still loads its workflow. The video is written by ComfyUI's own code — the same
`video.save_to()` call its **Save Video** node makes — with the same `format`,
`codec` and `crf` choices (`crf` -1 keeps ComfyUI's default).

- **Never replaces a file.** If `clip.mp4` exists the video becomes
  `clip_1.mp4`, then `clip_2.mp4`, … The final name is claimed with a hard link,
  which fails on a taken name, so two saves racing for one name can't clobber
  each other; volumes without hard links use an exclusive create, which is just
  as atomic.
- **First frame as a PNG** (`save_first_frame`, on by default) next to the video,
  with the same number and the same metadata. The Metadata Reader only reads
  images, so this is what lets it pick a video's prompt as a reference; it also
  gives the video a thumbnail.
- **Long deadline.** Encoding and writing share one `save_timeout` (300s by
  default, up to an hour). At most two saves run at once and an abandoned save
  keeps its slot until it truly ends, as in the image writer. The video is
  encoded under a hidden temporary name ending in its real extension (MP4 only
  keeps custom tags such as `gen_meta` then) and renamed when complete, so a
  half-written video never appears.

### Wildcard Raw Text

`ImpactWildcardProcessor` and `ImpactWildcardEncode` expose only their populated
output, never the raw `wildcard_text` with `__wildcards__` still in it. Wire the
processor's STRING output into **Wildcard Raw Text** and it returns both the raw
prompt and the populated one.

It works because `wildcard_text` is an ordinary widget, so it reaches the backend
in the API prompt, and Impact's server hook only rewrites `populated_text` and
`mode`. The node reads the hidden PROMPT, follows the link on its own input back
to the source, and reads `wildcard_text` from it — walking through intermediate
nodes (reroutes, concats) up to 8 hops, with cycle detection.

**Wildcard Raw Text (by node id)** is the no-wiring fallback: type the wildcard
node's id instead of connecting anything. Useful when the path back passes
through muted branches that drop links.

## Reading the metadata outside ComfyUI

```sh
exiftool -j image.png | jq '.[0].Gen_meta | fromjson | {prompt, negative}'
```

`exiftool` reports the `gen_meta` chunk as `Gen_meta`.

## Notes

- Prompt text is never written to disk by these nodes beyond the images
  themselves. The reader's prompt index is held in memory for the life of the
  process; the only on-disk cache (`output/.metareader_cache.json`) holds
  directory listings and timestamps.
- **Metadata Reader never fails a run.** A missing folder, a deleted reference,
  a filter that matches nothing, or a stale combo value all produce an empty
  prompt with a warning on the console instead of refusing to queue. It also
  declares `VALIDATE_INPUTS(**kwargs)`, so ComfyUI skips validating combo values
  that were built from the filesystem and may have changed since.
- Older records are still readable: fields nested under `request`, civitai
  payloads under `raw.meta`, and the legacy `promt` spelling.
- `web/scope_reference.js` narrows the reference combo to the folder selected
  in `folder`, and `web/hover_preview.js` shows a thumbnail beside a dropdown
  entry as you hover it. Both are presentation only: delete either and the node
  still works, just with a longer list or without hover previews. They exist
  because `INPUT_TYPES` is static and the server is never told what a widget is
  set to until the graph runs, so neither behaviour is reachable from Python.

## Tests

```sh
python tests/test_meta_reader.py
python tests/test_meta_writer.py
COMFYUI_DIR=/path/to/ComfyUI python tests/test_meta_video_writer.py  # with ComfyUI's Python
python tests/test_never_blocks.py
python tests/test_wildcard_raw_text.py
node   tests/test_scope_reference.mjs
```

The `.mjs` suite loads `web/scope_reference.js` against a stub of ComfyUI's `app`
module and drives it through real folder changes. `node --check` only parses a
file; it cannot catch a variable used outside the block it was declared in.

No ComfyUI required; the suites stub `folder_paths` and work on synthetic
fixtures — except the video writer's, which runs ComfyUI's real video code
and so needs ComfyUI's Python and a checkout (`COMFYUI_DIR`); it skips
itself otherwise.

## Publishing

Before submitting to the ComfyUI Registry, fill in the `CHANGEME` entries in
`pyproject.toml` (publisher id and repository URL) and add a `LICENSE` file.
