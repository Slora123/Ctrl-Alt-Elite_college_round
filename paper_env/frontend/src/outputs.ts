// Rich output rendering with JupyterLab's rendermime (as a library only). Output from the kernel is UNTRUSTED:
// models are created with trusted=false, so HTML is sanitized and scripts never run.
import { RenderMimeRegistry, standardRendererFactories } from '@jupyterlab/rendermime';
import { Widget } from '@lumino/widgets';
import type { LiveWidgetManager } from './widgets';

const WIDGET_MIME = 'application/vnd.jupyter.widget-view+json';
const registry = new RenderMimeRegistry({ initialFactories: standardRendererFactories });
let widgets: LiveWidgetManager | null = null;
export const setWidgetManager = (m: LiveWidgetManager) => { widgets = m; };

async function renderMime(mime: string, data: Record<string, any>, host: HTMLElement) {
  const model = registry.createModel({ data, trusted: false });
  const renderer = registry.createRenderer(mime);
  await renderer.renderModel(model);
  Widget.attach(renderer, host);
}

export async function renderStream(name: string, text: string, host: HTMLElement) {
  host.replaceChildren();
  await renderMime(name === 'stderr' ? 'application/vnd.jupyter.stderr' : 'application/vnd.jupyter.stdout', { [name === 'stderr' ? 'application/vnd.jupyter.stderr' : 'application/vnd.jupyter.stdout']: text }, host);
}

export async function renderBundle(data: Record<string, any>, host: HTMLElement) {
  host.replaceChildren();
  if (data[WIDGET_MIME] && widgets) { await widgets.renderModel(data[WIDGET_MIME].model_id, host); return; }
  // large items are replaced by {omitted_bytes} placeholders by the server
  const usable = Object.fromEntries(Object.entries(data).filter(([, v]) => !(v && typeof v === 'object' && 'omitted_bytes' in (v as any))));
  const mime = registry.preferredMimeType(usable, 'ensure') ?? registry.preferredMimeType(usable, 'prefer');
  if (!mime) {
    const omitted = Object.keys(data).filter(k => !(k in usable));
    host.textContent = omitted.length ? `[output omitted: too large for the database (${omitted.join(', ')}); see the cell's full-output file]` : JSON.stringify(data);
    host.className = 'note';
    return;
  }
  await renderMime(mime, usable, host);
}
