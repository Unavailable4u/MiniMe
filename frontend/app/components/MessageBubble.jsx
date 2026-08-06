"use client";
import { useState, memo } from "react";
import Markdown from "./Markdown";
import { useSession } from "../context/SessionContext";   // NEW — Data Layer §9d: generateNotebooks
import { Sparkles, X, Loader2, CheckCircle2, Check, Pencil } from "lucide-react";   // NEW — Data Layer §9d; Check/Pencil NEW — CO3 patch 4
import BranchRow from "./notebooks/BranchRow";   // NEW — Phase 2 step 2.10
import { TARGETS } from "../lib/notebookCapabilities";   // NEW — Phase 3 step 3.2
import { useProactiveSuggestions } from "../hooks/useProactiveSuggestions";   // NEW — Phase 3 step 3.7
import ArtifactRenderer from "./ArtifactRenderer";   // NEW — Phase CO, CO2

const TARGETS_BY_KEY = Object.fromEntries(TARGETS.map((t) => [t.key, t]));   // NEW — Phase 3 step 3.2

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
// NEW — CO3 patch 3: durable pause status, alongside the existing
// "ok"/"error" handling.
const PAUSED_STYLE = { label: "Paused", text: "text-amber-300", dot: "bg-amber-300" };

function tierStyle(data) {
  if (data.status === "error") return ERROR_STYLE;
  if (data.status === "paused") return PAUSED_STYLE;
  return TIER_STYLES[data.tier] || { label: `Tier ${data.tier}`, text: "text-[var(--neutral-400)]", dot: "bg-[var(--neutral-500)]" };
}

// NEW — CO3 patch 3: onResume/isActivePause threaded down the same
// path onSendCommand already takes (WorkspaceChatPanel.jsx's rowProps
// -> VirtualMessageRow -> MessageRow -> here). isActivePause is true
// only for the single most-recent message while dock.state.pausedRun
// is still set, so a stale paused bubble further up the thread (from a
// run that has since been resumed) doesn't keep showing a live Resume
// button.
function MessageBubble({ message, onNavigateSubTab, onSendCommand, onResume, isActivePause }) {
  // NEW — Phase 3 step 3.7. Called unconditionally, ahead of every
  // early-return branch below (role === "generation"/"suggestion"/
  // "user") — Rules of Hooks: a hook can't be called only on the path
  // that happens to reach the assistant-bubble render further down.
  // Only that render actually reads the value (see the
  // prerequisite_suggestions check below); the other branches just
  // carry an unused variable, same cost as any other hook call every
  // component already pays for on every render.
  const [proactiveSuggestionsEnabled] = useProactiveSuggestions();

  // NEW — Notebooks Chat-First refinement, Phase 2 step 2.10. A
  // chat-triggered generation's own inline status card — pushed and
  // then live-updated in place (by runId) as the run progresses, by
  // WorkspaceChatPanel.jsx's runGenerateTarget(). Previously a
  // chat-triggered "make me flashcards" left literally no trace in the
  // thread: tryHandleGenerateIntent()/tryHandleClassifiedToolCall() both
  // short-circuit BEFORE sendTask() (the only thing that normally adds a
  // message), and the ONLY feedback was the Working Panel's
  // notebooksGenerateRun key -- itself a single slot a second run fully
  // overwrites (see WorkspaceChatPanel.jsx's own step-2.10 comment).
  // Rendered as stacked BranchRow pills, same as
  // NotebooksGeneratePicker.jsx's popover -- no bubble background here
  // since this isn't prose, it's a status readout.
  if (message.role === "generation") {
    return (
      <div className="flex justify-start">
        <div className="max-w-[80%] w-full space-y-1.5">
          {message.branches.map((b) => (
            <BranchRow key={b.panel_key} branch={b} onNavigate={(subTab) => onNavigateSubTab?.(subTab)} />
          ))}
        </div>
      </div>
    );
  }

  // NEW — Phase 3 step 3.2/3.3. The post-generation cross-sell card:
  // WorkspaceDockContext.jsx's generation_done handling (step 3.1's
  // notebookAffinities.js lookup) appends one of these right after a
  // completed run has a defined pairing. Deliberately its own component
  // rather than folded into PrerequisiteSuggestions below — that one's
  // suggestions come from the backend (task_runner's prerequisite pass,
  // one per chat turn, keyed by topic_id) and accept by calling
  // generateNotebooks() directly for one scoped topic; this one comes
  // from the client-side affinity map (one per finished panel_key) and
  // accepts by re-sending an equivalent chat command through
  // WorkspaceChatPanel.jsx's own send path (step 3.3's onSendCommand —
  // see that file's dispatchText()), same as PrerequisiteSuggestions'
  // "offer, never auto-run" shape but without a second execution path.
  // Card styling below intentionally still matches its sibling's.
  if (message.role === "suggestion") {
    return (
      <AffinitySuggestionCard
        sourceKey={message.sourceKey}
        suggestedKey={message.suggestedKey}
        onSendCommand={onSendCommand}
      />
    );
  }

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
        {/* NEW — CO3 patch 3/4: resume right from the chat bubble, not
            only from WorkingPanel.jsx's AgentStepList affordance.
            ResumeBubbleActions (defined below) mirrors
            AgentStepList.jsx's ManualPauseActions — same
            approve-or-redirect shape, since a bubble's paused message
            is exactly the same "manual pause, no specific role's
            output to review" case that component handles, just
            surfaced in the chat thread instead of the Working Panel. */}
        {data.status === "paused" && isActivePause && (
          <ResumeBubbleActions onResume={onResume} />
        )}
        {/* NEW — Data Layer §9d: api/task_runner.py only ever sets this
            for a status="ok" tier-0/1 chat answer (see
            _maybe_attach_prerequisite_suggestions()'s own docstring for
            the gating) — every other tier/status is simply undefined
            here, so this check alone is enough, no separate tier check
            needed on the frontend side.
            CHANGED — Phase 3 step 3.7: also requires the per-browser
            opt-out (useProactiveSuggestions.js) to still be on. Checked
            at render time (not by asking the backend not to attach the
            data at all) since this is a client-only preference — the
            backend has no notion of it and doesn't need one, it just
            always computes the suggestion the same way it always did;
            this is the one place that decides whether to show it. */}
        {data.result?.prerequisite_suggestions?.length > 0 && proactiveSuggestionsEnabled && (
          <PrerequisiteSuggestions suggestions={data.result.prerequisite_suggestions} />
        )}
      </div>
    </div>
  );
}

// Item 6 (perf audit): memoized so a re-render of WorkspaceChatPanel's
// message list (e.g. from an unrelated sibling message streaming in)
// doesn't force every OTHER already-rendered bubble to re-render too.
// Only pays off now that SessionContext's useCallback pass (item 2) is
// done -- onNavigateSubTab/onSendCommand are stable function identities
// from their callers, and `message` objects are only ever replaced
// (never mutated in place), so this comparison is meaningful rather
// than always-true.
export default memo(MessageBubble);

// NEW — CO3 patch 4. Same shape as AgentStepList.jsx's
// ManualPauseActions — approve-as-is, or open a blank textarea and
// resume with {action: "edit", text}. Kept as its own small component
// (not imported from AgentStepList.jsx) since it's a chat-bubble-sized
// version of the same affordance, not the same component reused in a
// different container — the two files don't otherwise share UI
// components today, and this one's button sizing is deliberately more
// compact to fit inside a bubble rather than the wider Working Panel.
function ResumeBubbleActions({ onResume }) {
  const [redirecting, setRedirecting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function act(action, payload) {
    setBusy(true);
    try {
      await onResume?.({ action, ...(payload || {}) });
    } finally {
      setBusy(false);
    }
  }

  if (redirecting) {
    return (
      <div className="space-y-1.5">
        <textarea
          id="bubble-resume-redirect"
          name="bubble-resume-redirect"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={3}
          placeholder="New instructions to steer the run before it continues…"
          className="w-full resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-relaxed"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => setRedirecting(false)}
            className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-2 py-1"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={busy || !text.trim()}
            onClick={() => act("edit", { text })}
            className="flex items-center gap-1.5 text-xs px-2 py-1 rounded bg-[var(--accent)] text-[var(--accent-text)] font-medium disabled:opacity-60"
          >
            <Check size={12} />
            Redirect & Continue
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <button
        type="button"
        disabled={busy}
        onClick={() => setRedirecting(true)}
        className="flex items-center gap-1.5 text-xs px-2 py-1 rounded border border-[var(--neutral-700)] text-[var(--neutral-400)] hover:bg-white/5 transition-colors"
      >
        <Pencil size={11} />
        Redirect
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => act("approve")}
        className="flex items-center gap-1.5 text-xs px-2 py-1 rounded border border-amber-800 text-amber-300 hover:bg-amber-950/40 transition-colors"
      >
        <Check size={11} />
        Resume
      </button>
    </div>
  );
}

// NEW — Data Layer §9d: chat proactive suggestions, rendered directly
// under a tier 0/1 chat answer whenever eo/prerequisite_suggestions.py's
// Mode C pass (via api/task_runner.py's
// _maybe_attach_prerequisite_suggestions()) found real "prerequisite-of"
// connections (agents/backlink_detector.py) into whatever this turn's
// notebook grounding pulled in.
//
// This is the actual "explicit-agreement gate" §8's Chat integration
// bullet asks for ("offer — never silently start... Generation only
// starts after explicit agreement"): nothing calls generateNotebooks()
// until a person clicks Generate on ONE specific card, and even then
// only that one suggestion is dispatched — accepting one never
// auto-runs the others still sitting in the list.
//
// Scoped to a single Generate target (study_guide) rather than opening
// the full NotebooksGeneratePicker chip flow (NotebooksGeneratePicker.jsx)
// — that picker lives inside Notebooks' own popover and isn't wired as
// a cross-component "open me with this prefilled scope" control today
// (see that file's own SCOPE NOTE on the chat-compose-box entry point
// being deliberately out of scope for this same reason). study_guide is
// the one target every topic can produce regardless of how much raw
// material got pulled in for it — flashcards/quiz genuinely need enough
// scoped content to be worth generating, a study guide degrades
// gracefully to a short summary even off a thin topic. Reaching the
// picker for anything else (flashcards, a mind map, etc.) is still one
// click away in Notebooks — this card isn't meant to replace it, just to
// surface the offer where the conversation already is.
// NEW — Phase 3 step 3.2. One card per notebookAffinities.js pairing
// (study_flashcards -> study_quiz, study_quiz -> study_guide,
// clusters -> suggested_notes as of this step — mindmap -> workflow is
// deferred, see WorkspaceDockContext.jsx's generation_done comment).
// Local run state only (isRunning/isDone/error/dismissed) — no
// dock/BranchRow/notebooksGenerateRun wiring — same reasoning as
// PrerequisiteSuggestions just below: this is a single, scoped,
// explicitly-accepted action, not a run the Working Panel needs to
// trace. Calls generateNotebooks() with `null` scope (whole notebook),
// matching every existing whole-notebook call site
// (NotebooksGeneratePicker.jsx, NotebooksTab.jsx) — every capability
// this can currently suggest has scopeAllowed: "whole" in the Phase 1
// manifest (workflow, the one topic-scoped entry, can't reach this card
// yet per the `enabled` gate in WorkspaceDockContext.jsx).
function AffinitySuggestionCard({ sourceKey, suggestedKey, onSendCommand }) {
  const [dismissed, setDismissed] = useState(false);
  const [sent, setSent] = useState(false);

  const sourceTarget = TARGETS_BY_KEY[sourceKey];
  const suggestedTarget = TARGETS_BY_KEY[suggestedKey];
  // Defensive — a manifest sync (notebookCapabilities.js's
  // syncCapabilitiesFromServer) that drops/renames a key between this
  // message being appended and being rendered shouldn't crash the
  // thread, it should just quietly show nothing.
  if (dismissed || !suggestedTarget) return null;

  // NEW — Phase 3 step 3.3. `onSendCommand` is WorkspaceChatPanel.jsx's
  // `dispatchText()` — the exact same function handleSubmit calls for
  // typed input. Sends `Generate <keyword>`, built from the suggested
  // capability's own first keyword (notebookCapabilities.js's
  // TARGETS[].keywords, the same array parseFreeText() already matches
  // against), so this reliably short-circuits through
  // tryHandleGenerateIntent()'s cheap keyword path before ever reaching
  // classification — no different, latency- or path-wise, than the
  // person having typed it themselves. Once sent, the actual run shows
  // up as its own "generation"-role message (runGenerateTarget()) or,
  // if CHAT_TOOL_CALLING_ENABLED routes it through classification
  // instead, whatever tryHandleClassifiedToolCall() renders for that —
  // this card's own job ends at "sent," it doesn't track the run itself
  // (that would be the second execution path the guide calls out to
  // avoid).
  function accept() {
    onSendCommand?.(`Generate ${suggestedTarget.keywords[0]}`);
    setSent(true);
  }

  const Icon = suggestedTarget.icon || Sparkles;

  return (
    <div className="flex justify-start">
      <div className="max-w-[80%] rounded-lg border border-[var(--neutral-800)] bg-black/20 px-2.5 py-2 text-xs space-y-1.5">
        <div className="flex items-start gap-1.5 text-[var(--neutral-300)]">
          <Icon size={12} className="mt-0.5 shrink-0 text-amber-400" />
          <span>
            Want a <strong>{suggestedTarget.label}</strong> to go with that{" "}
            {sourceTarget?.label || sourceKey}?
          </span>
        </div>
        {sent ? (
          <div className="flex items-center gap-1 pl-[18px] text-emerald-400">
            <CheckCircle2 size={12} /> Sent — check the thread above.
          </div>
        ) : (
          <div className="flex items-center gap-2 pl-[18px]">
            <button
              type="button"
              onClick={accept}
              className="flex items-center gap-1 rounded px-2 py-1 bg-[var(--accent)] text-[var(--accent-text)] font-medium"
            >
              Generate {suggestedTarget.label.toLowerCase()}
            </button>
            <button
              type="button"
              onClick={() => setDismissed(true)}
              className="flex items-center gap-1 text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            >
              <X size={11} /> No thanks
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function PrerequisiteSuggestions({ suggestions }) {
  const { generateNotebooks } = useSession();
  const [dismissed, setDismissed] = useState(() => new Set());
  const [runningId, setRunningId] = useState(null);
  const [doneIds, setDoneIds] = useState(() => new Set());
  const [errorById, setErrorById] = useState({});

  const visible = suggestions.filter((s) => !dismissed.has(s.topic_id));
  if (visible.length === 0) return null;

  async function accept(s) {
    setRunningId(s.topic_id);
    setErrorById((prev) => ({ ...prev, [s.topic_id]: null }));
    try {
      // Scoped to just this ONE topic's own source_section_ids
      // (eo/prerequisite_suggestions.py's "source_node_ids", straight
      // off get_packet()'s covers-edge walk) — accepting one suggestion
      // never regenerates material for the whole notebook.
      const { branches } = await generateNotebooks(
        s.workspace_id, ["study_guide"], { source_node_ids: s.source_node_ids }
      );
      const failed = branches?.find((b) => b.status === "error");
      if (failed) throw new Error(failed.error || "Generation failed");
      setDoneIds((prev) => new Set(prev).add(s.topic_id));
    } catch (err) {
      setErrorById((prev) => ({ ...prev, [s.topic_id]: String(err.message || err) }));
    } finally {
      setRunningId(null);
    }
  }

  function dismiss(topicId) {
    setDismissed((prev) => new Set(prev).add(topicId));
  }

  return (
    <div className="space-y-1.5 pt-1">
      {visible.map((s) => {
        const isRunning = runningId === s.topic_id;
        const isDone = doneIds.has(s.topic_id);
        const error = errorById[s.topic_id];
        return (
          <div
            key={s.topic_id}
            className="rounded-lg border border-[var(--neutral-800)] bg-black/20 px-2.5 py-2 text-xs space-y-1.5"
          >
            <div className="flex items-start gap-1.5 text-[var(--neutral-300)]">
              <Sparkles size={12} className="mt-0.5 shrink-0 text-amber-400" />
              <span>
                <strong>{s.name}</strong> is a prerequisite of {s.for_topic_name}, which you're
                asking about — want a study guide for it too?
              </span>
            </div>
            {error && <div className="text-red-400 pl-[18px]">{error}</div>}
            {isDone ? (
              <div className="flex items-center gap-1 pl-[18px] text-emerald-400">
                <CheckCircle2 size={12} /> Added — check the Notebooks tab.
              </div>
            ) : (
              <div className="flex items-center gap-2 pl-[18px]">
                <button
                  type="button"
                  onClick={() => accept(s)}
                  disabled={isRunning}
                  className="flex items-center gap-1 rounded px-2 py-1 bg-[var(--accent)] text-[var(--accent-text)] font-medium disabled:opacity-50"
                >
                  {isRunning ? <Loader2 size={11} className="animate-spin" /> : null}
                  Generate study guide
                </button>
                <button
                  type="button"
                  onClick={() => dismiss(s.topic_id)}
                  disabled={isRunning}
                  className="flex items-center gap-1 text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                >
                  <X size={11} /> No thanks
                </button>
              </div>
            )}
          </div>
        );
      })}
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
    // Part 6 §6.2 — answerTextOf only ever returns markdown text, so a
    // per-platform card grid (real JSX layout) isn't rendered from here.
    // This branch is the plain-text fallback for any caller that needs
    // one (e.g. the older-cached-response path below, or a future
    // plain-text export) instead of being mistaken for code modules by
    // looksLikeModuleMap() just below.
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
    // Phase CO, CO1 (Master Guide v2, §5): api/task_runner.py's
    // result.answer is now a real synthesis across every role's output
    // (agents/output_organizer.py), not just the final role's leftover
    // text — so it's rendered directly as the whole answer, with no
    // per-role trace toggle competing for space in the bubble. The full
    // unorganized per-role breakdown isn't gone, it just isn't rendered
    // here anymore: it already streams live into WorkingPanel's
    // AgentStepList during the run, and is what CO4 makes the Working
    // Panel show in more detail after the fact too.
    const answer = data.result?.answer;
    // NEW — Phase CO, CO2 (Master Guide v2, §5): any interactive
    // artifacts a role attached (currently only ever populated once a
    // role starts emitting them — see api/task_runner.py's
    // collect_artifacts() call) render as their own bordered cards right
    // under the answer, same "extra structured content sits below the
    // prose, never replaces it" pattern the old per-role trace used.
    const artifacts = data.result?.artifacts;
    if (answer) {
      return (
        <>
          <Markdown>{answer}</Markdown>
          {Array.isArray(artifacts) && artifacts.length > 0 &&
            artifacts.map((artifact, i) => (
              <ArtifactRenderer key={i} artifact={artifact} />
            ))}
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

// Phase CO, CO1 (Master Guide v2, §5): the AgentTraceDisclosure "Show/Hide
// all N agent outputs" toggle that used to live here is removed — that raw
// per-role dump was the wrong place for it. The full per-role breakdown
// still exists and isn't lost: it streams live into WorkingPanel's
// AgentStepList during the run, and CO4 is where the Working Panel gains a
// proper after-the-fact detailed view of it. This file only renders the
// organized answer now.
