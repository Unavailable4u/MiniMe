"use client";
// frontend/app/components/workbench/ProblemsPanel.jsx — W8.3b (Build
// Workbench plan). The bottom panel's Problems tab: what the tier-3
// pipeline's test run and security scans found in the project's files,
// grouped by file, one row per finding. Clicking a row opens that file
// at that line.
//
// Presentational, like ConsolePanel: it neither fetches nor subscribes. EditorWorkbench.jsx owns the findings (the same data also
// feeds the Problems tab's count, the status bar's count and the
// editor's own underlines — none of which can wait for this panel to be
// the open tab) and reloads them from the file provider's one existing
// subscribe() callback; see CloudFileProvider.findings() for why this
// must not open a Pusher subscription of its own.
//
// What a row's "Edited since" means is problems.js's header: the file
// moved to a newer version after the finding was made, so the line may
// have moved. Such rows stay listed, dimmed, and aren't counted.
import { memo, useMemo } from "react";
import { AlertTriangle, Info, XCircle } from "lucide-react";
import { countProblems, groupByPath, sourceLabel, summarizeProblems } from "../../lib/workbench/problems";

const SEVERITY_STYLE = {
  error: { Icon: XCircle, color: "text-red-400", label: "Error" },
  warning: { Icon: AlertTriangle, color: "text-amber-300", label: "Warning" },
  info: { Icon: Info, color: "text-[var(--neutral-400)]", label: "Info" },
};

const STALE_HINT = "This file was edited after it was checked, so the line may have moved.";

function EmptyState({ title, body }) {
  return (
    <div className="h-full flex flex-col items-center justify-center gap-1 text-center">
      <p className="text-xs text-[var(--neutral-400)]">{title}</p>
      <p className="max-w-sm text-[11px] leading-relaxed text-[var(--neutral-600)]">{body}</p>
    </div>
  );
}

function rowMeta(finding) {
  return [
    sourceLabel(finding.source),
    finding.line ? `Ln ${finding.line}` : "Whole file",
    finding.stale ? "Edited since" : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * @param {object} props
 * @param {(object & {stale: boolean})[]} props.findings - problems.js's annotateFindings() output, already sorted
 * @param {boolean} [props.available=true] - false for a file source nothing stores findings for (a local folder)
 * @param {string|null} [props.error] - the last reload's failure, if it failed
 * @param {(finding: object) => void} [props.onOpen] - open the finding's file at its line
 */
function ProblemsPanel({ findings, available = true, error = null, onOpen }) {
  const counts = useMemo(() => countProblems(findings), [findings]);
  const groups = useMemo(() => groupByPath(findings), [findings]);
  const staleCount = useMemo(() => findings.filter((f) => f.stale).length, [findings]);

  if (!available) {
    return (
      <EmptyState
        title="Problems aren't available here"
        body="Problems come from the checks that run when the AI writes project code, so they're only listed for project files stored in the cloud."
      />
    );
  }

  if (findings.length === 0) {
    // A failed load must not read as a clean bill of health.
    if (error) {
      return <EmptyState title="Couldn't load problems" body={error} />;
    }
    return (
      <EmptyState
        title="No problems"
        body="Errors and warnings found in this project's files will be listed here."
      />
    );
  }

  return (
    <div className="h-full flex flex-col gap-1.5">
      <div className="flex shrink-0 items-center justify-between gap-2 px-1.5 text-[11px] text-[var(--neutral-500)]">
        <span>{counts.total === 0 ? "No current problems" : summarizeProblems(counts)}</span>
        {staleCount > 0 && (
          <span title={STALE_HINT} className="text-[var(--neutral-600)]">
            {staleCount} edited since
          </span>
        )}
      </div>

      {error && (
        <p role="alert" className="shrink-0 px-1.5 text-[11px] text-amber-300">
          Couldn&apos;t refresh — showing the last results. {error}
        </p>
      )}

      <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-2">
        {groups.map((group) => (
          <section key={group.path} aria-label={group.path}>
            <h3 className="truncate px-1.5 pb-0.5 text-[11px] font-medium text-[var(--neutral-300)]" title={group.path}>
              {group.path}
            </h3>
            <ul className="flex flex-col">
              {group.items.map((finding) => {
                const { Icon, color, label } = SEVERITY_STYLE[finding.severity] || SEVERITY_STYLE.info;
                return (
                  <li key={finding.key}>
                    <button
                      type="button"
                      onClick={() => onOpen?.(finding)}
                      title={finding.stale ? STALE_HINT : `Open ${finding.path}${finding.line ? ` at line ${finding.line}` : ""}`}
                      className={`w-full flex items-start gap-1.5 rounded px-1.5 py-1 text-left hover:bg-[var(--neutral-900)] ${
                        finding.stale ? "opacity-60" : ""
                      }`}
                    >
                      <Icon size={12} aria-hidden="true" className={`mt-0.5 shrink-0 ${color}`} />
                      <span className="sr-only">{label}:</span>
                      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[11px] text-[var(--neutral-200)]">
                        {finding.message}
                      </span>
                      <span className="shrink-0 pl-2 text-[10px] text-[var(--neutral-600)]">{rowMeta(finding)}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}

export default memo(ProblemsPanel);
