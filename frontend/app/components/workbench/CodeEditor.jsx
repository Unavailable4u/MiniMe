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

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { MessageSquareCode, Sparkles } from "lucide-react";
import { Annotation, Compartment, EditorSelection, EditorState, RangeSetBuilder } from "@codemirror/state";
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
import {
  Chunk,
  acceptChunk,
  getChunks,
  getOriginalDoc,
  goToNextChunk,
  goToPreviousChunk,
  rejectChunk,
  unifiedMergeView,
  updateOriginalDoc,
} from "@codemirror/merge";
import { buildEditorTheme, buildReviewTheme } from "../../lib/workbench/cmTheme";
import {
  detectIndentUnit,
  indentGuideOffsets,
  minimalChange,
  normalizeLineBreaks,
} from "../../lib/workbench/editorUtils";
import { chunkLineStats } from "../../lib/workbench/reviewMode";

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

// The exact text + line range of the given selection, or — when
// nothing is selected — of the line the caret sits on (a bare Mod-L
// with an empty selection acts on "the current line", the same
// forgiving default most editors' own "add/copy line" shortcuts use).
// Shared by the floating toolbar's button and the Mod-L keybinding
// below so both produce the identical shape codeContext.js's ADD_REF
// expects.
function selectionRangeInfo(state) {
  const sel = state.selection.main;
  const doc = state.doc;
  const from = sel.empty ? doc.lineAt(sel.head).from : sel.from;
  const to = sel.empty ? doc.lineAt(sel.head).to : sel.to;
  return {
    from,
    to,
    fromLine: doc.lineAt(from).number,
    toLine: doc.lineAt(to).number,
    snippet: state.sliceDoc(from, to),
  };
}

// Extensions every editor instance shares regardless of file — syntax
// highlighting/language support and the indent unit are layered in
// separately (see the compartments in useCodeMirror below) since they
// depend on the file and, for language support, have to lazy-load
// asynchronously while this bundle doesn't.
//
// W4.1 adds two of the plan's "selection actions" straight into these
// shared extensions, rather than as a separate compartment: neither
// needs to change per-file, so there's nothing to reconfigure later.
//   - `lineNumbers({ domEventHandlers })`: clicking the gutter selects
//     that whole line; Shift-clicking another line extends the
//     selection to cover every line between the two ("gutter
//     shift-click line ranges" — plan §5 W4.1). `gutterAnchorRef` is
//     the line a range started from, so a second Shift-click keeps
//     extending/shrinking from the SAME anchor instead of the
//     previous click.
//   - a `Mod-l` keymap entry ("Add to chat ⌘L" on the plan's own
//     floating toolbar): reports the current selection (or, empty,
//     the current line) to `onAddToChatRef` — the same callback the
//     toolbar's own button calls, kept as a ref (not a closed-over
//     prop) so a caller re-render doesn't have to rebuild this whole
//     extension array (see onSaveRef's own comment for why `Mod-s`
//     already does this).
function baseExtensions(onSaveRef, onAddToChatRef, gutterAnchorRef) {
  return [
    lineNumbers({
      domEventHandlers: {
        mousedown: (view, lineBlock, event) => {
          if (event.button !== 0) return false;
          const doc = view.state.doc;
          const clickedLine = doc.lineAt(lineBlock.from).number;
          const anchorLine =
            event.shiftKey && gutterAnchorRef.current != null ? gutterAnchorRef.current : clickedLine;
          gutterAnchorRef.current = anchorLine;
          const fromLine = Math.min(anchorLine, clickedLine);
          const toLine = Math.max(anchorLine, clickedLine);
          view.dispatch({
            selection: EditorSelection.range(doc.line(fromLine).from, doc.line(toLine).to),
          });
          view.focus();
          return true; // handled — don't let CM also start a text-drag selection from the gutter
        },
      },
    }),
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
      {
        key: "Mod-l",
        preventDefault: true,
        run: (view) => {
          onAddToChatRef.current?.(selectionRangeInfo(view.state));
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

// W4.1: where (if anywhere) the floating "Add to chat ⌘L / Edit with AI
// ⌘K" toolbar should sit — null hides it. Coordinates are relative to
// the EditorView's own DOM node (view.dom), which is what CodeEditor's
// outer component positions its absolutely-positioned toolbar against
// (see that component's `relative` wrapper). Anchored on whichever end
// of the selection the caret ISN'T resting at isn't worth the added
// bookkeeping here — the toolbar reads better hugging the head, same
// as most editors' own selection popovers.
function computeToolbarPos(view) {
  const sel = view.state.selection.main;
  if (sel.empty) return null;
  const coords = view.coordsAtPos(sel.head, sel.head <= sel.anchor ? -1 : 1) || view.coordsAtPos(sel.head);
  if (!coords) return null;
  const box = view.dom.getBoundingClientRect();
  const doc = view.state.doc;
  return {
    top: coords.top - box.top,
    left: coords.left - box.left,
    from: sel.from,
    to: sel.to,
    fromLine: doc.lineAt(sel.from).number,
    toLine: doc.lineAt(sel.to).number,
  };
}

// W4.1: "Range chips track edits via CM6 ChangeSet.mapPos so they
// don't drift while you type" (plan §5 W4.1) — this builds exactly the
// plain `(from, to) => {from, to, fromLine, toLine} | null` function
// codeContext.js's REMAP_REFS action expects, from one ViewUpdate's
// own ChangeSet. `-1`/`1` bias (stay-before / stay-after) on the two
// ends is the usual "typing at an edge grows the range outward" rule —
// text inserted exactly at `from` doesn't get swallowed, and text
// inserted exactly at `to` doesn't get left out. A range that maps to
// zero width (its text was deleted outright, edges collapsed together)
// returns null so the reducer drops the chip instead of leaving it
// pointing at nothing.
function buildMapRange(update) {
  const changes = update.changes;
  const doc = update.state.doc;
  return (from, to) => {
    const mappedFrom = changes.mapPos(from, -1);
    const mappedTo = changes.mapPos(to, 1);
    if (mappedTo <= mappedFrom) return null;
    return {
      from: mappedFrom,
      to: mappedTo,
      fromLine: doc.lineAt(mappedFrom).number,
      toLine: doc.lineAt(mappedTo).number,
    };
  };
}

// W5.3: the "Keep"/"Undo" buttons @codemirror/merge's unifiedMergeView
// paints inline on every changed chunk, in place of the package's own
// default green/red "accept"/"reject" pair — same wording as
// ReviewPanel.jsx's toolbar so a hunk's own buttons and the file-wide
// "Keep all"/"Undo all" ones read as the same action at two scales.
// `action` is @codemirror/merge's own click handler (calls
// acceptChunk()/rejectChunk() on this chunk, under the hood identical
// to what keepAll()/undoAll() below do for every chunk); this only
// supplies the element and stops a click from also moving focus/
// selection into the editor first, which mousedown would otherwise do.
function renderMergeControl(type, action) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `cm-mm-merge-btn cm-mm-merge-btn-${type === "accept" ? "accept" : "reject"}`;
  button.textContent = type === "accept" ? "Keep" : "Undo";
  button.setAttribute("aria-label", type === "accept" ? "Keep this change" : "Undo this change");
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", action);
  return button;
}

// W5.3: the `{current, remaining, added, removed}` shape ReviewPanel.jsx
// passes straight to editorStore.js's REVIEW_FILE_UPDATE — read fresh
// from CM6's own merge state rather than kept as separate React state,
// so it can never drift from what's actually on screen. `getChunks`
// only returns null before the merge view's ChunkField has run at all
// (see that function's own doc comment); `Chunk.build` recomputes the
// same thing directly for that one edge case, from `getOriginalDoc` —
// the ORIGINAL text, which itself moves as chunks are accepted (see
// acceptChunk's own doc comment: "no longer highlighted", not "gone
// from the document") — against the live document.
function reviewSnapshot(state) {
  const info = getChunks(state);
  const original = getOriginalDoc(state);
  const chunks = info ? info.chunks : Chunk.build(original, state.doc);
  const { added, removed } = chunkLineStats(chunks, original, state.doc);
  return { current: state.doc.toString(), remaining: chunks.length, added, removed };
}

// Exported (W2.5) so ConflictCompareView's two read-only MergeView sides
// highlight with the same lazy per-file loader instead of a second copy.
export async function loadLanguageExtension(filePath) {
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
function useCodeMirror({
  containerRef,
  value,
  onChange,
  filePath,
  readOnly,
  onSave,
  onCursorChange,
  autoFocus,
  onAddToChat,
  onSelectionToolbar,
  onRangeChange,
  review,
  onReviewChange,
}) {
  const viewRef = useRef(null);
  const onChangeRef = useRef(onChange);
  const onSaveRef = useRef(onSave);
  const onCursorChangeRef = useRef(onCursorChange);
  // W4.1: same "ref, not a closure the extension array has to rebuild"
  // shape as onSaveRef above — see baseExtensions()'s own header on why.
  const onAddToChatRef = useRef(onAddToChat);
  const onSelectionToolbarRef = useRef(onSelectionToolbar);
  const onRangeChangeRef = useRef(onRangeChange);
  // W5.3: same shape again — ReviewPanel.jsx's ReviewEditor rebuilds its
  // `onReviewChange` closure on every render (it closes over `path`), so
  // this can't be read directly in the mount effect the way `review`
  // itself (below, never a fresh object after the initial render) can.
  const onReviewChangeRef = useRef(onReviewChange);
  const gutterAnchorRef = useRef(null); // W4.1: last gutter-clicked line, for Shift-click ranges
  const languageCompartmentRef = useRef(null);
  const readOnlyCompartmentRef = useRef(null);
  const indentCompartmentRef = useRef(null);
  const loadTokenRef = useRef(0);

  onChangeRef.current = onChange;
  onSaveRef.current = onSave;
  onCursorChangeRef.current = onCursorChange;
  onAddToChatRef.current = onAddToChat;
  onSelectionToolbarRef.current = onSelectionToolbar;
  onRangeChangeRef.current = onRangeChange;
  onReviewChangeRef.current = onReviewChange;

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

    // W5.3: `review` is set once by the caller (ReviewPanel.jsx's own
    // ReviewEditor memoizes it on `original`, which never changes for a
    // review's lifetime — the same "read directly, not via a ref"
    // treatment already given `value`/`filePath`/`readOnly` above,
    // since a review editor is never handed a NEW review to switch to;
    // closing one unmounts it (see ReviewPanel.jsx's own header).
    const isReview = !!review;

    const state = EditorState.create({
      doc: value ?? "",
      extensions: [
        baseExtensions(onSaveRef, onAddToChatRef, gutterAnchorRef),
        buildEditorTheme(),
        isReview ? buildReviewTheme() : [],
        indentCompartment.of(indentUnit.of(detectIndentUnit(value ?? ""))),
        languageCompartment.of([]),
        readOnlyCompartment.of([
          EditorState.readOnly.of(!!readOnly),
          EditorView.editable.of(!readOnly),
        ]),
        // W4.1: pure scrolling moves where the selection sits on screen
        // without firing selectionSet/docChanged, so the toolbar's own
        // position (below) would otherwise go stale mid-scroll.
        EditorView.domEventHandlers({
          scroll: (_event, view) => {
            onSelectionToolbarRef.current?.(computeToolbarPos(view));
          },
        }),
        EditorView.updateListener.of((update) => {
          const isExternal = update.docChanged && update.transactions.some((tr) => tr.annotation(External));
          if (update.docChanged) {
            if (!isExternal) {
              onChangeRef.current?.(update.state.doc.toString());
            }
            // W4.1: an external update (Reload/Restore) shifts
            // positions just as much as typing does, so range chips
            // remap for both — only a real keystroke is excluded from
            // onChange above, not from this.
            onRangeChangeRef.current?.(buildMapRange(update));
          }
          // Caret position for a status bar's "Ln 12, Col 4". Reported
          // for external updates too — a Reload can move the caret —
          // but only when something that could have moved it happened,
          // not on every viewport/focus update.
          if (update.selectionSet || update.docChanged) {
            onCursorChangeRef.current?.(cursorPosition(update.state));
            // W4.1: floating "Add to chat / Edit with AI" toolbar —
            // shows above a non-empty selection, hides otherwise.
            onSelectionToolbarRef.current?.(computeToolbarPos(update.view));
          }
          // W5.3: report the file's current text and remaining-hunk
          // count after anything that could have changed either —
          // typing, Keep/Undo on one hunk, or keepAll()/undoAll() below
          // (each of those is its own dispatch, so each is its own
          // update here). `docChanged` alone would miss Keep: acceptChunk
          // moves the ORIGINAL doc via the updateOriginalDoc effect and
          // touches no text, since the accepted text was already on
          // screen (see reviewSnapshot()'s own comment).
          if (
            isReview &&
            (update.docChanged || update.transactions.some((tr) => tr.effects.some((e) => e.is(updateOriginalDoc))))
          ) {
            onReviewChangeRef.current?.(reviewSnapshot(update.state));
          }
        }),
        // W5.3: the merge view itself, comparing this editor's document
        // (the proposed text `value` seeded above) against `review.original`
        // — see ConflictCompareView.jsx's header for why review mode
        // uses unifiedMergeView (one document, per-hunk controls
        // threaded through it) rather than that component's two-pane
        // MergeView (two read-only finished snapshots side by side).
        isReview
          ? unifiedMergeView({
              original: normalizeLineBreaks(review.original ?? ""),
              mergeControls: renderMergeControl,
              collapseUnchanged: { margin: 3 },
            })
          : [],
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;
    if (autoFocus) view.focus();
    // Report where the caret starts (line 1, col 1) so a status bar has
    // something to show before the first click or keystroke.
    onCursorChangeRef.current?.(cursorPosition(view.state));
    // W5.3: and the initial hunk count, so reviewMode.js's
    // reviewProgress() can tell "still loading" (editorStore.js's
    // REVIEW_OPEN seeds `remaining: null`) from "loaded, nothing left to
    // decide" (a proposal that turned out to be a no-op diff) without
    // waiting for the person to touch anything.
    if (isReview) onReviewChangeRef.current?.(reviewSnapshot(view.state));

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
 * @param {(sel: {from: number, to: number, fromLine: number, toLine: number, snippet: string}) => void} [props.onAddToChat] -
 *   W4.1: fired by the floating toolbar's "Add to chat" button and by
 *   Mod-L (selection, or the current line if nothing's selected).
 *   Omitted entirely (the default, before EditorWorkbench.jsx wires a
 *   codeContext store in) hides the toolbar altogether — there's
 *   nothing for it to do.
 * @param {(path: string, mapRange: (from: number, to: number) => ({from:number,to:number,fromLine:number,toLine:number}|null)) => void} [props.onRangeChange] -
 *   NOT called with `path` by this component (it doesn't know its own
 *   path) — EditorWorkbench.jsx's EditorPane closes over it; see that
 *   file. Fires on every doc change (typed or external) with a plain
 *   ChangeSet.mapPos-based mapper, for codeContext.js's REMAP_REFS.
 * @param {{original: string}} [props.review] - W5.3: turns this editor
 *   into a Copilot-style review of one file — `value` is the proposed
 *   text (still editable unless `readOnly`), diffed inline against
 *   `review.original` via @codemirror/merge's unifiedMergeView, with
 *   per-hunk Keep/Undo buttons. Set once; see useCodeMirror()'s own
 *   comment on why this doesn't react to later prop changes the way
 *   `value`/`filePath`/`readOnly` do. ReviewPanel.jsx is the only
 *   caller.
 * @param {(snap: {current: string, remaining: number, added: number, removed: number}) => void} [props.onReviewChange] -
 *   W5.3: fires once on mount and after every hunk decision (typing,
 *   a hunk's own Keep/Undo, or keepAll()/undoAll() below) — the shape
 *   ReviewPanel.jsx forwards straight into editorStore.js's
 *   REVIEW_FILE_UPDATE. Ignored when `review` isn't set.
 */
function CodeEditor(
  {
    value,
    onChange,
    filePath,
    readOnly = false,
    onSave,
    onCursorChange,
    autoFocus = false,
    className,
    onAddToChat,
    onRangeChange,
    review,
    onReviewChange,
  },
  ref
) {
  const containerRef = useRef(null);
  // W4.1: null hides the floating toolbar; see computeToolbarPos()'s
  // own header for the coordinate space this is in.
  const [toolbar, setToolbar] = useState(null);
  const viewRef = useCodeMirror({
    containerRef,
    value,
    onChange,
    filePath,
    readOnly,
    onSave,
    onCursorChange,
    autoFocus,
    onAddToChat,
    onSelectionToolbar: onAddToChat ? setToolbar : undefined,
    onRangeChange,
    review,
    onReviewChange,
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
      // W5.3 — review mode only (ReviewPanel.jsx's Keep all/Undo all and
      // Prev/Next). Harmless no-ops on a plain editor: getChunks() finds
      // no merge extension installed and returns null, and the
      // goToNext/PreviousChunk commands simply have nothing to move to.
      keepAll: () => {
        const view = viewRef.current;
        if (!view) return;
        // Each accept/reject is its own dispatch and can shift or
        // remove OTHER chunks (rejecting one changes document length;
        // accepting one moves the diff baseline — see reviewSnapshot()'s
        // comment), so this re-reads the chunk list after every one
        // rather than iterating a snapshot taken before any of them.
        for (;;) {
          const info = getChunks(view.state);
          if (!info || info.chunks.length === 0) break;
          if (!acceptChunk(view, info.chunks[0].fromB)) break;
        }
      },
      undoAll: () => {
        const view = viewRef.current;
        if (!view) return;
        for (;;) {
          const info = getChunks(view.state);
          if (!info || info.chunks.length === 0) break;
          if (!rejectChunk(view, info.chunks[0].fromB)) break;
        }
      },
      nextChange: () => {
        const view = viewRef.current;
        if (view) goToNextChunk(view);
      },
      prevChange: () => {
        const view = viewRef.current;
        if (view) goToPreviousChunk(view);
      },
    }),
    [viewRef]
  );

  return (
    <div className="relative h-full">
      <div ref={containerRef} className={className} style={{ height: "100%", overflow: "auto" }} />
      {/* W4.1: "floating toolbar on selection" (plan §5 W4.1). Only
          "Add to chat" is wired — "Edit with AI" needs the proposal
          endpoint W5.1 adds, so it stays a disabled stub with the same
          "Coming soon" convention Explorer.jsx's own add-to-chat item
          used before this step (see that file's pre-W4.1 comment). */}
      {toolbar && onAddToChat && (
        <div
          role="toolbar"
          aria-label="Selection actions"
          style={{ top: Math.max(0, toolbar.top - 34), left: Math.max(0, toolbar.left) }}
          className="absolute z-20 flex items-center gap-0.5 rounded-md border border-[var(--neutral-700)] bg-[var(--neutral-900)] p-0.5 shadow-lg"
        >
          <button
            type="button"
            onClick={() => {
              onAddToChat({
                from: toolbar.from,
                to: toolbar.to,
                fromLine: toolbar.fromLine,
                toLine: toolbar.toLine,
                snippet: viewRef.current?.state.sliceDoc(toolbar.from, toolbar.to) ?? "",
              });
            }}
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-[var(--neutral-200)] hover:bg-[var(--neutral-800)]"
          >
            <MessageSquareCode size={12} className="shrink-0" />
            Add to chat
            <span className="text-[var(--neutral-500)]">⌘L</span>
          </button>
          <button
            type="button"
            disabled
            title="Coming in W4.2"
            className="flex items-center gap-1 rounded px-2 py-1 text-[11px] text-[var(--neutral-600)] cursor-default"
          >
            <Sparkles size={12} className="shrink-0" />
            Edit with AI
            <span className="text-[var(--neutral-700)]">⌘K</span>
          </button>
        </div>
      )}
    </div>
  );
}

export default forwardRef(CodeEditor);
