import { app } from "../../scripts/app.js";

// Narrows the MetaReader "reference" combo to the folder chosen in "folder".
//
// This has to live in the browser. INPUT_TYPES is static and evaluated per node
// class, and the server is never told what a widget is set to until the graph is
// executed — so no pure-Python mechanism can make one combo depend on another.
// The full list is already delivered with the node definition; all this does is
// filter what is in front of the user. Delete this file and the node falls back
// to showing every folder, which is exactly how it behaves without it.

const NODE_CLASS = "MetaReader";
const OUTPUT_PREFIX = "output/";
// Must match _EMPTY_REFERENCE in meta_reader.py so the node's own error message
// fires if someone runs a manual pick with nothing selectable.
const EMPTY = "(no images found)";

function applyScope(node) {
	const widgets = node.widgets;
	if (!widgets) return;
	const folder = widgets.find((w) => w.name === "folder");
	const reference = widgets.find((w) => w.name === "reference");
	if (!folder || !reference || !Array.isArray(reference.options?.values)) return;

	// Keep the unfiltered list; reference.options.values is rewritten below.
	if (!node.__nnAllReferences) {
		node.__nnAllReferences = [...reference.options.values];
	}
	const all = node.__nnAllReferences;
	const selected = String(folder.value ?? "");

	// prefix is read again further down, so it must outlive this block.
	let prefix = "";
	let filtered;
	if (selected.startsWith(OUTPUT_PREFIX)) {
		prefix = selected.slice(OUTPUT_PREFIX.length) + "/";
		filtered = all.filter((v) => v.startsWith(prefix));
	} else {
		// A bare root ("output"), an input/* folder, or a legacy absolute path.
		// Don't narrow: scoping a bare root to just its loose top-level files
		// empties the combo on any library that keeps images in subfolders.
		filtered = all;
	}

	reference.options.values = filtered.length ? filtered : [EMPTY];

	// A busy output folder pushes older images out of the capped list. If the
	// current selection belongs to the folder in scope but is no longer listed,
	// keep it rather than silently repointing the node at a different image: the
	// file is usually still on disk, the node loads it by path, and the backend
	// no longer validates combo values against this list.
	const current = reference.value;
	const inScope = prefix ? String(current).startsWith(prefix) : true;
	if (current && current !== EMPTY && inScope
		&& !reference.options.values.includes(current)) {
		reference.options.values = [current, ...reference.options.values.filter((v) => v !== EMPTY)];
	}

	if (!reference.options.values.includes(reference.value)) {
		reference.value = reference.options.values[0];
	}
}

app.registerExtension({
	name: "NNNodes.ScopeReference",

	async nodeCreated(node) {
		if (node.comfyClass !== NODE_CLASS) return;
		const folder = node.widgets?.find((w) => w.name === "folder");
		if (!folder) return;

		// BaseWidget.setValue() calls callback, which covers picking from the
		// dropdown. But the plain setter is `set value(v){ this._state.value = v }`
		// — it notifies nobody — so any path that assigns widget.value directly
		// would slip past. Hook both; applyScope is idempotent.
		const original = folder.callback;
		folder.callback = function (...args) {
			const result = original?.apply(this, args);
			applyScope(node);
			return result;
		};

		let proto = Object.getPrototypeOf(folder);
		let accessor = null;
		while (proto && !accessor) {
			const desc = Object.getOwnPropertyDescriptor(proto, "value");
			if (desc?.get && desc?.set) accessor = desc;
			proto = Object.getPrototypeOf(proto);
		}
		if (accessor) {
			Object.defineProperty(folder, "value", {
				configurable: true,
				enumerable: true,
				get() {
					return accessor.get.call(this);
				},
				set(v) {
					accessor.set.call(this, v);
					applyScope(node);
				},
			});
		}

		applyScope(node);
	},

	// Widget values are restored after nodeCreated, so a loaded workflow needs
	// the scope reapplied or the combo keeps the default folder's list.
	loadedGraphNode(node) {
		if (node.comfyClass !== NODE_CLASS) return;
		applyScope(node);
	},
});
