"use client";
// frontend/app/components/workbench/StatusBar.jsx — W2.3a (Build
// Workbench plan). The thin strip along the bottom of the workbench:
// which file source is showing (provider), the active file's save
// state, its language and caret position, and the "Pending changes (N)"
// chip (W5.4 feeds it).
//
// Presentational only. Everything comes in as props, which is what
// keeps this correct over both Cloud and Local files: the local
// provider just passes a different `providerId` ("Local folder" vs
// "Project files") and, as of W3.1 part 2, its own "awaiting-
// confirmation" save state alongside the ones Cloud already used.
//
// W8.3b: the problem counts (errors / warnings, plus info when there is
// any) sit next to "Pending changes". Omitting `problemCounts` leaves
// the slot out entirely — that is how a file source with no findings
// (a local folder) opts out instead of showing a permanent "0 0".
import { memo } from "react";
import { AlertTriangle, Info, Loader2, XCircle } from "lucide-react";
import { summarizeProblems } from "../../lib/workbench/problems";

const PROVIDER_LABELS = {
  cloud: "Project files",
  local: "Local folder",
};

// The "Pending changes" slot (W2.3b). Pending changes = AI-proposed
// edits waiting for the person's review (plan D4) — not to be confused
// with unsaved edits, which the save state next to it covers. Always
// rendered so the slot is visible before there's anything in it; it
// only becomes a button once there's a count AND something to do on
// click (W5.4 passes both).
function PendingChanges({ count, onClick }) {
  if (count > 0 && onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        title="AI-proposed edits waiting for your review"
        className="text-amber-300 hover:text-amber-200 hover:underline underline-offset-2"
      >
        Pending changes ({count})
      </button>
    );
  }
  return (
    <span
      title="AI-proposed edits waiting for your review will be listed here"
      className={count > 0 ? "text-amber-300" : undefined}
    >
      Pending changes ({count})
    </span>
  );
}

// The problem counts (W8.3b). A count turns red / amber only while it is
// non-zero, so a clean project reads as quiet grey. Always a button when
// there is somewhere to go (`onClick`), same as PendingChanges above.
function ProblemCounts({ counts, onClick }) {
  const { error = 0, warning = 0, info = 0 } = counts;
  const title = summarizeProblems(counts);
  const content = (
    <>
      <span className={`flex items-center gap-0.5 ${error > 0 ? "text-red-400" : ""}`}>
        <XCircle size={10} aria-hidden="true" /> {error}
      </span>
      <span className={`flex items-center gap-0.5 ${warning > 0 ? "text-amber-300" : ""}`}>
        <AlertTriangle size={10} aria-hidden="true" /> {warning}
      </span>
      {info > 0 && (
        <span className="flex items-center gap-0.5">
          <Info size={10} aria-hidden="true" /> {info}
        </span>
      )}
    </>
  );
  const className = "flex items-center gap-2";
  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        title={title}
        aria-label={`Problems: ${title}`}
        className={`${className} hover:text-[var(--neutral-200)]`}
      >
        {content}
      </button>
    );
  }
  return (
    <span title={title} aria-label={`Problems: ${title}`} className={className}>
      {content}
    </span>
  );
}

/**
 * @param {object} props
 * @param {string} props.providerId - FileProvider.id ("cloud" | "local")
 * @param {"saved"|"dirty"|"saving"|"awaiting-confirmation"|"error"|"conflict"|null} props.saveState - null when no file is active; "conflict" (W2.5) = the server rejected the save because the file changed underneath it and the person hasn't chosen Reload theirs / Keep mine yet; "awaiting-confirmation" (W3.1 part 2, Local only) = proposed but not yet confirmed on PendingActionBar
 * @param {string} [props.saveError] - shown as the tooltip on "Save failed"
 * @param {number} [props.version] - the active buffer's server version
 * @param {string|null} [props.language]
 * @param {{line: number, col: number}|null} [props.cursor]
 * @param {number} [props.pendingCount=0] - proposals waiting for review
 * @param {() => void} [props.onPendingClick] - opens the review tray (W5.4); without it the chip is inert
 * @param {{error: number, warning: number, info: number}} [props.problemCounts] - W8.3b: the findings that still apply; omitted = no problems slot at all
 * @param {() => void} [props.onProblemsClick] - opens the Problems tab (W8.3b); without it the counts are inert
 * @param {boolean} [props.reserveRight] - keep the bar's right end clear for the app's floating "open chat" bubble
 */
function StatusBar({
  providerId,
  saveState,
  saveError,
  version,
  language,
  cursor,
  pendingCount = 0,
  onPendingClick,
  problemCounts,
  onProblemsClick,
  reserveRight = false,
}) {
  return (
    // While the chat dock is closed the app draws a round "open chat"
    // button fixed at the screen's bottom-right corner — over this bar's
    // right end, where the caret position and language are. `reserveRight`
    // (BuildTab passes it while the dock is closed) leaves room for it
    // (42px button + a gap) so nothing here is hidden behind it.
    <div
      className="shrink-0 flex items-center justify-between gap-3 h-6 pl-3 text-[10px] text-[var(--neutral-500)] border-t border-[var(--neutral-800)] bg-[var(--neutral-950)]"
      style={{ paddingRight: reserveRight ? 56 : 12 }}
    >
      <div className="flex items-center gap-3 min-w-0">
        <span title="Where these files live">{PROVIDER_LABELS[providerId] || providerId}</span>

        <span role="status" aria-live="polite" className="flex items-center gap-1 truncate">
          {saveState === "saving" && (
            <>
              <Loader2 size={10} className="animate-spin" /> Saving…
            </>
          )}
          {saveState === "error" && (
            <span className="text-red-400" title={saveError || undefined}>
              Save failed
            </span>
          )}
          {saveState === "conflict" && (
            <span className="text-amber-300" title="The server has a newer version of this file">
              Save conflict
            </span>
          )}
          {saveState === "awaiting-confirmation" && (
            <span className="text-amber-300" title="Confirm or deny it above to finish saving">
              Waiting for confirmation
            </span>
          )}
          {saveState === "dirty" && <span className="text-amber-300">Unsaved changes</span>}
          {saveState === "saved" && <span>{version ? `Saved · v${version}` : "Saved"}</span>}
        </span>

        {problemCounts && <ProblemCounts counts={problemCounts} onClick={onProblemsClick} />}

        <PendingChanges count={pendingCount} onClick={onPendingClick} />
      </div>

      <div className="flex items-center gap-3 shrink-0">
        {cursor && (
          <span>
            Ln {cursor.line}, Col {cursor.col}
          </span>
        )}
        {saveState && <span>{language || "plain text"}</span>}
      </div>
    </div>
  );
}

export default memo(StatusBar);
