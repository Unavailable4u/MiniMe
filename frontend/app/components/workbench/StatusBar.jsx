"use client";
// frontend/app/components/workbench/StatusBar.jsx — W2.3a (Build
// Workbench plan). The thin strip along the bottom of the workbench:
// which file source is showing (provider), the active file's save
// state, its language and caret position, and a slot for the "Pending
// changes (N)" tray W5.4 will fill in.
//
// Presentational only. Everything comes in as props so it stays
// correct over Cloud files now and Local files after W3.1 — the local
// provider will just pass a different `providerId` and a save state
// that reads "Waiting for confirmation" once that exists.
import { memo } from "react";
import { Loader2 } from "lucide-react";

const PROVIDER_LABELS = {
  cloud: "Project files",
  local: "Local folder",
};

/**
 * @param {object} props
 * @param {string} props.providerId - FileProvider.id ("cloud" | "local")
 * @param {"saved"|"dirty"|"saving"|"error"|null} props.saveState - null when no file is active
 * @param {string} [props.saveError] - shown as the tooltip on "Save failed"
 * @param {number} [props.version] - the active buffer's server version
 * @param {string|null} [props.language]
 * @param {{line: number, col: number}|null} [props.cursor]
 * @param {import("react").ReactNode} [props.pendingSlot] - W5.4's "Pending changes" tray; nothing renders until then
 */
function StatusBar({ providerId, saveState, saveError, version, language, cursor, pendingSlot }) {
  return (
    <div className="shrink-0 flex items-center justify-between gap-3 h-6 px-3 text-[10px] text-[var(--neutral-500)] border-t border-[var(--neutral-800)] bg-[var(--neutral-950)]">
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
          {saveState === "dirty" && <span className="text-amber-300">Unsaved changes</span>}
          {saveState === "saved" && <span>{version ? `Saved · v${version}` : "Saved"}</span>}
        </span>

        {pendingSlot}
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
