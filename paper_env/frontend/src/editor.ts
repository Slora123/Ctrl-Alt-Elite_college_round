import { EditorView, keymap, lineNumbers, highlightActiveLine, drawSelection } from '@codemirror/view';
import { EditorState, Prec } from '@codemirror/state';
import { python } from '@codemirror/lang-python';
import { markdown } from '@codemirror/lang-markdown';
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap, type CompletionContext } from '@codemirror/autocomplete';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { bracketMatching, indentOnInput, syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language';
import { oneDark } from '@codemirror/theme-one-dark';

export interface EditorHooks {
  onChange(doc: string): void;
  onRun(): void;
  onSave(): void;
  onInspect(code: string, pos: number): void;
  complete(code: string, pos: number): Promise<{ matches: string[]; cursor_start: number } | null>;
}

export interface Editor {
  view: EditorView;
  get(): string;
  set(text: string): void;
  replaceLine(index: number, text: string): void;
  focus(): void;
}

const dark = window.matchMedia?.('(prefers-color-scheme: dark)').matches;

export function createEditor(parent: HTMLElement, initial: string, hooks: EditorHooks, lang: 'python' | 'markdown' = 'python'): Editor {
  // Completions come from the live kernel (IPython's completer); without a kernel they quietly yield nothing.
  const kernelSource = async (ctx: CompletionContext) => {
    const before = ctx.matchBefore(/[\w.]*/);
    if (!ctx.explicit && (!before || before.from === before.to)) return null;
    const r = await hooks.complete(ctx.state.doc.toString(), ctx.pos).catch(() => null);
    if (!r || !r.matches.length) return null;
    return { from: r.cursor_start, options: r.matches.map(label => ({ label })), validFor: /^[\w.]*$/ };
  };
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: initial,
      extensions: [
        Prec.highest(keymap.of([
          { key: 'Shift-Enter', run: () => { hooks.onRun(); return true; } },
          { key: 'Mod-s', run: () => { hooks.onSave(); return true; } },
          { key: 'Shift-Tab', run: v => { hooks.onInspect(v.state.doc.toString(), v.state.selection.main.head); return true; } },
        ])),
        lineNumbers(), history(), drawSelection(), highlightActiveLine(), indentOnInput(), bracketMatching(), closeBrackets(),
        lang === 'python' ? python() : markdown(), syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
        ...(lang === 'python' ? [EditorState.languageData.of(() => [{ autocomplete: kernelSource }]), autocompletion({ activateOnTyping: true })] : []),
        keymap.of([...closeBracketsKeymap, ...completionKeymap, ...historyKeymap, indentWithTab, ...defaultKeymap]),
        EditorView.updateListener.of(u => { if (u.docChanged && !(u.transactions.some(t => t.annotation(external)))) hooks.onChange(u.state.doc.toString()); }),
        EditorView.lineWrapping,
        ...(dark ? [oneDark] : []),
      ],
    }),
  });
  return {
    view,
    get: () => view.state.doc.toString(),
    set(text) { if (text !== view.state.doc.toString()) view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text }, annotations: external.of(true) }); },
    replaceLine(i, text) { const l = view.state.doc.line(i + 1); view.dispatch({ changes: { from: l.from, to: l.to, insert: text } }); },
    focus: () => view.focus(),
  };
}

import { Annotation } from '@codemirror/state';
const external = Annotation.define<boolean>();
