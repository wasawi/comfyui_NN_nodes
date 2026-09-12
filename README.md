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
  Images only: a `.json` has nothing to show.
- **prompt_filter** — restricts the automatic modes to files whose prompt
  contains the given text.
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

Writes run concurrently and share one deadline for the whole batch
(`save_timeout`, 30s by default). Encoding a PNG costs milliseconds while the
write can take seconds on network storage, so overlapping them is nearly all
win. At most four writes run at once: a Python thread cannot be killed, so a
write abandoned at the deadline keeps going, and holding its slot until it truly
ends means stragglers throttle the next save instead of accumulating. When the
deadline passes the generation is abandoned so the queue moves on.

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
python tests/test_never_blocks.py
python tests/test_wildcard_raw_text.py
node   tests/test_scope_reference.mjs
```

The `.mjs` suite loads `web/scope_reference.js` against a stub of ComfyUI's `app`
module and drives it through real folder changes. `node --check` only parses a
file; it cannot catch a variable used outside the block it was declared in.

No ComfyUI required; the suites stub `folder_paths` and work on synthetic
fixtures.

## Publishing

Before submitting to the ComfyUI Registry, fill in the `CHANGEME` entries in
`pyproject.toml` (publisher id and repository URL) and add a `LICENSE` file.
