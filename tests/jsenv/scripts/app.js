// Minimal stand-in for ComfyUI's app module, enough to load a web extension.
export const app = {
	extensions: [],
	canvas: { current_node: null, getWidgetAtCursor: () => null },
	registerExtension(ext) { this.extensions.push(ext); },
};
