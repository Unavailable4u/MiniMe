// frontend/app/lib/workbench/pulse.js — W6.5 (Build Workbench plan).
// "...scroll to and select the element's exact range, brief pulse
// decoration" — the pulse half. A range gets a highlighted background
// for a moment after a click in the preview lands the editor on it, so
// the eye finds the selection even in a long file (a selection alone is
// easy to miss when the editor didn't previously have focus).
//
// Same split gotoPosition.js's header describes: this needs
// @codemirror/state + view, so it isn't part of the import-free
// family — but the StateField itself only needs EditorState to
// exercise (no DOM), which is what __tests__/pulse.test.mjs relies on.
//
// `pulseExtension` must be part of a CodeEditor's extensions for the
// decoration to render; dispatching pulseRange() at a view WITHOUT it
// is harmless (an effect nothing listens for is simply ignored), so a
// caller never has to check whether a given editor installed it.
import { StateEffect, StateField } from "@codemirror/state";
import { Decoration, EditorView } from "@codemirror/view";

// value: {from, to} to show a pulse, null to clear it
export const setPulse = StateEffect.define();

const PULSE_MARK = Decoration.mark({ class: "cm-mm-pulse" });

export const pulseField = StateField.define({
  create: () => Decoration.none,
  update(decorations, tr) {
    // The pulse follows the text if someone types during its ~1s life.
    decorations = decorations.map(tr.changes);
    for (const effect of tr.effects) {
      if (!effect.is(setPulse)) continue;
      if (!effect.value) {
        decorations = Decoration.none;
        continue;
      }
      // Clamped to the CURRENT doc, and skipped when empty: a mark
      // decoration may not be zero-length, and a range from a build a
      // keystroke stale can point past the end of the text.
      const len = tr.newDoc.length;
      const from = Math.max(0, Math.min(effect.value.from, len));
      const to = Math.max(0, Math.min(effect.value.to, len));
      decorations = to > from ? Decoration.set([PULSE_MARK.range(from, to)]) : Decoration.none;
    }
    return decorations;
  },
  provide: (field) => EditorView.decorations.from(field),
});

// baseTheme rather than cmTheme.js's own theme: the pulse is a
// self-contained, theme-independent amber wash, so it lives with the
// module that owns the class name instead of splitting the two across
// files.
export const pulseExtension = [
  pulseField,
  EditorView.baseTheme({
    ".cm-mm-pulse": { backgroundColor: "rgba(245, 158, 11, 0.35)", borderRadius: "2px" },
  }),
];

export const PULSE_DURATION_MS = 900;

// One pending clear-timer per view: a second pulse before the first
// has faded must not be cut short by the FIRST pulse's timer firing.
const timers = new WeakMap();

/**
 * @param {import("@codemirror/view").EditorView|null|undefined} view
 * @param {number} from
 * @param {number} to
 * @param {number} [durationMs]
 */
export function pulseRange(view, from, to, durationMs = PULSE_DURATION_MS) {
  if (!view || !(to > from)) return;
  clearTimeout(timers.get(view));
  view.dispatch({ effects: setPulse.of({ from, to }) });
  timers.set(
    view,
    setTimeout(() => {
      timers.delete(view);
      try {
        view.dispatch({ effects: setPulse.of(null) });
      } catch {
        // the view was destroyed (tab closed) before the pulse faded
      }
    }, durationMs)
  );
}
