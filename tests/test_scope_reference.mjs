// Executes web/scope_reference.js against a fake node. node --check only parses;
// it cannot catch a ReferenceError from a variable used outside its block, which
// is exactly the bug this file exists to prevent.
import { readFileSync, mkdirSync, copyFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
// The extension imports "../../scripts/app.js", so mirror that layout.
const extDir = join(here, "jsenv", "extensions", "nn");
mkdirSync(extDir, { recursive: true });
copyFileSync(join(here, "..", "web", "scope_reference.js"), join(extDir, "scope_reference.js"));

const { app } = await import(join(here, "jsenv", "scripts", "app.js"));
await import(join(extDir, "scope_reference.js"));

const fails = [];
const check = (label, cond, extra = "") => {
	console.log((cond ? "PASS " : "FAIL ") + label + (extra ? "  " + extra : ""));
	if (!cond) fails.push(label);
};

const ext = app.extensions.find((e) => e.name === "NNNodes.ScopeReference");
check("extension registered", !!ext);

const ALL = [
	"root.png",
	"busy/a.png", "busy/b.png", "busy/c.png",
	"other/x.png",
];

function makeNode(folderValue, referenceValue) {
	const folder = { name: "folder", value: folderValue, options: { values: ["output", "output/busy", "output/other"] } };
	const reference = { name: "reference", value: referenceValue, options: { values: [...ALL] } };
	return { comfyClass: "MetaReader", widgets: [folder, reference], _f: folder, _r: reference };
}
const setFolder = (node, v) => { node._f.value = v; node._f.callback?.call(node._f, v); };

// 1. creation with a bare root must not narrow
let n = makeNode("output", "busy/a.png");
await ext.nodeCreated(n);
check("bare root keeps the whole list", n._r.options.values.length === ALL.length, n._r.options.values.length);

// 2. switching folder narrows — this is the path that threw ReferenceError
n = makeNode("output", "busy/a.png");
await ext.nodeCreated(n);
setFolder(n, "output/busy");
check("switching folder narrows to it", n._r.options.values.join() === "busy/a.png,busy/b.png,busy/c.png", n._r.options.values.join());

// 3. a selection that fell off the list but is still in scope is kept
n = makeNode("output/busy", "busy/gone.png");
await ext.nodeCreated(n);
check("out-of-list selection in scope is preserved", n._r.options.values.includes("busy/gone.png"), n._r.options.values.join());
check("and stays selected", n._r.value === "busy/gone.png", n._r.value);

// 4. a selection from another folder is not dragged along
n = makeNode("output", "busy/a.png");
await ext.nodeCreated(n);
setFolder(n, "output/other");
check("out-of-scope selection is dropped", !n._r.options.values.includes("busy/a.png"), n._r.options.values.join());
check("value repointed into the new folder", n._r.value === "other/x.png", n._r.value);

// 5. a folder with no images shows the sentinel rather than an empty combo
n = makeNode("output/empty", "busy/a.png");
await ext.nodeCreated(n);
check("empty folder shows the sentinel", n._r.options.values.join() === "(no images found)", n._r.options.values.join());

// 6. nothing explodes on a node missing widgets
try {
	await ext.nodeCreated({ comfyClass: "MetaReader" });
	await ext.nodeCreated({ comfyClass: "SomethingElse", widgets: [] });
	ext.loadedGraphNode?.({ comfyClass: "MetaReader" });
	check("malformed nodes do not throw", true);
} catch (e) {
	check("malformed nodes do not throw", false, String(e));
}

console.log();
console.log(fails.length ? "FAILED: " + fails.join(", ") : "ALL PASS");
process.exit(fails.length ? 1 : 0);
