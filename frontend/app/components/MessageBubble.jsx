"use client";
import { useState } from "react";
import Markdown from "./Markdown";

// Per-tier accent color — gives each response a quick at-a-glance
// identity in the chat instead of every bubble looking identical. Kept
// to Tailwind's built-in palette (no arbitrary hex) so it stays
// consistent with the rest of the dark theme.
const TIER_STYLES = {
  sga: { label: "Instant", text: "text-emerald-400", dot: "bg-emerald-400" },
  cache: { label: "Cached", text: "text-emerald-400", dot: "bg-emerald-400" },
  0: { label: "Tier 0 · Instant", text: "text-emerald-400", dot: "bg-emerald-400" },
  1: { label: "Tier 1 · Direct", text: "text-sky-400", dot: "bg-sky-400" },
  2: { label: "Tier 2 · Fixed", text: "text-violet-400", dot: "bg-violet-400" },
  3: { label: "Tier 3 · Ultimate Structure", text: "text-amber-400", dot: "bg-amber-400" },
};
const ERROR_STYLE = { label: "Error", text: "text-red-400", dot: "bg-red-500" };

function tierStyle(data) {
  if (data.status === "error") return ERROR_STYLE;
  return TIER_STYLES[data.tier] || { label: `Tier ${data.tier}`, text: "text-[var(--neutral-400)]", dot: "bg-[var(--neutral-500)]" };
}

export default function MessageBubble({ message }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        {/* whitespace-pre-wrap so a multiline/indented user message (e.g.
            pasted code) actually keeps its line breaks and indentation
            instead of collapsing to one line. */}
        <div className="bg-[var(--neutral-800)] rounded-lg px-[var(--density-bubble-padding-x)] py-[var(--density-bubble-padding-y)] text-sm max-w-[80%] whitespace-pre-wrap leading-[var(--density-line-height)]">
          {message.text}
        </div>
      </div>
    );
  }

  const { data } = message;
  const style = tierStyle(data);
  return (
    <div className="flex justify-start">
      <div className="bg-[var(--neutral-900)] border border-[var(--neutral-800)] rounded-lg px-[var(--density-bubble-padding-x)] py-[var(--density-bubble-padding-y)] text-sm max-w-[80%] space-y-[var(--density-card-gap)]">
        <div className={`flex items-center gap-1.5 text-xs font-medium ${style.text}`}>
          <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
          {style.label}
          <span className="text-[var(--neutral-600)] font-normal">· {data.status}</span>
        </div>
        <ResultBody data={data} />
      </div>
    </div>
  );
}

// Mirrors eo/result_render.py's render_agent_result() — keep these two
// in sync if a new agent result shape is added on the backend. Turns
// ANY agent result shape in this codebase into markdown text instead of
// falling back to a raw JSON/object dump.
function renderCodeModules(modules) {
  const names = Object.keys(modules || {});
  if (names.length === 0) return "_(no modules)_";
  return names
    .map((name) => {
      const entry = modules[name];
      const isObj = entry && typeof entry === "object";
      const lang = isObj ? entry.language || "" : "";
      const code = isObj ? entry.code || "" : String(entry);
      return `**${name}**\n\`\`\`${lang}\n${code}\n\`\`\``;
    })
    .join("\n\n");
}

function looksLikeModuleMap(result) {
  const values = Object.values(result);
  return values.every(
    (v) => typeof v === "string" || (v && typeof v === "object" && "code" in v)
  );
}

// Mirrors eo/result_render.py's _render_extraction_table() —
// agents/extraction_table_builder.py's shape (Part 3 §3.5).
function renderExtractionTable(result) {
  const papers = result.papers || [];
  const fieldNames = result.field_names || [];
  if (papers.length === 0) return "_(no papers extracted)_";

  const esc = (v) => {
    if (v === null || v === undefined || v === "") return "—";
    return String(v).replaceAll("|", "\\|").replaceAll("\n", " ");
  };

  const headers = ["Title", "Year", ...fieldNames.map((f) => f.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()))];
  const lines = [
    "| " + headers.join(" | ") + " |",
    "|" + headers.map(() => "---").join("|") + "|",
  ];
  for (const p of papers) {
    const row = [esc(p.title), esc(p.year), ...fieldNames.map((f) => esc(p[f]))];
    lines.push("| " + row.join(" | ") + " |");
  }
  return lines.join("\n");
}

// Part 6 §6.4 — content_calendar_builder's structured
// {date, platform, content_ref} row list (agents/exporter.py's
// export_content_calendar() input shape, unchanged here). Rendered as
// the same date/platform/content markdown table exporter.py's own
// _write_calendar_md() writer produces, so the in-chat preview and the
// downloadable .md export always show identical column order/labels —
// deliberately NOT reusing renderExtractionTable's headers (papers has
// its own Title/Year/field shape; this is a different structured list).
function isCalendarEntryList(result) {
  return (
    Array.isArray(result) &&
    result.length > 0 &&
    result.every((r) => r && typeof r === "object" && !Array.isArray(r) &&
      ("date" in r || "platform" in r || "content_ref" in r))
  );
}

function renderContentCalendar(entries) {
  if (entries.length === 0) return "_(no calendar entries)_";
  const esc = (v) => {
    const s = String(v ?? "").trim();
    return s ? s.replaceAll("|", "\\|") : "—";
  };
  const lines = [
    "| Date | Platform | Content |",
    "|------|----------|---------|",
  ];
  for (const row of entries) {
    lines.push(`| ${esc(row.date)} | ${esc(row.platform)} | ${esc(row.content_ref)} |`);
  }
  return lines.join("\n");
}

// Part 6 §6.2/§6.7 — content_adapter_pool's {platform: content} fan-out
// (agents/content_adapter_pool.py). Gated on role, not shape alone:
// a flat map of platform -> string is structurally indistinguishable
// from looksLikeModuleMap()'s {module: code} shape below, so without
// checking which role actually produced it, a set of platform variants
// would get mistakenly rendered as source-code blocks.
function isPlatformContentMap(result, role) {
  if (role !== "content_adapter_pool") return false;
  if (!result || typeof result !== "object" || Array.isArray(result)) return false;
  const values = Object.values(result);
  return values.length > 0 && values.every((v) => typeof v === "string");
}

function answerTextOf(result, role) {
  if (result == null) return "";
  if (typeof result === "string") return result;
  if (typeof result !== "object") return String(result);

  if (result.text) return result.text;

  if (Array.isArray(result.issues)) {
    // agents/reviewer.py's "verifier" shape.
    const lines = [];
    const summary = (result.summary || "").trim();
    if (summary) lines.push(summary);
    if (result.issues.length > 0) {
      if (summary) lines.push("");
      for (const issue of result.issues) {
        const count = issue.flagged_by_count;
        const tag = count ? ` _(flagged by ${count} reviewer${count !== 1 ? "s" : ""})_` : "";
        lines.push(`- **[${issue.severity || ""}]** \`${issue.module || ""}\`: ${issue.description || ""}${tag}`);
      }
    } else if (!summary) {
      lines.push("No issues found.");
    }
    return lines.join("\n");
  }

  if (result.fixed_code && typeof result.fixed_code === "object") {
    // agents/fixer_pool.py's "fixer" shape.
    return renderCodeModules(result.fixed_code);
  }

  if (result.code) return result.code;
  if (result.answer) return String(result.answer);

  if (Array.isArray(result.papers) && Array.isArray(result.field_names)) {
    // agents/extraction_table_builder.py's shape (Part 3 §3.5) — checked
    // via field_names specifically so this doesn't also catch
    // agents/academic_search.py's {"papers", "edges_written"} shape,
    // which has no field_names and reads better as its own summary
    // line below.
    return renderExtractionTable(result);
  }

  if (isCalendarEntryList(result)) {
    // Part 6 §6.4 — checked before looksLikeModuleMap below since a
    // calendar row list is an array (Object.values on it would return
    // its row objects, which happen to fail looksLikeModuleMap's check
    // anyway — but being explicit here keeps this from ever depending
    // on that incidental fact).
    return renderContentCalendar(result);
  }

  if (isPlatformContentMap(result, role)) {
    // Part 6 §6.2 — handled as JSX (PlatformVariantCards) at the
    // AgentTraceDisclosure call site below, not here: answerTextOf only
    // ever returns markdown text, and a per-platform card grid needs
    // real layout, not a markdown string. This branch exists so any
    // OTHER caller of answerTextOf (e.g. a future plain-text export)
    // still gets a readable fallback instead of being mistaken for code
    // modules by looksLikeModuleMap() just below.
    return Object.entries(result)
      .map(([platform, content]) => `**${platform.replaceAll("_", " ")}**\n\n${content}`)
      .join("\n\n---\n\n");
  }

  if (looksLikeModuleMap(result)) {
    // agents/code_writers.py ("implementer") / agents/test_writer.py
    // ("test_writer") flat {module: code} shape, including the
    // legitimate empty-object "no tests generated" case.
    return renderCodeModules(result);
  }

  if (typeof result.summary === "string" && result.summary) {
    // Part 3's other real-action roles (academic_search,
    // contradiction_prefilter, source_quality_flagger,
    // citation_graph_builder, ...) all already produce a human-readable
    // "summary" string for exactly this purpose.
    return result.summary;
  }

  // Genuinely unrecognized shape — pretty-printed JSON (still readable)
  // rather than React's default object-to-string coercion.
  try {
    return "```json\n" + JSON.stringify(result, null, 2) + "\n```";
  } catch {
    return String(result);
  }
}

// Part 6 §6.2/§6.7 — one card per platform variant instead of joined
// markdown text: these are N independent, unrelated outputs from one
// brief (Part 6 §6.2's fan-out), so concatenating them into a single
// markdown blob makes it hard to tell where one platform's copy ends
// and the next begins. Ships as plain stacked/grid cards, no carousel or
// tabs — same "build a comparison view only if scanning plain text
// proves genuinely hard" discipline Part 6 §6.7 calls for; a labeled
// grid is already enough to scan side by side.
const PLATFORM_LABELS = {
  twitter: "Twitter / X",
  linkedin: "LinkedIn",
  instagram_caption: "Instagram Caption",
  press_release: "Press Release",
};

function PlatformVariantCards({ data }) {
  const platforms = Object.keys(data || {});
  if (platforms.length === 0) return <Markdown>{"_(no platform variants)_"}</Markdown>;
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
      {platforms.map((platform) => (
        <div
          key={platform}
          className="rounded-lg border border-[var(--neutral-800)] bg-black/30 p-2.5 space-y-1.5"
        >
          <div className="text-[11px] font-medium text-[var(--neutral-400)] uppercase tracking-wide">
            {PLATFORM_LABELS[platform] || platform.replaceAll("_", " ")}
          </div>
          <Markdown>{String(data[platform] ?? "")}</Markdown>
        </div>
      ))}
    </div>
  );
}

function ResultBody({ data }) {
  if (data.status === "error" || data.message) {
    return <div className="text-red-400 whitespace-pre-wrap">{data.message}</div>;
  }
  if (data.tier === "sga" || data.tier === "cache") {
    return <Markdown>{data.result?.answer}</Markdown>;
  }
  if (data.tier === 0) {
    return <Markdown>{data.result?.answer}</Markdown>;
  }
  if (data.tier === 1) {
    // NOT run through Markdown here on purpose: result.code is raw code
    // text, not markdown prose — parsing it as markdown risks mangling
    // things like underscores (_snake_case_) as italics. Styled the same
    // as Markdown's own fenced-code blocks for visual consistency.
    return (
      <div className="rounded-lg border border-[var(--neutral-800)] bg-black/50 overflow-hidden">
        <pre className="overflow-x-auto p-3 text-xs text-[var(--neutral-300)]">
          <code>{data.result?.code}</code>
        </pre>
      </div>
    );
  }
  if (data.tier === 2) {
    const text = answerTextOf(data.result?.output);
    return text ? (
      <Markdown>{text}</Markdown>
    ) : (
      <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
        {JSON.stringify(data.result?.output, null, 2)}
      </pre>
    );
  }
  if (data.tier === 3) {
    // NEW — bug fix: api/task_runner.py now returns a clean
    // result.answer (the final role's own text) alongside the full
    // role-keyed result.output tree. Render just the answer as markdown,
    // with the full multi-agent trace tucked behind an optional toggle
    // instead of dumped inline as raw JSON.
    const answer = data.result?.answer;
    if (answer) {
      return (
        <>
          <Markdown>{answer}</Markdown>
          {data.result?.output && Object.keys(data.result.output).length > 1 && (
            <AgentTraceDisclosure output={data.result.output} finalRole={data.result?.final_role} />
          )}
        </>
      );
    }
    // Fallback for older cached responses that predate the "answer"
    // field — still avoid a raw JSON dump.
    const fallbackText = answerTextOf(
      data.result?.output && data.result?.final_role
        ? data.result.output[data.result.final_role]
        : null,
      data.result?.final_role
    );
    return fallbackText ? (
      <Markdown>{fallbackText}</Markdown>
    ) : (
      <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
        {JSON.stringify(data.result?.output, null, 2)}
      </pre>
    );
  }
  return (
    <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

// Collapsed by default — the full per-role breakdown (writer, reviewer,
// editor, ...) is useful for inspecting the pipeline but shouldn't
// compete with the actual answer for attention. The live per-agent
// steps already stream into WorkingPanel's AgentStepList as the task
// runs; this is just a static after-the-fact version scoped to this one
// message, for whoever wants to double check what each role produced.
function AgentTraceDisclosure({ output, finalRole }) {
  const [open, setOpen] = useState(false);
  const roles = Object.keys(output);
  return (
    <div className="pt-1 border-t border-[var(--neutral-800-a70)]">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)] transition-colors flex items-center gap-1"
      >
        <span>{open ? "▾" : "▸"}</span>
        {open ? "Hide" : "Show"} all {roles.length} agent outputs
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          {roles.map((role) => (
            <div key={role} className="rounded-lg border border-[var(--neutral-800)] bg-black/30 p-2">
              <div
                className={`text-[11px] font-medium mb-1 ${
                  role === finalRole ? "text-amber-400" : "text-[var(--neutral-500)]"
                }`}
              >
                {role}
                {role === finalRole ? " · final" : ""}
              </div>
              {isPlatformContentMap(output[role], role) ? (
                <PlatformVariantCards data={output[role]} />
              ) : (
                <Markdown>{answerTextOf(output[role], role)}</Markdown>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
