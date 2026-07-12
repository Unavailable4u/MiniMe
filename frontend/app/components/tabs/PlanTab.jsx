
"use client";
import { useState, useEffect } from "react";
import { useSession } from "../../context/SessionContext";
import MermaidDiagram from "../MermaidDiagram";
import WireframePreview from "../WireframePreview";
import Markdown from "../Markdown";
import {
  FileText, GitBranch, Database, Webhook, Skull, Calculator,
  LayoutTemplate, Rocket, FolderOpen,
} from "lucide-react";

// Part 5 — Plan as a dedicated top-level section, same shape as Notebooks
// (§4.7) and Research (§3.9): a project (= workspace, exactly like
// "notebook"/"research project" == workspace_id there) picker on the
// left, sub-tabs on the right.
//
// Unlike Notebooks/Research, NOTHING in this domain writes a Part 0
// knowledge-graph node — confirmed straight from agents/handoff_packager.py
// (§5.6): prd_writer/api_contract_writer/devils_advocate/
// feasibility_estimator are plain generic_worker roles living at
// stage_output:{session_id}:{role}, and architecture_diagrammer/
// schema_diagrammer write to their own bare bus keys
// (ARCHITECTURE_DIAGRAM_KEY/SCHEMA_DIAGRAM_KEY), never eo.knowledge_graph.
// write_node(). So there's no "browse past PRDs for this project" store —
// every artifact sub-tab below takes a paste of a completed chat run's
// output, same known-simplification-flagged-not-hidden pattern
// ResearchTab's ExtractionPanel/ContradictionsPanel already established.
//
// Start Building is the one panel that's genuinely live (dispatches a
// real task) rather than a paste box — see StartBuildingPanel below.
// --- Start Building (§5.6) — auto-parses handoff_packager's own
// summary sentence (confirmed verbatim from eo/result_render.py: since
// handoff_packager's result has no "text"/"issues"/"fixed_code"/"code"/
// "answer"/"papers", it falls through to the summary branch, so this
// IS exactly what renders in chat). Manual fields stay as the
// fallback/override in case the sentence's exact wording ever drifts.
function StartBuildingPanel({ wsId, openScopedSubChat, onOpenChat }) {
  const [pasted, setPasted] = useState("");
  const [appSlug, setAppSlug] = useState("");
  const [cycleGoal, setCycleGoal] = useState("");
  const [starting, setStarting] = useState(false);

  function parsePasted(text) {
    setPasted(text);
    // Matches handoff_packager.py's exact f-string:
    // '...first cycle target: "{target_feature}"... app_slug "{app_slug}"...'
    const slugMatch = /app_slug "([^"]+)"/.exec(text);
    const targetMatch = /first cycle target: "([^"]+)"/.exec(text);
    if (slugMatch) setAppSlug(slugMatch[1]);
    if (targetMatch) setCycleGoal(`Implement ${targetMatch[1]} as scoped in the PRD's first cycle.`);
  }

  async function start() {
    if (!appSlug.trim() || !cycleGoal.trim()) return;
    setStarting(true);
    try {
      const chatId = await openScopedSubChat(wsId, cycleGoal.trim(), appSlug.trim());
      onOpenChat?.(chatId);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="space-y-4 max-w-lg">
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
          Paste handoff_packager's chat response (optional — auto-fills the fields below)
        </label>
        <textarea
          value={pasted}
          onChange={(e) => parsePasted(e.target.value)}
          placeholder='Handoff ready for "..." — 4 feature(s), first cycle target: "Auth". Scoped to app_slug "my-app_ab12cd34"...'
          rows={2}
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">App slug</label>
        <input
          value={appSlug}
          onChange={(e) => setAppSlug(e.target.value)}
          placeholder="my-app_ab12cd34"
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">First task / cycle goal</label>
        <textarea
          value={cycleGoal}
          onChange={(e) => setCycleGoal(e.target.value)}
          placeholder="Implement Auth as scoped in the PRD's first cycle."
          rows={3}
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)]"
        />
      </div>
      <button
        onClick={start}
        disabled={starting || !appSlug.trim() || !cycleGoal.trim()}
        className="flex items-center gap-1.5 text-xs bg-[var(--cyber-amber)] text-black rounded px-3 py-2 font-medium disabled:opacity-50"
      >
        <Rocket size={13} /> {starting ? "Starting…" : "Start building this"}
      </button>
    </div>
  );
}
const SUB_TABS = [
  { id: "prd", label: "PRD", icon: FileText },
  { id: "architecture", label: "Architecture", icon: GitBranch },
  { id: "schema", label: "Schema", icon: Database },
  { id: "api_contract", label: "API Contract", icon: Webhook },
  { id: "devils_advocate", label: "Devil's Advocate", icon: Skull },
  { id: "feasibility", label: "Feasibility", icon: Calculator },
  { id: "wireframes", label: "Wireframes", icon: LayoutTemplate },
  { id: "start_building", label: "Start Building", icon: Rocket },
];

// Strips an optional ```mermaid fenced code block wrapper so a raw paste
// of either the bare diagram or the fenced chat-rendered form both work.
function unfenceMermaid(text) {
  const m = /```(?:mermaid)?\s*\n?([\s\S]*?)```/.exec(text || "");
  return (m ? m[1] : text || "").trim();
}

export default function PlanTab({ onOpenChat }) {
  const { workspaces, sessionId, sendTask, openScopedSubChat } = useSession();
  const [activeWsId, setActiveWsId] = useState(null);
  const [subTab, setSubTab] = useState("prd");

  useEffect(() => {
    if (!activeWsId && workspaces.length > 0) setActiveWsId(workspaces[0].id);
  }, [workspaces, activeWsId]);

  const activeWs = workspaces.find((w) => w.id === activeWsId) || null;

  return (
    <div className="flex h-full">
      {/* Project picker — a "plan project" is just a workspace, same as a
          "notebook"/"research project" is. No new container concept. */}
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col">
        <div className="px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)]">Plan projects</span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {workspaces.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No projects yet. Create one from the chat sidebar's <FolderOpen size={11} className="inline" /> button, then come back here.
            </p>
          )}
          {workspaces.map((ws) => (
            <button
              key={ws.id}
              onClick={() => setActiveWsId(ws.id)}
              className={`w-full text-left px-3 py-2 text-xs border-b border-[var(--neutral-900)] ${
                ws.id === activeWsId
                  ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                  : "text-[var(--neutral-300)] hover:bg-[var(--neutral-900)]"
              }`}
            >
              {ws.name}
              <span className="text-[var(--neutral-600)]"> · {ws.chat_ids.length}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-h-0 flex flex-col">
        <div className="flex items-center gap-1 px-3 py-2 border-b border-[var(--neutral-800)] overflow-x-auto">
          {SUB_TABS.map((t) => {
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                onClick={() => setSubTab(t.id)}
                className={`flex items-center gap-1.5 text-xs rounded-lg px-2.5 py-1.5 whitespace-nowrap ${
                  subTab === t.id
                    ? "bg-[var(--cyber-amber)] text-black font-medium"
                    : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                }`}
              >
                <Icon size={13} />
                {t.label}
              </button>
            );
          })}
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto p-4">
          {!activeWs ? (
            <p className="text-xs text-[var(--neutral-600)]">Pick or create a project to get started.</p>
          ) : subTab === "prd" ? (
            <MarkdownPastePanel
              placeholder="Paste prd_writer's PRD output (from a chat message) below."
              paste_hint="Includes a Features/Priorities/First-cycle-scope section, per prd_writer's brief."
            />
          ) : subTab === "architecture" ? (
            <DiagramPastePanel roleLabel="architecture_diagrammer" />
          ) : subTab === "schema" ? (
            <DiagramPastePanel roleLabel="schema_diagrammer" />
          ) : subTab === "api_contract" ? (
            <MarkdownPastePanel
              placeholder="Paste api_contract_writer's endpoint table output below."
            />
          ) : subTab === "devils_advocate" ? (
            <MarkdownPastePanel
              placeholder="Paste devils_advocate's critique output below."
            />
          ) : subTab === "feasibility" ? (
            <MarkdownPastePanel
              placeholder="Paste feasibility_estimator's output below."
              estimateBanner="Rough complexity signal — not a time/cost estimate (Part 5 §5.4)"
            />
          ) : subTab === "wireframes" ? (
            <WireframesPanel sessionId={sessionId} sendTask={sendTask} />
          ) : (
            <StartBuildingPanel
              wsId={activeWs.id}
              openScopedSubChat={openScopedSubChat}
              onOpenChat={onOpenChat}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// --- Shared paste-pattern panel for PRD / API Contract / Devil's
// Advocate / Feasibility — all plain generic_worker roles with no
// persistent store, same textarea-then-Markdown shape ResearchTab's
// ContradictionsPanel already established. `estimateBanner`, when
// given, renders the same amber "AI-estimated" callout ContradictionsPanel
// uses for consensus_meter (§3.8's labeling discipline, applied here per
// §5.4's identical requirement for feasibility_estimator).
function MarkdownPastePanel({ placeholder, paste_hint, estimateBanner }) {
  const [raw, setRaw] = useState("");
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        {placeholder} There's no per-project "last run" store yet for this domain — this is a
        manual paste step, same as Research's Extraction Table/Contradictions tabs.
        {paste_hint && <> {paste_hint}</>}
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="Paste the role's markdown output here…"
        rows={8}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
      />
      {raw.trim() && (
        <div className={estimateBanner ? "border border-[var(--cyber-amber)]/40 bg-[var(--cyber-amber)]/5 rounded-lg p-3" : ""}>
          {estimateBanner && (
            <p className="text-[10px] uppercase tracking-wide text-[var(--cyber-amber)] mb-2">
              {estimateBanner}
            </p>
          )}
          <Markdown>{raw}</Markdown>
        </div>
      )}
    </div>
  );
}

// --- Architecture / Schema — same paste pattern, rendered via the
// existing MermaidDiagram.jsx instead of Markdown. Accepts either a raw
// mermaid string or a ```mermaid fenced block (unfenceMermaid strips the
// fence if present), since it's not certain which form eo/result_render.py
// renders these two roles' {"mermaid": "..."} bus-key output as in chat.
function DiagramPastePanel({ roleLabel }) {
  const [raw, setRaw] = useState("");
  const mermaidText = unfenceMermaid(raw);
  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Paste {roleLabel}'s output below — either the raw Mermaid syntax or a fenced
        <code className="mx-1 text-[var(--neutral-400)]">```mermaid</code> block copied from a chat message.
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder={"graph TD\n  A[Client] --> B[API]\n  B --> C[(Database)]"}
        rows={6}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
      />
      {mermaidText && (
        <div className="border border-[var(--neutral-800)] rounded-lg overflow-hidden p-3 bg-[var(--neutral-950-a50)]">
          <MermaidDiagram mermaidText={mermaidText} />
        </div>
      )}
    </div>
  );
}

// --- Wireframes — paste the initial HTML, then edit via the existing
// WireframePreview.jsx round trip. Per WireframePreview's own docstring,
// onRequestEdit reuses the ordinary chat-send function, and the edit
// round-trip only works while the CURRENTLY ACTIVE chat (sessionId) is
// the same one that actually ran wireframe_sketcher — flagged plainly
// here rather than hidden, same discipline as every other known
// simplification in this domain.
function WireframesPanel({ sessionId, sendTask }) {
  const [raw, setRaw] = useState("");
  const html = unfenceMermaid(raw.replace(/```html/i, "```")); // reuse the same fence-stripper for ```html blocks

  return (
    <div className="space-y-3">
      <p className="text-[11px] text-[var(--neutral-600)]">
        Paste wireframe_sketcher's HTML output below (raw or a fenced <code>```html</code> block).
        "Send edit" below re-sends the edit instruction into whichever chat is currently open
        (session <code>{sessionId ? sessionId.slice(0, 8) : "none"}</code>) — this only produces a
        real follow-up wireframe if that's the same chat that generated this one (§5.5/§5.7).
      </p>
      <textarea
        value={raw}
        onChange={(e) => setRaw(e.target.value)}
        placeholder="<!doctype html>..."
        rows={6}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
      />
      <WireframePreview
        html={html}
        screenLabel="Pasted wireframe"
        onRequestEdit={sendTask ? (instruction) => sendTask(instruction) : undefined}
      />
    </div>
  );
}

// --- Start Building (§5.6) — the one genuinely live panel in this
// domain. handoff_packager's result isn't a persistent fetch either
// (same session-scoped-only constraint as everywhere else here), but
// asking for a full JSON paste is brittle against whatever
// eo/result_render.py actually renders for this role — instead, take
// the two values a person can read straight off handoff_packager's own
// plain-English summary sentence ('...Scoped to app_slug "X"...',
// '...first cycle target: "Y"...') as two short manual fields. Requires
// the SessionContext.jsx openScopedSubChat/sendTask appSlug patch —
// without it this silently falls back to today's un-scoped dispatch.
function StartBuildingPanel({ wsId, openScopedSubChat, onOpenChat }) {
  const [appSlug, setAppSlug] = useState("");
  const [cycleGoal, setCycleGoal] = useState("");
  const [starting, setStarting] = useState(false);

  async function start() {
    if (!appSlug.trim() || !cycleGoal.trim()) return;
    setStarting(true);
    try {
      const chatId = await openScopedSubChat(wsId, cycleGoal.trim(), appSlug.trim());
      onOpenChat?.(chatId);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="space-y-4 max-w-lg">
      <p className="text-[11px] text-[var(--neutral-600)]">
        After a handoff_packager run finishes, its chat response reads something like{" "}
        <em>"...Scoped to app_slug "my-app_ab12cd34"...first cycle target: "Auth"..."</em> — copy
        those two values in below, then start cycle 1 of the coding domain against that
        pre-filled plan (§5.6).
      </p>
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">App slug</label>
        <input
          value={appSlug}
          onChange={(e) => setAppSlug(e.target.value)}
          placeholder="my-app_ab12cd34"
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)] font-mono"
        />
      </div>
      <div>
        <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">First task / cycle goal</label>
        <textarea
          value={cycleGoal}
          onChange={(e) => setCycleGoal(e.target.value)}
          placeholder='Implement Auth as scoped in the PRD'
          rows={3}
          className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-3 py-2 text-xs outline-none focus:border-[var(--cyber-amber)]"
        />
      </div>
      <button
        onClick={start}
        disabled={starting || !appSlug.trim() || !cycleGoal.trim()}
        className="flex items-center gap-1.5 text-xs bg-[var(--cyber-amber)] text-black rounded px-3 py-2 font-medium disabled:opacity-50"
      >
        <Rocket size={13} /> {starting ? "Starting…" : "Start building this"}
      </button>
    </div>
  );
}
