"use client";
// frontend/app/components/workbench/ContextChips.jsx — W4.1 (Build
// Workbench plan). Renders lib/workbench/codeContext.js's `refs` as a
// row of dismissible chips with click-to-jump — the plan's own "chips
// with ×, click-to-jump" bullet.
//
// Reads the store directly via useCodeContext() rather than taking
// `refs`/callbacks as props: BuildTab.jsx mounts CodeContextProvider
// above both this and EditorWorkbench as siblings (see codeContext.js's
// own header), so there's nothing for BuildTab to prop-drill — it just
// renders <ContextChips /> wherever it wants the tray to show today.
// W4.2 is expected to relocate that render to sit just above the chat
// composer ("chips render above the composer" — plan §5 W4.2) without
// this component itself changing at all.
//
// Deliberately NOT wired into any chat message yet — that's W4.2. This
// is only the tray: add / see / remove / jump.
import { memo } from "react";
import { File, Folder, Loader2, MessageSquareCode, Plus, Search, X } from "lucide-react";
import { contextBudget, useCodeContext } from "../../lib/workbench/codeContext";
import { elementLabel } from "../../lib/workbench/elementRef";
import { basename } from "../../lib/workbench/fileTree";
import { isComponentSourcePath } from "../../lib/workbench/usageSites";

const KIND_ICONS = {
  range: MessageSquareCode,
  file: File,
  folder: Folder,
  element: MessageSquareCode,
  error: MessageSquareCode,
};

function chipLabel(ref) {
  // W6.2: an error ref has no `path` when the console/error bridge
  // couldn't map it to a real file:line (see PreviewPane.jsx's own
  // comment on why that's the common case today, ahead of W6.4/6.5's
  // click-to-code work). ADD_REF's reducer only ever copies through a
  // fixed field set (codeContext.js's own ADD_REF case) — there's no
  // room to smuggle a separate "display label" through it — so this
  // reads the label back out of the one field that DOES survive and
  // that a bridge-created error ref always has something in: the first
  // line of its own (already-truncated) snippet.
  if (ref.kind === "error") {
    const firstLine = (ref.snippet || "").split("\n")[0].trim();
    return firstLine || "Console error";
  }
  // W6.5: an element chip reads as the element it is (`button.btn-primary`),
  // not as the file it lives in — the code range is still on the ref for
  // the jump and the prompt.
  if (ref.kind === "element" && ref.element) return elementLabel(ref.element);
  const name = basename(ref.path) || ref.path;
  if (ref.kind === "range") {
    return ref.fromLine === ref.toLine ? `${name} L${ref.fromLine}` : `${name} L${ref.fromLine}-${ref.toLine}`;
  }
  if (ref.kind === "folder") return `${name}/`;
  return name;
}

function Chip({ entry, onRemove, onJump, onFindUsages }) {
  const Icon = KIND_ICONS[entry.kind] || File;
  const jumpable = typeof onJump === "function";
  return (
    <span className="group inline-flex items-center gap-1 rounded-full border border-[var(--neutral-700)] bg-[var(--neutral-900)] pl-2 pr-1 py-0.5 text-[11px] text-[var(--neutral-300)]">
      <Icon size={11} className="shrink-0 text-[var(--neutral-500)]" />
      <button
        type="button"
        disabled={!jumpable}
        onClick={onJump}
        title={jumpable ? `Jump to ${entry.path}` : entry.path || chipLabel(entry)}
        className={`truncate max-w-[12rem] ${jumpable ? "hover:underline underline-offset-2" : "cursor-default"}`}
      >
        {chipLabel(entry)}
      </button>
      {entry.truncated && (
        <span title={`Only the first lines were kept (over the size cap)`} className="text-amber-400">
          …
        </span>
      )}
      {typeof onFindUsages === "function" && (
        <button
          type="button"
          onClick={onFindUsages}
          title="Find where this component is used — the edit may belong at the usage site"
          aria-label={`Find usages of the component in ${entry.path}`}
          className="touch-target shrink-0 rounded-full p-0.5 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          <Search size={11} />
        </button>
      )}
      <button
        type="button"
        onClick={onRemove}
        title="Remove from chat context"
        aria-label={`Remove ${entry.path} from chat context`}
        className="touch-target shrink-0 rounded-full p-0.5 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
      >
        <X size={11} />
      </button>
    </span>
  );
}

/**
 * W7.1b — the result of an element chip's "Find usages" (see
 * lib/workbench/usageSites.js for what the text search does and does not
 * know). Nothing is added to the chat automatically: a hit is only a
 * candidate (the search doesn't resolve imports), so each row has its own
 * Add, which puts that usage's opening tag in the chat as a range chip.
 */
function UsagePanel({ usage, refs, onAdd, onJump, onDismiss }) {
  const named = usage.names.length ? usage.names.map((n) => `<${n}>`).join(", ") : "this component";
  const added = (site) =>
    refs.some((r) => r.kind === "range" && r.path === site.path && r.fromLine === site.fromLine && r.toLine === site.toLine);
  return (
    <div className="mt-1.5 rounded-md border border-[var(--neutral-800)] bg-[var(--neutral-950)] p-2 text-[11px] text-[var(--neutral-300)]">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-[var(--neutral-400)]">
          {usage.status === "loading" ? "Looking for usages…" : `Where ${named} is used`}
        </span>
        <button
          type="button"
          onClick={onDismiss}
          title="Close"
          aria-label="Close usages"
          className="touch-target shrink-0 rounded p-0.5 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          <X size={11} />
        </button>
      </div>
      {usage.status === "loading" && (
        <p className="mt-1 flex items-center gap-1 text-[var(--neutral-500)]">
          <Loader2 size={11} className="animate-spin" /> Searching the project…
        </p>
      )}
      {usage.status !== "loading" && usage.message && <p className="mt-1 text-amber-300">{usage.message}</p>}
      {usage.status === "done" && !usage.message && usage.sites.length === 0 && (
        <p className="mt-1 text-[var(--neutral-500)]">
          No usages found. It may be imported under another name, or only used by a file this search skips.
        </p>
      )}
      {usage.sites.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {usage.sites.map((site) => (
            <li key={`${site.path}:${site.from}`} className="flex items-center gap-1.5">
              <button
                type="button"
                onClick={() => onJump(site)}
                title={`Jump to ${site.path}`}
                className="min-w-0 flex-1 truncate text-left hover:underline underline-offset-2"
              >
                <span className="text-[var(--neutral-200)]">
                  {site.path}:{site.fromLine}
                </span>{" "}
                <span className="font-mono text-[var(--neutral-500)]">{site.preview}</span>
              </button>
              <button
                type="button"
                disabled={added(site)}
                onClick={() => onAdd(site)}
                title={added(site) ? "Already in the chat" : "Add this usage to the chat"}
                aria-label={`Add ${site.path} line ${site.fromLine} to chat context`}
                className="touch-target inline-flex shrink-0 items-center gap-0.5 rounded border border-[var(--neutral-700)] px-1 py-0.5 text-[10px] enabled:hover:text-[var(--neutral-100)] disabled:opacity-50"
              >
                {added(site) ? "Added" : (<><Plus size={10} /> Add</>)}
              </button>
            </li>
          ))}
        </ul>
      )}
      {usage.truncated && <p className="mt-1 text-[var(--neutral-500)]">Showing the first {usage.sites.length} matches.</p>}
    </div>
  );
}

/**
 * @param {object} props
 * @param {string} [props.className] - wrapper class; the caller
 *   decides padding/placement (today: a strip above EditorWorkbench in
 *   BuildTab.jsx's Editor sub-tab; W4.2 may move it above the chat
 *   composer instead).
 */
function ContextChips({ className }) {
  const { refs, removeRef, requestJump, addRef, usage, requestUsageSites, clearUsage } = useCodeContext();

  if (refs.length === 0) return null;

  // W7.1b: add a usage site's opening tag as an ordinary range chip (so it
  // tracks edits like any other), inheriting the provider of the element
  // chip the lookup was for.
  const addUsageSite = (site) => {
    const source = refs.find((r) => r.id === usage?.refId);
    addRef({
      kind: "range",
      path: site.path,
      provider: source?.provider ?? null,
      from: site.from,
      to: site.to,
      fromLine: site.fromLine,
      toLine: site.toLine,
      snippet: site.snippet,
    });
  };

  const budget = contextBudget(refs);

  return (
    <div className={className}>
      {budget.overBudget && (
        <p className="text-[10px] text-amber-300 mb-1">
          That&apos;s {Math.round(budget.totalChars / 1000)}k characters of context — consider removing a few chips.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        {refs.map((ref) => (
          <Chip
            key={ref.id}
            entry={ref}
            onRemove={() => removeRef(ref.id)}
            // Folder refs have no single position to jump to — W5.5
            // expands them server-side into a file set, and until then
            // there's nowhere to scroll.
            onJump={ref.kind === "folder" || (ref.kind === "error" && !ref.path) ? undefined : () => requestJump(ref)}
            // W7.1b: only an element chip whose source can be a component
            // definition (a .jsx/.tsx/.vue/… file) offers the lookup.
            onFindUsages={ref.kind === "element" && isComponentSourcePath(ref.path) ? () => requestUsageSites(ref) : undefined}
          />
        ))}
      </div>
      {usage && (
        <UsagePanel
          usage={usage}
          refs={refs}
          onAdd={addUsageSite}
          onJump={(site) => requestJump({ path: site.path, fromLine: site.fromLine, toLine: site.toLine })}
          onDismiss={clearUsage}
        />
      )}
    </div>
  );
}

export default memo(ContextChips);
