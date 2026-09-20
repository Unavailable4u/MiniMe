"use client";
// frontend/app/components/workbench/CodeEditor.jsx — W2.1 (Build
// Workbench plan): the raw CodeMirror 6 editor the merged Build-tab
// workbench is built on top of (see MiniMe_Build_Workbench_Plan.md's
// D1 — CM6 over Monaco, chosen specifically because Monaco doesn't
// support mobile browsers and CM6 ships @codemirror/merge's
// unifiedMergeView, the per-hunk Keep/Undo review-mode UI a later
// patch (W5.x) builds directly on top of this same component).
//
// This is a THIN hand-rolled wrapper around CM6, not a wrapper library
// (react-codemirror2, @uiw/react-codemirror, etc.) — the plan calls for
// imperative control over selection/decorations/merge later (W4.1's
// selection chips, W5.x's review mode), which a wrapper library's
// props-only API tends to fight rather than help. `useCodeMirror`
// below is that raw-CM6-through-a-hook shape, kept local to this file
// since nothing else needs it yet.
//
// CLIENT-ONLY: CodeMirror measures real DOM nodes (line heights, gutter
// widths) and has no server-side rendering story. This file itself is
// "use client", but that alone isn't enough — whatever mounts
// <CodeEditor> for real (W2.3's EditorWorkbench) must ALSO wrap it in
// `dynamic(() => import(...), { ssr: false })` from "next/dynamic",
// same as this app already does anywhere else it renders something
// DOM-measurement-dependent. Skipping that dynamic() wrapper makes Next
// try to prerender this on the server, which throws immediately (no
// `document`).
//
// Controlled component contract:
//   - `value`/`onChange` behave like a controlled <textarea>: typing
//     fires onChange(newText); setting a new `value` prop from outside
//     (the file-history Restore button, a live "Reload" from W0.1's
//     "Changed on server" banner) updates the doc WITHOUT throwing away
//     undo history or moving the cursor — see the `External` annotation
//     and minimalChange() below, the real CM6-specific plumbing a plain
//     controlled <textarea> doesn't need. Recreating the EditorState
//     from scratch on every external `value` change (the naive
//     approach) would reset undo history every time, which defeats the
//     entire point of building this on an editor that HAS undo history.
//   - An external update is itself ONE undo step, on purpose: Cmd-Z
//     after "Reload" or "Restore" brings back the text you had before,
//     which is the forgiving behaviour for both. The flip side is that
//     one EditorView is one document's undo history. To show a
//     DIFFERENT file, mount a separate <CodeEditor key={path}> instead
//     of feeding a new file's text through `value` — otherwise Cmd-Z
//     would undo "back" into the previous file. (Changing only
//     `filePath` on the same instance — a rename, a.js -> a.ts — is
//     fine and just re-selects the syntax mode.)
//   - `filePath` selects the syntax mode via @codemirror/language-data
//     (lazy-loaded per file, per the plan — most files never pay for
//     most languages' parser bundles) rather than a `language` enum
//     prop the caller would have to keep in sync with the file itself.
//   - Line endings: CM6 always splits on \r\n, \r and \n alike and
//     always reports "\n" back, so `onChange` text is LF-normalised
//     even for a CRLF file. `value` may contain CRLF (it's normalised
//     before comparing, so it never causes a spurious update), but
//     preserving CRLF on save is the caller's job — it matters for
//     W3.1's local files, not for the cloud files this is first used on.
//   - Indentation follows the file: the indent unit (2 spaces, 4
//     spaces, tab) is detected from the file's own text on mount and on
//     each external update (see detectIndentUnit() in editorUtils.js).
//     A fixed unit would make auto-indent insert 2 spaces into a
//     4-space Python file.

import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { Annotation, Compartment, EditorState, RangeSetBuilder } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  crosshairCursor,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
  rectangularSelection,
} from "@codemirror/view";
import {
  LanguageDescription,
  bracketMatching,
  foldGutter,
  foldKeymap,
  indentOnInput,
  indentUnit,
} from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap } from "@codemirror/autocomplete";
import { highlightSelectionMatches, search, searchKeymap } from "@codemirror/search";
import { buildEditorTheme } from "../../lib/workbench/cmTheme";
import {
  detectIndentUnit,
  indentGuideOffsets,
  minimalChange,
  normalizeLineBreaks,
} from "../../lib/workbench/editorUtils";

// Tags a transaction as coming from THIS component's own `value`-prop
// effect (an external update), not from the person typing — read back
// in the updateListener below so an external update doesn't bounce
// straight back out through onChange as if the user had typed it. That
// would both be factually wrong (the user didn't type it) and, worse,
// could re-enter whatever caller code just set `value` in the first
// place.
const External = Annotation.define();

// One vertical guide per completed indent level, drawn by giving the
// FIRST character of each indent-unit-sized chunk of a line's leading
// whitespace a left rule (see cmTheme.js's `.cm-mm-indent-guide` rule
// for the actual color/width). No core CM6 extension does this —
// @codemirror/language ships the indent LOGIC (getIndentUnit,
// indentOnInput), not a visual guide — and the community package that
// draws one isn't in this patch's install list, so this is our own
// small plugin rather than a new dependency.
//
// Which characters get a mark (spaces for a space-indented file, tabs
// for a tab-indented one, nothing on blank lines) is decided by
// indentGuideOffsets() in editorUtils.js so it can be unit-tested
// without a browser.
const INDENT_GUIDE_MARK = Decoration.mark({ class: "cm-mm-indent-guide" });

function computeIndentGuideDecorations(view) {
  const builder = new RangeSetBuilder();
  const unit = view.state.facet(indentUnit);
  const doc = view.state.doc;
  let lastLineNumber = 0;

  for (const { from, to } of view.visibleRanges) {
    let pos = from;
    while (pos <= to) {
      const line = doc.lineAt(pos);
      pos = line.to + 1;
      // Two visible ranges can touch the same line (a fold that starts
      // or ends mid-line). RangeSetBuilder throws if a range is added
      // out of order, and re-adding a line's marks would do exactly
      // that — so each line is only ever marked once.
      if (line.number <= lastLineNumber) continue;
      lastLineNumber = line.number;

      for (const offset of indentGuideOffsets(line.text, unit)) {
        const at = line.from + offset;
        builder.add(at, at + 1, INDENT_GUIDE_MARK);
      }
    }
  }
  return builder.finish();
}

function indentGuides() {
  return ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.decorations = computeIndentGuideDecorations(view);
      }

      update(update) {
        // viewportChanged also covers the visible ranges changing (a
        // fold or unfold), not just scrolling.
        if (
          update.docChanged ||
          update.viewportChanged ||
          update.startState.facet(indentUnit) !== update.state.facet(indentUnit)
        ) {
          this.decorations = computeIndentGuideDecorations(update.view);
        }
      }
    },
    { decorations: (plugin) => plugin.decorations }
  );
}

// Extensions every editor instance shares regardless of file — syntax
// highlighting/language support and the indent unit are layered in
// separately (see the compartments in useCodeMirror below) since they
// depend on the file and, for language support, have to lazy-load
// asynchronously while this bundle doesn't.
function baseExtensions(onSaveRef) {
  return [
    lineNumbers(),
    highlightActiveLineGutter(),
    highlightActiveLine(),
    highlightSpecialChars(),
    history(),
    foldGutter(),
    drawSelection(),
    dropCursor(),
    crosshairCursor(),
    rectangularSelection(),
    // Multi-cursor: Alt-click / Alt-drag work via allowMultipleSelections
    // + drawSelection above, and Cmd/Ctrl-D ("select next occurrence")
    // comes with searchKeymap below — no keymap entry of our own needed.
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    autocompletion(),
    search({ top: true }),
    highlightSelectionMatches(),
    indentGuides(),
    keymap.of([
      {
        key: "Mod-s",
        preventDefault: true,
        run: () => {
          onSaveRef.current?.();
          return true;
        },
      },
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...searchKeymap,
      ...historyKeymap,
      ...foldKeymap,
      ...completionKeymap,
      indentWithTab,
    ]),
  ];
}

// 1-based line and column of the main selection's head — what every
// editor's status bar shows. Column counts UTF-16 units from the start
// of the line (a tab is one column), the same convention CM6 itself
// uses for positions.
function cursorPosition(state) {
  const head = state.selection.main.head;
  const line = state.doc.lineAt(head);
  return { line: line.number, col: head - line.from + 1 };
}

async function loadLanguageExtension(filePath) {
  if (!filePath) return [];
  // matchFilename() tests some languages' patterns against the WHOLE
  // string it's given, and those patterns are anchored to it —
  // /^Dockerfile$/, /^CMakeLists\.txt$/, /^Jenkinsfile$/, /^Gemfile$/.
  // Handed "docker/Dockerfile" or "services/api/CMakeLists.txt" they
  // silently match nothing and the file opens as plain text, so reduce
  // the path to its file name first. (Extension matches don't care,
  // which is why this only bites those special-named files.)
  const fileName = filePath.split(/[\\/]/).pop();
  const desc = LanguageDescription.matchFilename(languages, fileName);
  if (!desc) return [];
  try {
    const support = await desc.load();
    return [support];
  } catch (err) {
    // A parser bundle failing to load (a network hiccup on a lazy
    // chunk, an unusual extension slipping past matchFilename) should
    // degrade to plain-text editing, not break the editor — same
    // "under-highlighted, never wrong" posture lib/highlightCode.js's
    // own regex tokenizer already documents for itself.
    console.error(`[CodeEditor] failed to load language support for ${filePath}`, err);
    return [];
  }
}

/**
 * Owns the actual EditorView lifecycle: creates it once against
 * `containerRef`, tears it down on unmount, and wires the prop-driven
 * effects (external value changes, read-only toggling, per-file
 * language loading) described in this file's own header comment. Kept
 * local to CodeEditor.jsx rather than exported — nothing else needs a
 * raw CM6 instance yet.
 */
function useCodeMirror({ containerRef, value, onChange, filePath, readOnly, onSave, onCursorChange, autoFocus }) {
  const viewRef = useRef(null);
  const onChangeRef = useRef(onChange);
  const onSaveRef = useRef(onSave);
  const onCursorChangeRef = useRef(onCursorChange);
  const languageCompartmentRef = useRef(null);
  const readOnlyCompartmentRef = useRef(null);
  const indentCompartmentRef = useRef(null);
  const loadTokenRef = useRef(0);

  onChangeRef.current = onChange;
  onSaveRef.current = onSave;
  onCursorChangeRef.current = onCursorChange;

  // Mount once. `value`/`filePath`/`readOnly` at THIS instant seed the
  // initial state; every later change to any of them is handled by the
  // effects below via compartment reconfiguration or an External
  // transaction, never by tearing this EditorView down and rebuilding
  // it — rebuilding would drop undo history and the current selection
  // on every prop change, which is exactly what a controlled component
  // built on an editor that HAS undo history must not do.
  useEffect(() => {
    const languageCompartment = new Compartment();
    const readOnlyCompartment = new Compartment();
    const indentCompartment = new Compartment();
    languageCompartmentRef.current = languageCompartment;
    readOnlyCompartmentRef.current = readOnlyCompartment;
    indentCompartmentRef.current = indentCompartment;

    const state = EditorState.create({
      doc: value ?? "",
      extensions: [
        baseExtensions(onSaveRef),
        buildEditorTheme(),
        indentCompartment.of(indentUnit.of(detectIndentUnit(value ?? ""))),
        languageCompartment.of([]),
        readOnlyCompartment.of([
          EditorState.readOnly.of(!!readOnly),
          EditorView.editable.of(!readOnly),
        ]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            const isExternal = update.transactions.some((tr) => tr.annotation(External));
            if (!isExternal) {
              onChangeRef.current?.(update.state.doc.toString());
            }
          }
          // Caret position for a status bar's "Ln 12, Col 4". Reported
          // for external updates too — a Reload can move the caret —
          // but only when something that could have moved it happened,
          // not on every viewport/focus update.
          if (update.selectionSet || update.docChanged) {
            onCursorChangeRef.current?.(cursorPosition(update.state));
          }
        }),
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;
    if (autoFocus) view.focus();
    // Report where the caret starts (line 1, col 1) so a status bar has
    // something to show before the first click or keystroke.
    onCursorChangeRef.current?.(cursorPosition(view.state));

    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Intentionally mount-once — see comment above. `containerRef` and
    // `autoFocus` are only ever read at mount time by design.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // External value changes — dispatched as an ordinary transaction
  // against the SAME state/history, tagged `External` so the
  // updateListener above doesn't mistake it for a keystroke.
  //
  // Two details that a plain "replace the whole doc" would get wrong:
  //   - Only the span that actually differs is replaced (see
  //     minimalChange()), so the cursor/selection and folds outside it
  //     stay put. A whole-document replace maps every position inside
  //     it to the start, which would throw the person back to line 1
  //     whenever the server changed one line.
  //   - `value` is line-break-normalised before comparing/diffing,
  //     because CM6's own text never contains "\r" (see
  //     normalizeLineBreaks()). Compared raw, a CRLF file would look
  //     "changed" at every line end.
  // The indent unit is re-detected here too, since new text may follow
  // a different convention than what was loaded before.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    const next = normalizeLineBreaks(value ?? "");
    if (next === current) return;

    const unit = detectIndentUnit(next);
    const unitChanged = unit !== view.state.facet(indentUnit);
    view.dispatch({
      changes: minimalChange(current, next),
      annotations: [External.of(true)],
      effects: unitChanged ? [indentCompartmentRef.current.reconfigure(indentUnit.of(unit))] : [],
    });
  }, [value]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view || !readOnlyCompartmentRef.current) return;
    view.dispatch({
      effects: readOnlyCompartmentRef.current.reconfigure([
        EditorState.readOnly.of(!!readOnly),
        EditorView.editable.of(!readOnly),
      ]),
    });
  }, [readOnly]);

  // Lazy per-file language loading. `loadTokenRef` guards against a
  // fast tab switch resolving out of order — e.g. open app.py (slow
  // Python parser chunk), then immediately switch to style.css (fast
  // CSS chunk): without this guard, app.py's load could resolve AFTER
  // style.css's and stomp the language back to Python on the wrong
  // file.
  useEffect(() => {
    const view = viewRef.current;
    if (!view || !languageCompartmentRef.current) return;
    const myToken = ++loadTokenRef.current;

    loadLanguageExtension(filePath).then((extension) => {
      if (myToken !== loadTokenRef.current || !viewRef.current) return;
      viewRef.current.dispatch({
        effects: languageCompartmentRef.current.reconfigure(extension),
      });
    });
  }, [filePath]);

  return viewRef;
}

/**
 * Raw CodeMirror 6 editor. Controlled `value`/`onChange`, like a
 * `<textarea>`. See this file's own header comment for the fuller
 * contract, and useCodeMirror() above for the lifecycle this wraps.
 *
 * Imperative handle exposes the underlying EditorView plus a couple of
 * selection helpers — not used by anything yet, but this is exactly
 * the "imperative control over selection... later" the plan's D1
 * decision calls out as CM6's advantage over a wrapper library; W4.1's
 * selection chips are the first real caller.
 *
 * @param {object} props
 * @param {string} props.value
 * @param {(next: string) => void} props.onChange
 * @param {string} [props.filePath] - selects syntax highlighting via
 *   @codemirror/language-data; omit for a plain-text editor.
 * @param {boolean} [props.readOnly=false]
 * @param {() => void} [props.onSave] - Cmd/Ctrl-S.
 * @param {(pos: {line: number, col: number}) => void} [props.onCursorChange] -
 *   the caret's 1-based position; fires once on mount and whenever the
 *   selection or document changes (W2.3a's status bar).
 * @param {boolean} [props.autoFocus=false]
 * @param {string} [props.className]
 */
function CodeEditor(
  { value, onChange, filePath, readOnly = false, onSave, onCursorChange, autoFocus = false, className },
  ref
) {
  const containerRef = useRef(null);
  const viewRef = useCodeMirror({
    containerRef,
    value,
    onChange,
    filePath,
    readOnly,
    onSave,
    onCursorChange,
    autoFocus,
  });

  useImperativeHandle(
    ref,
    () => ({
      getView: () => viewRef.current,
      focus: () => viewRef.current?.focus(),
      getSelectedText: () => {
        const view = viewRef.current;
        if (!view) return "";
        const { from, to } = view.state.selection.main;
        return view.state.sliceDoc(from, to);
      },
      getSelectionRange: () => {
        const view = viewRef.current;
        if (!view) return null;
        const { from, to } = view.state.selection.main;
        const doc = view.state.doc;
        return { from, to, fromLine: doc.lineAt(from).number, toLine: doc.lineAt(to).number };
      },
    }),
    [viewRef]
  );

  return <div ref={containerRef} className={className} style={{ height: "100%", overflow: "auto" }} />;
}

export default forwardRef(CodeEditor);
