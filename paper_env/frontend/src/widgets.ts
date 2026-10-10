// ipywidgets support: a widget manager whose comms are tunnelled through our HTTP API (frontend -> kernel) and
// the event WebSocket (kernel -> frontend, as ephemeral `comm_message` events).
import { ManagerBase } from '@jupyter-widgets/base-manager';
import * as base from '@jupyter-widgets/base';
import * as controls from '@jupyter-widgets/controls';
import * as outputs from '@jupyter-widgets/output';
import { Widget } from '@lumino/widgets';
import '@jupyter-widgets/controls/css/widgets.built.css';
import { fromBase64, toBase64 } from './api';

export type CommTransport = (msgType: string, content: any, buffers: string[]) => Promise<void> | void;

class PyComm {
  private msgCb: ((m: any) => any) | null = null;
  private closeCb: ((m: any) => any) | null = null;
  constructor(public comm_id: string, public target_name: string, private transport: CommTransport) {}
  private bufs(b?: ArrayBuffer[] | ArrayBufferView[]) { return (b ?? []).map(x => toBase64(x as any)); }
  open(data: any, _cb?: any, _meta?: any, buffers?: any) {
    void this.transport('comm_open', { comm_id: this.comm_id, target_name: this.target_name, data }, this.bufs(buffers)); return '';
  }
  send(data: any, _cb?: any, _meta?: any, buffers?: any) {
    void this.transport('comm_msg', { comm_id: this.comm_id, data }, this.bufs(buffers)); return '';
  }
  close(data?: any) { void this.transport('comm_close', { comm_id: this.comm_id, data: data ?? {} }, []); return ''; }
  on_msg(cb: (m: any) => any) { this.msgCb = cb; }
  on_close(cb: (m: any) => any) { this.closeCb = cb; }
  deliver(msg: any) { this.msgCb?.(msg); }
  deliverClose(msg: any) { this.closeCb?.(msg); }
}

export interface CommEventPayload { msg_type: string; content: any; buffers?: string[]; metadata?: any }

export class LiveWidgetManager extends ManagerBase {
  private comms = new Map<string, PyComm>();
  constructor(private transport: CommTransport) { super(); }

  /** Only the built-in widget packages are available (custom third-party widget JS is deliberately not loaded). */
  protected loadClass(className: string, moduleName: string, _moduleVersion: string): Promise<any> {
    const mods: Record<string, any> = { '@jupyter-widgets/base': base, '@jupyter-widgets/controls': controls, '@jupyter-widgets/output': outputs };
    const m = mods[moduleName]; const cls = m?.[className];
    return cls ? Promise.resolve(cls) : Promise.reject(new Error(`Widget class ${moduleName}.${className} is not available in this UI`));
  }
  async display_view(view: any, el: HTMLElement): Promise<void> { Widget.attach((view as any).luminoWidget, el); }

  protected async _create_comm(target_name: string, model_id?: string, data?: any, _m?: any, buffers?: any): Promise<any> {
    const comm = new PyComm(model_id ?? crypto.randomUUID().replace(/-/g, ''), target_name, this.transport);
    this.comms.set(comm.comm_id, comm);
    if (data !== undefined) comm.open(data, undefined, undefined, buffers);
    return comm;
  }
  protected async _get_comm_info(): Promise<{}> { return {}; }

  async handle(ev: CommEventPayload): Promise<void> {
    const buffers = (ev.buffers ?? []).map(fromBase64);
    const msg: any = { header: { msg_type: ev.msg_type }, parent_header: {}, metadata: ev.metadata ?? {}, content: ev.content, buffers };
    if (ev.msg_type === 'comm_open') {
      if (ev.content.target_name !== 'jupyter.widget') return;
      const comm = new PyComm(ev.content.comm_id, ev.content.target_name, this.transport);
      this.comms.set(comm.comm_id, comm);
      await this.handle_comm_open(comm as any, msg);
    } else if (ev.msg_type === 'comm_msg') {
      this.comms.get(ev.content.comm_id)?.deliver(msg);
    } else if (ev.msg_type === 'comm_close') {
      this.comms.get(ev.content.comm_id)?.deliverClose(msg);
      this.comms.delete(ev.content.comm_id);
    }
  }

  /** Forget every widget (the kernel they belonged to is gone). */
  async reset() { this.comms.clear(); await this.clear_state(); }

  /** Render a widget view into `host`. The model may still be arriving over the WebSocket, so wait for it. */
  async renderModel(modelId: string, host: HTMLElement): Promise<void> {
    for (let i = 0; i < 100 && !this.has_model(modelId); i++) await new Promise(r => setTimeout(r, 100));
    if (!this.has_model(modelId)) { host.textContent = `[widget ${modelId.slice(0, 8)} is not active in this kernel; re-run the cell]`; host.className = 'note'; return; }
    const model = await this.get_model(modelId);
    const view = await this.create_view(model);
    Widget.attach((view as any).luminoWidget ?? (view as any).pWidget, host);
  }
}
