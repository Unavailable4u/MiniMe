"use client";
// frontend/app/components/workbench/ProjectSearchPanel.jsx — W2.6
// (Build Workbench plan). The bottom panel's Search tab: a query box
// (+ case-sensitive toggle) over hooks/useProjectSearch.js, rendering
// lib/workbench/projectSearch.js's own {path, matches[]} shape file by
// file, line by line. Clicking a result reports it through
// `onJumpToResult` — opening the file first if it isn't already, and
// moving the caret there, are both the workbench's job (EditorWorkbench
// owns the open tabs and the CodeMirror views), not this panel's.
//
// Presentational, like Explorer/EditorTabs/StatusBar: every value here
// is a prop from useProjectSearch(), called once in EditorWorkbench and
// passed down, so a project switch (which remounts the whole workbench,
// key={selected.id}) naturally gets a fresh search instead of this
// panel needing to know that happened.
import { useEffect, useRef } from "react";
import { CaseSensitive, Loader2, Search } from "lucide-react";
import { basename, dirname } from "../../lib/workbench/fileTree";

// Renders one already-matched line, coloring the ranges
// searchFileContent() found (already remapped to `text`'s own
// coordinates by trimLineForDisplay(), so no further adjustment is
// needed here).
function HighlightedLine({ text, ranges }) {
  if (!ranges || ranges.length === 0) return <>{text}</>;
  const parts = [];
  let cursor = 0;
  ranges.forEach(([start, end], i) => {
    if (start > cursor) parts.push(<span key={`t${i}`}>{text.slice(cursor, start)}</span>);
    parts.push(
      <span key={`m${i}`} className="rounded-sm bg-[var(--accent)]/30 text-[var(--neutral-100)]">
        {text.slice(start, end)}
      </span>
    );
    cursor = end;
  });
  if (cursor < text.length) parts.push(<span key="tail">{text.slice(cursor)}</span>);
  return <>{parts}</>;
}

function summaryLine({ error, query, loading, matchCount, results, truncated, filesScanned, totalFiles }) {
  if (error) return error;
  if (!query.trim()) return `Searches ${totalFiles} file${totalFiles === 1 ? "" : "s"} — open and closed.`;
  if (loading) return "Searching…";
  if (matchCount === 0) return "No matches.";
  const files = `${results.length} file${results.length === 1 ? "" : "s"}`;
  const matches = `${matchCount} match${matchCount === 1 ? "" : "es"}`;
  return `${matches} in ${files}${truncated ? " (showing the first results)" : ""} · scanned ${filesScanned}/${totalFiles}`;
}

/**
 * @param {object} props
 * @param {string} props.query
 * @param {(q: string) => void} props.onQueryChange
 * @param {boolean} props.caseSensitive
 * @param {(v: boolean) => void} props.onCaseSensitiveChange
 * @param {{path: string, matches: object[], truncated: boolean}[]} props.results
 * @param {number} props.matchCount
 * @param {boolean} props.truncated
 * @param {boolean} props.loading
 * @param {string|null} props.error
 * @param {number} props.filesScanned
 * @param {number} props.totalFiles
 * @param {(path: string, line: number, column: number, endColumn: number) => void} props.onJumpToResult
 * @param {number} [props.focusSeq] - bumped by the workbench (Cmd/Ctrl-Shift-F) to refocus and re-select the query box
 */
export default function ProjectSearchPanel({
  query,
  onQueryChange,
  caseSensitive,
  onCaseSensitiveChange,
  results,
  matchCount,
  truncated,
  loading,
  error,
  filesScanned,
  totalFiles,
  onJumpToResult,
  focusSeq,
}) {
  const inputRef = useRef(null);

  useEffect(() => {
    if (focusSeq) inputRef.current?.select();
  }, [focusSeq]);

  return (
    <div className="h-full flex flex-col gap-2">
      <div className="shrink-0 flex items-center gap-1.5 rounded border border-[var(--neutral-800)] bg-[var(--neutral-950)] px-1.5 focus-within:border-[var(--neutral-600)]">
        <Search size={11} className="shrink-0 text-[var(--neutral-600)]" />
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder="Search across project files"
          aria-label="Search across project files"
          spellCheck={false}
          autoComplete="off"
          className="touch-input min-w-0 flex-1 bg-transparent py-1 text-xs text-[var(--neutral-200)] placeholder:text-[var(--neutral-600)] outline-none"
        />
        <button
          type="button"
          onClick={() => onCaseSensitiveChange(!caseSensitive)}
          aria-pressed={caseSensitive}
          title="Match case"
          className={`touch-target shrink-0 rounded px-1 py-0.5 ${
            caseSensitive ? "text-[var(--accent)]" : "text-[var(--neutral-600)] hover:text-[var(--neutral-300)]"
          }`}
        >
          <CaseSensitive size={13} />
        </button>
        {loading && <Loader2 size={11} className="shrink-0 animate-spin text-[var(--neutral-600)]" />}
      </div>

      <p className="shrink-0 text-[11px] text-[var(--neutral-600)]">
        {summaryLine({ error, query, loading, matchCount, results, truncated, filesScanned, totalFiles })}
      </p>

      <div className="flex-1 min-h-0 overflow-auto">
        {results.map((fileResult) => (
          <div key={fileResult.path} className="mb-2">
            <div className="sticky top-0 z-10 flex items-baseline gap-1.5 bg-[var(--neutral-950)] px-1 py-0.5 text-[11px]">
              <span className="truncate text-[var(--neutral-200)]">{basename(fileResult.path)}</span>
              <span className="truncate text-[var(--neutral-600)]">{dirname(fileResult.path)}</span>
              <span className="ml-auto shrink-0 text-[var(--neutral-600)]">
                {fileResult.matches.length}
                {fileResult.truncated ? "+" : ""}
              </span>
            </div>
            {fileResult.matches.map((m) => (
              <button
                key={`${fileResult.path}:${m.line}:${m.column}`}
                type="button"
                onClick={() => onJumpToResult(fileResult.path, m.line, m.column, m.column + query.trim().length)}
                title={`Line ${m.line}`}
                className="block w-full truncate rounded px-2 py-0.5 text-left font-mono text-[11px] text-[var(--neutral-400)] hover:bg-[var(--neutral-900)] hover:text-[var(--neutral-100)]"
              >
                <span className="mr-2 select-none text-[var(--neutral-600)]">{m.line}</span>
                <HighlightedLine text={m.text} ranges={m.ranges} />
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
