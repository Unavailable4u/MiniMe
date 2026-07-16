"use client";
import { useMemo, useState } from "react";
import { ArrowUp, ArrowDown, ArrowUpDown, Download } from "lucide-react";

// Part 3 §3.9 — "a small new table component is genuinely needed here —
// nothing in the reviewed frontend files is a generic data-table view."
// This is that component: rows = papers, columns = extracted fields,
// sortable, nothing fancier for v1.
//
// Input is the exact GFM pipe-table text eo/result_render.py's
// _render_extraction_table() already produces (Part 3 §3.5) — the same
// text a completed extraction_table_builder chat run shows inline via
// Markdown.jsx's table renderer. Pasting that text here doesn't need a
// second markdown renderer; it needs sorting, which react-markdown's
// table doesn't give for free — so this parses the same pipe-table
// syntax back into {headers, rows} instead of re-implementing the
// extraction logic client-side.

function parsePipeTable(text) {
  const lines = (text || "")
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.startsWith("|") && l.endsWith("|"));
  if (lines.length < 2) return null;

  const splitRow = (line) =>
    line
      .slice(1, -1)
      .split("|")
      .map((cell) => cell.trim());

  const headers = splitRow(lines[0]);
  // lines[1] is the `|---|---|` separator row — skip it.
  const rows = lines.slice(2).map(splitRow);
  return { headers, rows };
}

// Numeric-aware compare so "Year" and any numeric extracted field (e.g.
// sample size, effect size) sort as numbers, not lexicographically
// ("9" before "10" is still wrong for a table of study years/sizes).
function compareValues(a, b) {
  const na = parseFloat(a);
  const nb = parseFloat(b);
  const bothNumeric = !isNaN(na) && !isNaN(nb) && /^-?[\d.,]+$/.test(a?.trim?.() || "") && /^-?[\d.,]+$/.test(b?.trim?.() || "");
  if (bothNumeric) return na - nb;
  return String(a || "").localeCompare(String(b || ""));
}

// Converts agents/note_table_builder.py's build_table() response
// ({ rows: [{node_id, title, tags, ...field_names}], field_names })
// into the same {headers, rows: string[][]} shape parsePipeTable()
// produces, so both the auto-generated path and the legacy manual-paste
// path render through one table body below.
function fromStructuredData(data) {
  if (!data || !Array.isArray(data.rows) || data.rows.length === 0) return null;
  const headers = ["Title", ...data.field_names, "Tags"];
  const rows = data.rows.map((r) => [
    r.title || "Untitled",
    ...data.field_names.map((f) => (r[f] === null || r[f] === undefined ? "" : String(r[f]))),
    (r.tags || []).join(", "),
  ]);
  return { headers, rows };
}

function toCsv(headers, rows) {
  const escape = (v) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [headers, ...rows].map((r) => r.map(escape).join(",")).join("\n");
}

export default function ExtractionTableView({ text, data }) {
  const parsed = useMemo(() => fromStructuredData(data) || parsePipeTable(text), [data, text]);
  const [sortCol, setSortCol] = useState(null);
  const [sortDir, setSortDir] = useState("asc");

  if (!parsed) {
    return (
      <p className="text-xs text-[var(--neutral-500)]">
        Paste the extraction table's markdown output above to view it here.
      </p>
    );
  }

  const { headers, rows } = parsed;

  function downloadCsv() {
    const csv = toCsv(headers, sortedRowsFor(rows));
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "extraction_table.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  function sortedRowsFor(baseRows) {
    if (sortCol === null) return baseRows;
    return [...baseRows].sort((a, b) => {
      const cmp = compareValues(a[sortCol], b[sortCol]);
      return sortDir === "asc" ? cmp : -cmp;
    });
  }

  function toggleSort(colIdx) {
    if (sortCol === colIdx) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(colIdx);
      setSortDir("asc");
    }
  }

  const sortedRows = sortedRowsFor(rows);

  return (
    <div className="border border-[var(--neutral-800)] rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 bg-black/20 border-b border-[var(--neutral-800)]">
        <span className="text-[11px] text-[var(--neutral-500)]">
          {rows.length} row{rows.length === 1 ? "" : "s"}
        </span>
        <button
          onClick={downloadCsv}
          className="flex items-center gap-1 text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
        >
          <Download size={11} /> Export CSV
        </button>
      </div>
      <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-black/30 border-b border-[var(--neutral-800)]">
            {headers.map((h, i) => (
              <th
                key={i}
                onClick={() => toggleSort(i)}
                className="text-left px-3 py-2 font-medium text-[var(--neutral-300)] whitespace-nowrap cursor-pointer select-none hover:text-[var(--neutral-100)]"
              >
                <span className="flex items-center gap-1">
                  {h}
                  {sortCol === i ? (
                    sortDir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />
                  ) : (
                    <ArrowUpDown size={11} className="opacity-30" />
                  )}
                </span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedRows.map((row, ri) => (
            <tr key={ri} className="border-b border-[var(--neutral-900)] hover:bg-[var(--neutral-900)]">
              {headers.map((_, ci) => (
                <td key={ci} className="px-3 py-2 text-[var(--neutral-300)] align-top">
                  {row[ci] ?? "—"}
                </td>
              ))}
            </tr>
          ))}
          {sortedRows.length === 0 && (
            <tr>
              <td colSpan={headers.length} className="px-3 py-4 text-center text-[var(--neutral-600)]">
                No rows.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      </div>
    </div>
  );
}
