// frontend/app/lib/workbench/cmTheme.js — W2.1 (Build Workbench plan).
//
// CodeMirror 6 theme for components/workbench/CodeEditor.jsx, built from
// this app's own CSS custom properties (--neutral-*, --accent,
// --cyber-*, JetBrains Mono) rather than one of CM6's example themes
// (@codemirror/theme-one-dark and friends aren't even in this patch's
// install list). This is the same "reskin everywhere from one place"
// posture globals.css's own --neutral-*/--accent tokens already give
// every other component in this app (see that file's header comments):
// retargeting a value in globals.css reskins the editor along with
// everything else, without touching this file. That includes the
// translucent overlays (selection, active line, search matches) —
// those are derived from the same tokens via withAlpha() rather than
// hand-copied as rgba() literals that would silently go stale.
//
// Values are read from the custom properties via getComputedStyle() at
// theme-CONSTRUCTION time (once, when buildEditorTheme() runs — not on
// every render), with a literal fallback for the handful of contexts
// where there's no live <html> to read from yet (a future SSR pass, a
// unit test running under jsdom with no stylesheet attached). The
// fallback values are a plain copy of globals.css's current literals —
// if those ever drift, this file's fallback goes stale until someone
// notices, which is an acceptable failure mode for a value that's only
// ever used when there's no real theme to read in the first place.
// (The read-once snapshot is also why this is safe today: globals.css
// documents a single dark theme with no [data-theme] switching. If a
// second theme is ever added, buildEditorTheme() has to be re-run on
// the switch — CodeEditor.jsx currently builds it once per editor.)

import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorView } from "@codemirror/view";
import { tags as t } from "@lezer/highlight";
import { withAlpha } from "./editorUtils";

const FALLBACK = {
  "--neutral-100": "#f5f5f5",
  "--neutral-500": "#737373",
  "--neutral-600": "#525252",
  "--neutral-800": "#262626",
  "--neutral-900": "#171717",
  "--neutral-950": "#0a0a0a",
  "--accent": "#FF2052",
  "--accent-text": "#ffffff",
  "--cyber-amber": "#f59e0b",
  "--cyber-violet": "#a78bfa",
  "--cyber-lime": "#34d399",
};

function cssVar(name) {
  if (typeof window !== "undefined" && typeof window.getComputedStyle === "function") {
    const value = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (value) return value;
  }
  return FALLBACK[name];
}

/**
 * Returns the pair of CM6 extensions (dark UI theme + syntax
 * highlighting) components/workbench/CodeEditor.jsx installs once per
 * editor instance. Not memoized here — CodeEditor.jsx only calls this
 * once, inside the effect that creates its single long-lived
 * EditorView (see that file's own comment on why the view is never
 * torn down and rebuilt on prop changes).
 */
export function buildEditorTheme() {
  const colors = {
    bg: cssVar("--neutral-950"),
    panel: cssVar("--neutral-900"),
    border: cssVar("--neutral-800"),
    text: cssVar("--neutral-100"),
    dim: cssVar("--neutral-500"),
    gutterFg: cssVar("--neutral-600"),
    accent: cssVar("--accent"),
    accentText: cssVar("--accent-text"),
    amber: cssVar("--cyber-amber"),
    violet: cssVar("--cyber-violet"),
    lime: cssVar("--cyber-lime"),
  };

  // A faint wash of the text colour — the same "lift this row slightly"
  // overlay CM6's own dark themes use for the active line.
  const activeLine = withAlpha(colors.text, 0.04);

  const theme = EditorView.theme(
    {
      "&": {
        color: colors.text,
        backgroundColor: colors.bg,
        fontFamily: "'JetBrains Mono', monospace",
        fontSize: "13px",
        height: "100%",
      },
      ".cm-scroller": {
        fontFamily: "inherit",
        lineHeight: "1.6",
      },
      ".cm-content": {
        padding: "0.5rem 0",
      },
      // The visible caret is the one drawSelection() paints as a
      // .cm-cursor element — it also forces the native caret
      // transparent (!important), so a `caretColor` rule here would
      // never show. This is the rule that actually colours it.
      "&.cm-focused .cm-cursor, .cm-dropCursor": {
        borderLeftColor: colors.accent,
      },
      "&.cm-focused .cm-selectionBackground, ::selection": {
        // !important: CM6's own base theme styles the focused selection
        // layer with a higher-specificity selector than a plain theme
        // rule can beat.
        backgroundColor: `${withAlpha(colors.accent, 0.25)} !important`,
      },
      ".cm-gutters": {
        backgroundColor: colors.panel,
        color: colors.gutterFg,
        border: "none",
        borderRight: `1px solid ${colors.border}`,
      },
      ".cm-activeLineGutter": {
        backgroundColor: activeLine,
        color: colors.text,
      },
      ".cm-activeLine": {
        backgroundColor: activeLine,
      },
      ".cm-matchingBracket, .cm-nonmatchingBracket": {
        backgroundColor: withAlpha(colors.accent, 0.35),
        outline: "none",
      },
      ".cm-searchMatch": {
        backgroundColor: withAlpha(colors.amber, 0.25),
        outline: `1px solid ${colors.dim}`,
      },
      ".cm-searchMatch.cm-searchMatch-selected": {
        backgroundColor: withAlpha(colors.amber, 0.45),
      },
      ".cm-panels": {
        backgroundColor: colors.panel,
        color: colors.text,
      },
      ".cm-panels.cm-panels-top": {
        borderBottom: `1px solid ${colors.border}`,
      },
      ".cm-panels.cm-panels-bottom": {
        borderTop: `1px solid ${colors.border}`,
      },
      ".cm-panel input, .cm-panel button": {
        backgroundColor: colors.bg,
        color: colors.text,
        border: `1px solid ${colors.border}`,
        borderRadius: "4px",
      },
      ".cm-panel button:hover": {
        borderColor: colors.dim,
      },
      ".cm-tooltip": {
        backgroundColor: colors.panel,
        border: `1px solid ${colors.border}`,
        color: colors.text,
      },
      ".cm-tooltip-autocomplete ul li[aria-selected]": {
        backgroundColor: colors.accent,
        color: colors.accentText,
      },
      ".cm-foldPlaceholder": {
        backgroundColor: "transparent",
        border: `1px solid ${colors.border}`,
        color: colors.dim,
      },
      // components/workbench/CodeEditor.jsx's own indentGuides()
      // extension paints one span per indent level with this class —
      // no core CM6 extension does this (see that file's comment).
      // An inset box-shadow, not border-left: a border adds its own
      // width to the one-character span it's on, which pushes the rest
      // of the line right by 1px per guide and leaves code at different
      // indent depths visibly out of column with each other. A shadow
      // paints inside the box and changes no layout.
      ".cm-mm-indent-guide": {
        boxShadow: `inset 1px 0 0 0 ${colors.border}`,
      },
    },
    { dark: true }
  );

  const highlightStyle = HighlightStyle.define([
    { tag: [t.comment, t.lineComment, t.blockComment, t.docComment], color: colors.dim, fontStyle: "italic" },
    { tag: [t.keyword, t.controlKeyword, t.moduleKeyword, t.operatorKeyword], color: colors.accent },
    { tag: [t.string, t.docString, t.special(t.string)], color: colors.lime },
    { tag: [t.number, t.integer, t.float, t.bool, t.null, t.atom], color: colors.amber },
    { tag: [t.function(t.variableName), t.function(t.propertyName)], color: colors.violet },
    { tag: [t.className, t.typeName, t.namespace], color: colors.violet },
    { tag: t.definition(t.variableName), color: colors.text, fontWeight: "600" },
    { tag: [t.propertyName, t.attributeName], color: colors.violet },
    { tag: [t.tagName], color: colors.accent },
    { tag: [t.operator, t.derefOperator, t.compareOperator, t.updateOperator, t.arithmeticOperator], color: colors.text },
    { tag: [t.punctuation, t.separator, t.bracket, t.paren, t.brace, t.squareBracket, t.angleBracket], color: colors.dim },
    { tag: [t.heading], color: colors.accent, fontWeight: "bold" },
    { tag: [t.link], color: colors.violet, textDecoration: "underline" },
    { tag: [t.emphasis], fontStyle: "italic" },
    { tag: [t.strong], fontWeight: "bold" },
    { tag: [t.url, t.color], color: colors.lime },
    // No token in globals.css is a distinct "error red" (--cyber-magenta
    // is the same brand crimson as --accent, which keywords already
    // use), so this one stays a literal: an invalid token has to be
    // told apart from a keyword at a glance.
    { tag: [t.invalid], color: "#ef4444" },
  ]);

  return [theme, syntaxHighlighting(highlightStyle)];
}
