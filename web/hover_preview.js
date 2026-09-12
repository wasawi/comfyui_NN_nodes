import { app } from "../../scripts/app.js";

// Shows a thumbnail beside an entry of the MetaReader "reference" dropdown as
// you hover it, so a folder of UUID filenames can be browsed by eye.
//
// The technique follows pysssss's betterCombos: LiteGraph renders the dropdown
// as a .litecontextmenu appended to <body>, so a MutationObserver is the only
// way to catch it. Unlike betterCombos we need no server route to map an entry
// to a picture — the entry text already is the image's path under the output
// directory, so /view can be addressed directly.

const NODE_CLASS = "MetaReader";
const WIDGET_NAME = "reference";
const PREVIEW_PX = 250;

const style = document.createElement("style");
style.textContent = `
.nn-combo-image {
	position: absolute;
	width: ${PREVIEW_PX}px;
	height: ${PREVIEW_PX}px;
	object-fit: contain;
	object-position: top left;
	z-index: 9999;
	background: rgba(0, 0, 0, 0.75);
	border: 1px solid rgba(255, 255, 255, 0.15);
	border-radius: 4px;
	pointer-events: none;
}
`;
document.head.append(style);

const imageHost = document.createElement("img");
imageHost.className = "nn-combo-image";

function viewUrl(relPath) {
	const cut = relPath.lastIndexOf("/");
	const params = new URLSearchParams({
		filename: cut === -1 ? relPath : relPath.slice(cut + 1),
		subfolder: cut === -1 ? "" : relPath.slice(0, cut),
		type: "output",
		// Server-side downscale. Measured on this library: a 6.6 MB PNG comes
		// back as 339 KB, which is the difference between a thumbnail that
		// appears instantly on hover and one that crawls in.
		preview: "webp;60",
	});
	return `/view?${params}`;
}

function placeBeside(item) {
	const rect = item.getBoundingClientRect();
	const spaceRight = window.innerWidth - rect.right;
	const left = spaceRight > PREVIEW_PX + 20 ? rect.right + 10 : rect.left - PREVIEW_PX - 10;
	const top = Math.min(rect.top, window.innerHeight - PREVIEW_PX - 10);
	imageHost.style.left = `${Math.max(10, left)}px`;
	imageHost.style.top = `${Math.max(10, top)}px`;
}

function decorate(menu) {
	for (const item of menu.querySelectorAll(".litemenu-entry")) {
		const value = item.getAttribute("data-value")?.trim();
		if (!value || !/\.(png|jpe?g)$/i.test(value)) continue;

		item.addEventListener(
			"mouseover",
			() => {
				imageHost.src = viewUrl(value);
				document.body.appendChild(imageHost);
				placeBeside(item);
			},
			{ passive: true }
		);
		const hide = () => imageHost.remove();
		item.addEventListener("mouseout", hide, { passive: true });
		item.addEventListener("click", hide, { passive: true });
	}
}

app.registerExtension({
	name: "NNNodes.HoverPreview",
	setup() {
		new MutationObserver((mutations) => {
			const node = app.canvas?.current_node;
			if (node?.comfyClass !== NODE_CLASS) return;

			for (const mutation of mutations) {
				for (const removed of mutation.removedNodes) {
					if (removed.classList?.contains("litecontextmenu")) imageHost.remove();
				}
				for (const added of mutation.addedNodes) {
					if (!added.classList?.contains("litecontextmenu")) continue;
					if (app.canvas.getWidgetAtCursor()?.name !== WIDGET_NAME) return;
					requestAnimationFrame(() => {
						// The right-click node menu is also a .litecontextmenu; only the
						// combo dropdown carries a filter box.
						if (!added.querySelector(".comfy-context-menu-filter")) return;
						decorate(added);
					});
					return;
				}
			}
		}).observe(document.body, { childList: true, subtree: false });
	},
});
