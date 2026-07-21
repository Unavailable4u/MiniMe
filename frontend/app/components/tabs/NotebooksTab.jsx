"use client";
import { useEffect, useRef, useState } from "react";
import { useSession } from "../../context/SessionContext";
import IngestionDropzone from "../notebooks/IngestionDropzone";
import FlashcardFlipper from "../notebooks/FlashcardFlipper";
import QuizRunner from "../notebooks/QuizRunner";
import StudyGuideViewer from "../notebooks/StudyGuideViewer";
import KnowledgeGraphView from "../KnowledgeGraphView";
import MermaidDiagram from "../MermaidDiagram";
import ConfirmDialog from "../ConfirmDialog";           // NEW — §2/§3 fix: was already built, unused here
import ManageWorkspaceModal from "../ManageWorkspaceModal"; // NEW — §3 fix: was already built (rename/delete/members), unused here
import WorkspaceChatPanel from "../WorkspaceChatPanel";  // NEW — §6.2: embedded chat + WorkingPanel dock
import {
  NotebookText, Plus, MessageSquareText, FileText, GitBranch, Network,
  GraduationCap, Sparkles, X, Check, ChevronRight, BookMarked, Loader2, Layers,
  Trash2, MoreVertical, ArrowUpRight,
} from "lucide-react";

const SUB_TABS = [
  { id: "sources", label: "Sources", icon: FileText },
  { id: "mindmap", label: "Mind Map", icon: Network },
  { id: "backlinks", label: "Backlinks", icon: GitBranch },
  { id: "study", label: "Study", icon: GraduationCap },
  { id: "facts", label: "Facts", icon: BookMarked },
  { id: "clusters", label: "Clusters", icon: Layers },
  { id: "candidates", label: "Suggested notes", icon: Sparkles },
];

// NEW — §4 fix: persist which notebook and sub-tab were selected, same
// localStorage pattern AppShell.jsx uses for ACTIVE_TAB_KEY, so a page
// refresh doesn't drop you back to "no notebook selected."
const SELECTED_NOTEBOOK_KEY = "minime_notebooks_selected_id";
const SUB_TAB_KEY = "minime_notebooks_subtab";
// NEW — §6.2: separate collapse key from WorkspaceChatPanel's own internal
// WORKING_PANEL_KEY — this one folds away the *whole* dock (chat +
// WorkingPanel together), same "own toggle, own storage key" pattern the
// left ChatSidebar already uses for itself.
const CHAT_DOCK_KEY = "minime_notebooks_chatdock_collapsed";

function timeAgo(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleDateString(); } catch { return ""; }
}

// --- Sources sub-view ------------------------------------------------------

function SourcesView({ workspaceId, nodes, loading, onIngested, onSelectNode, onDeleteNode }) {
  const [pendingDelete, setPendingDelete] = useState(null); // NEW — §2 fix: node awaiting delete confirmation
  const [deleting, setDeleting] = useState(false);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      await onDeleteNode(pendingDelete.node_id);
      setPendingDelete(null);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-4">
      <IngestionDropzone workspaceId={workspaceId} onIngested={onIngested} />
      <div>
        <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">
          {loading ? "Loading…" : `${nodes.length} source${nodes.length === 1 ? "" : "s"}`}
        </div>
        <div className="space-y-1">
          {nodes.map((n) => (
            <div
              key={n.node_id}
              className="group w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-[var(--neutral-800)] hover:border-[var(--neutral-700)]"
            >
              <button
                onClick={() => onSelectNode(n)}
                className="flex-1 min-w-0 flex items-center justify-between gap-2 text-left"
              >
                <span className="text-xs text-[var(--neutral-200)] truncate">{n.title || n.node_id}</span>
                <span className="text-[10px] text-[var(--neutral-600)] shrink-0">{timeAgo(n.created_at)}</span>
              </button>
              <button
                onClick={() => setPendingDelete(n)}
                title="Delete source"
                className="shrink-0 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-red-400"
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
          {!loading && nodes.length === 0 && (
            <p className="text-xs text-[var(--neutral-600)]">No sources ingested yet — drop a file or paste a link above.</p>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete source"
        message={`Delete "${pendingDelete?.title || pendingDelete?.node_id}"? This also removes any links to it in Backlinks and Clusters.`}
        confirmLabel={deleting ? "Deleting…" : "Delete"}
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}

// --- Mind Map sub-view ------------------------------------------------------
// §4.7: extends MermaidDiagram.jsx (currently static, non-interactive
// SVG) with click handling that opens a scoped sub-chat. The mind map's
// own Mermaid source comes from the `mapper` role's chat output — pasted
// in here, then saved to eo/panel_content.py under panel_key "mindmap"
// so it survives a reload or a sub-tab switch instead of vanishing with
// local component state (the previous behavior).

function MindMapView({ workspaceId, onOpenSubChat, fetchPanelContent, savePanelContent }) {
  const [text, setText] = useState("");
  const [rendered, setRendered] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchPanelContent(workspaceId, "mindmap").then((saved) => {
      if (cancelled) return;
      const content = saved?.content || "";
      setText(content);
      setRendered(content);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchPanelContent]);

  async function handleRender() {
    setRendered(text);
    setSaving(true);
    try {
      await savePanelContent(workspaceId, "mindmap", text);
      setSavedAt(Date.now());
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>;
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-[var(--neutral-500)]">
        Paste Mermaid mind-map source from a <code className="text-amber-300">mapper</code> role's output, then click any node to open a sub-chat scoped to this notebook.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={"mindmap\n  root((Notebook))\n    Topic A\n    Topic B"}
        rows={5}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={handleRender}
          disabled={saving}
          className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {saving ? "Saving…" : "Render & Save"}
        </button>
        {savedAt && !saving && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
      </div>
      {rendered && (
        <div className="rounded-lg border border-[var(--neutral-800)] bg-black/30 p-4 overflow-auto">
          <MermaidDiagram
            mermaidText={rendered}
            onNodeClick={(label) => onOpenSubChat(workspaceId, `Tell me more about "${label}" using this notebook's sources.`)}
          />
        </div>
      )}
    </div>
  );
}

// --- Backlinks sub-view ------------------------------------------------------
// §4.7: reuses KnowledgeGraphView.jsx (Part 0/3) — third domain to use it,
// no new graph renderer.

function BacklinksView({ nodes, edges, loading, onDetect, onSelectNode }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-[var(--neutral-500)]">Bi-directional links between sources and notes in this notebook.</p>
        <button onClick={onDetect} className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]">
          Detect backlinks
        </button>
      </div>
      <div className="h-[420px] rounded-lg border border-[var(--neutral-800)] overflow-hidden">
        {loading ? (
          <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">Loading…</div>
        ) : nodes.length === 0 ? (
          <div className="h-full flex items-center justify-center text-xs text-[var(--neutral-600)]">Nothing to graph yet.</div>
        ) : (
          <KnowledgeGraphView nodes={nodes} edges={edges} onSelectNode={onSelectNode} />
        )}
      </div>
    </div>
  );
}

// --- Study sub-view ------------------------------------------------------
// §4.5/§4.7: flashcard flipper, quiz runner, study-guide viewer — plain
// generated Markdown pasted in from a chat run, same "paste the role's
// stage_output text" pattern the Mind Map view above already uses.

function StudyView({ workspaceId }) {
  const { synthesizePodcast, buildVideoOverview, fetchPanelContent, savePanelContent } = useSession();
  const [kind, setKind] = useState("flashcards");
  const [text, setText] = useState("");
  const [rendered, setRendered] = useState("");
  const [quizNodeId, setQuizNodeId] = useState("");
  // NEW — persistence for the three paste-and-Load kinds (flashcards,
  // quiz, study_guide) via eo/panel_content.py, panel_key
  // "study_<kind>". Podcast script / video slide text stay ephemeral —
  // those round-trip through the synthesis/build endpoints and produce
  // a durable audio/video file of their own, so the pasted source text
  // isn't the thing worth persisting there.
  const PERSISTED_KINDS = ["flashcards", "quiz", "study_guide"];
  const [loadingText, setLoadingText] = useState(true);
  const [savingText, setSavingText] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  useEffect(() => {
    if (!PERSISTED_KINDS.includes(kind)) { setLoadingText(false); return; }
    let cancelled = false;
    setLoadingText(true);
    setSavedAt(null);
    fetchPanelContent(workspaceId, `study_${kind}`).then((saved) => {
      if (cancelled) return;
      const content = saved?.content || "";
      setText(content);
      setRendered(content);
      setLoadingText(false);
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, kind]);

  async function handleLoad() {
    setRendered(text);
    if (!PERSISTED_KINDS.includes(kind)) return;
    setSavingText(true);
    try {
      await savePanelContent(workspaceId, `study_${kind}`, text);
      setSavedAt(Date.now());
    } finally {
      setSavingText(false);
    }
  }

  // NEW — Part 4 §4.4: podcast synthesis state. Kept separate from
  // `rendered` (the paste-and-Load flow above) since this kind doesn't
  // render the pasted text directly — it round-trips through the
  // synthesis endpoint first and renders an <audio> player from the
  // result instead.
  const [podcastTitle, setPodcastTitle] = useState("podcast");
  const [podcastAudioUrl, setPodcastAudioUrl] = useState("");
  const [synthesizing, setSynthesizing] = useState(false);
  const [synthesizeError, setSynthesizeError] = useState("");
  // Tracks which titles have been successfully synthesized in this
  // session — the video-overview build below needs a podcast already on
  // disk under a matching title (see build_video_overview_endpoint's
  // 404 message), so the UI surfaces that dependency instead of letting
  // the user hit the error blind.
  const [synthesizedTitles, setSynthesizedTitles] = useState(() => new Set());

  async function handleSynthesize() {
    setSynthesizing(true);
    setSynthesizeError("");
    setPodcastAudioUrl("");
    try {
      const url = await synthesizePodcast(text, podcastTitle);
      setPodcastAudioUrl(url);
      setSynthesizedTitles((prev) => new Set(prev).add(podcastTitle));
    } catch (err) {
      setSynthesizeError(String(err.message || err));
    } finally {
      setSynthesizing(false);
    }
  }

  // NEW — Part 4 §4.4: Video Overview state. Reuses podcastTitle above
  // (rather than a separate field) since the backend requires the two to
  // match exactly — it locates the already-synthesized mp3 on disk by
  // that title instead of re-synthesizing it.
  const [slideText, setSlideText] = useState("");
  const [videoTitle, setVideoTitle] = useState("video_overview");
  const [videoUrl, setVideoUrl] = useState("");
  const [buildingVideo, setBuildingVideo] = useState(false);
  const [videoError, setVideoError] = useState("");

  async function handleBuildVideo() {
    setBuildingVideo(true);
    setVideoError("");
    setVideoUrl("");
    try {
      const url = await buildVideoOverview(slideText, podcastTitle, videoTitle);
      setVideoUrl(url);
    } catch (err) {
      setVideoError(String(err.message || err));
    } finally {
      setBuildingVideo(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {["flashcards", "quiz", "study_guide", "podcast", "video_overview"].map((k) => (
          <button
            key={k}
            onClick={() => { setKind(k); setRendered(""); setText(""); }}
            className={`text-xs rounded-lg px-3 py-1 ${kind === k ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"}`}
          >
            {k === "flashcards" ? "Flashcards" : k === "quiz" ? "Quiz" : k === "study_guide" ? "Study guide" : k === "podcast" ? "Podcast" : "Video overview"}
          </button>
        ))}
      </div>
      {kind === "video_overview" ? (
        <p className="text-xs text-[var(--neutral-500)]">
          Paste the Markdown from a <code className="text-amber-300">slide_planner</code> chat run, then build a
          narrated slideshow using audio from a podcast you've already synthesized under the same title below.
        </p>
      ) : (
        <p className="text-xs text-[var(--neutral-500)]">
          Paste the Markdown from a <code className="text-amber-300">{kind === "flashcards" ? "flashcard_writer" : kind === "quiz" ? "quiz_writer" : kind === "study_guide" ? "study_guide_writer" : "podcast_scriptwriter"}</code> chat run.
        </p>
      )}
      {kind !== "video_overview" && loadingText && PERSISTED_KINDS.includes(kind) ? (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading saved text…</div>
      ) : kind !== "video_overview" && (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={5}
          placeholder={kind === "podcast" ? "HOST A: Welcome back to the show...\nHOST B: Today we're covering..." : undefined}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
        />
      )}
      {kind === "video_overview" && (
        <textarea
          value={slideText}
          onChange={(e) => setSlideText(e.target.value)}
          rows={5}
          placeholder={"# Title\n## Section heading\nSection body text..."}
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
        />
      )}
      {kind === "quiz" && (
        <input
          value={quizNodeId}
          onChange={(e) => setQuizNodeId(e.target.value)}
          placeholder="Quiz node_id (optional — enables progress tracking)"
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        />
      )}

      {kind === "podcast" ? (
        <div className="space-y-2">
          <input
            value={podcastTitle}
            onChange={(e) => setPodcastTitle(e.target.value)}
            placeholder="Title (used as the audio filename)"
            className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
          <button
            onClick={handleSynthesize}
            disabled={synthesizing || !text.trim()}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {synthesizing && <Loader2 size={12} className="animate-spin" />}
            {synthesizing ? "Synthesizing…" : "Synthesize"}
          </button>
          {synthesizeError && (
            <p className="text-xs text-red-400">{synthesizeError}</p>
          )}
          {podcastAudioUrl && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
              <audio controls src={podcastAudioUrl} className="w-full" />
              <a
                href={podcastAudioUrl}
                download={`${podcastTitle || "podcast"}.mp3`}
                className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
              >
                Download mp3
              </a>
            </div>
          )}
        </div>
      ) : kind === "video_overview" ? (
        <div className="space-y-2">
          <input
            value={podcastTitle}
            onChange={(e) => setPodcastTitle(e.target.value)}
            placeholder="Podcast title (must match an already-synthesized podcast)"
            className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
          {!synthesizedTitles.has(podcastTitle) && (
            <p className="text-[11px] text-amber-400">
              No podcast synthesized under this title yet this session. Switch to the Podcast tab and synthesize
              one with this exact title first, or the build below will fail.
            </p>
          )}
          <input
            value={videoTitle}
            onChange={(e) => setVideoTitle(e.target.value)}
            placeholder="Video title (used as the output filename)"
            className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
          <button
            onClick={handleBuildVideo}
            disabled={buildingVideo || !slideText.trim() || !podcastTitle.trim()}
            className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {buildingVideo && <Loader2 size={12} className="animate-spin" />}
            {buildingVideo ? "Building…" : "Build video overview"}
          </button>
          {videoError && (
            <p className="text-xs text-red-400">{videoError}</p>
          )}
          {videoUrl && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-3 space-y-2">
              <video controls src={videoUrl} className="w-full rounded" />
              <a
                href={videoUrl}
                download={`${videoTitle || "video_overview"}.mp4`}
                className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)]"
              >
                Download mp4
              </a>
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2">
            <button
              onClick={handleLoad}
              disabled={savingText}
              className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium disabled:opacity-50"
            >
              {savingText ? "Saving…" : "Load & Save"}
            </button>
            {savedAt && !savingText && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
          </div>

          {rendered && (
            <div className="rounded-lg border border-[var(--neutral-800)] p-4">
              {kind === "flashcards" && <FlashcardFlipper markdownText={rendered} />}
              {kind === "quiz" && <QuizRunner quizText={rendered} workspaceId={workspaceId} quizNodeId={quizNodeId || undefined} />}
              {kind === "study_guide" && <StudyGuideViewer markdownText={rendered} />}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// --- Candidates sub-view ------------------------------------------------------
// §4.6: the silent note-taking agent's proposals — never auto-committed.

function CandidatesView({ workspaceId, candidates, onAccept, onReject }) {
  if (candidates.length === 0) {
    return <p className="text-xs text-[var(--neutral-600)]">No suggested notes right now — the silent note-taker proposes one here when it spots a decision or action item in another chat in this notebook.</p>;
  }
  return (
    <div className="space-y-2">
      {candidates.map((c, i) => (
        <div key={i} className="rounded-lg border border-[var(--neutral-800)] p-3">
          <div className="text-xs font-medium text-[var(--neutral-200)]">{c.title}</div>
          <p className="text-xs text-[var(--neutral-400)] mt-1 whitespace-pre-wrap">{c.content}</p>
          <div className="flex items-center gap-2 mt-2">
            <button onClick={() => onAccept(i)} className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300">
              <Check size={12} /> Accept
            </button>
            <button onClick={() => onReject(i)} className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300">
              <X size={12} /> Discard
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

// --- Clusters sub-view ------------------------------------------------------
// agents/note_clusterer.py, §4.3 — deterministic KMeans over each node's
// existing embedding (no extra LLM/quota cost), proposed as accept/discard
// candidates — never auto-applied. Accepting a candidate links every member
// node to the cluster's first node with a "clustered_with" edge, same graph
// primitive the Backlinks tab's edges already use. Scan is explicit (like
// Backlinks' "Detect backlinks" button) rather than automatic, since it's
// a real recompute over every node in the notebook, not a passive fetch.

function ClustersView({ candidates, loading, scanning, onScan, onAccept, onReject }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-[var(--neutral-500)]">
          Suggested groupings of related sources, based on their existing embeddings — nothing is linked until you accept a group.
        </p>
        <button
          onClick={onScan}
          disabled={scanning}
          className="text-[11px] text-[var(--neutral-400)] hover:text-[var(--neutral-200)] disabled:opacity-50 shrink-0"
        >
          {scanning ? "Scanning…" : "Detect clusters"}
        </button>
      </div>

      {loading ? (
        <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading…</div>
      ) : candidates.length === 0 ? (
        <p className="text-xs text-[var(--neutral-600)]">No suggested clusters right now — click "Detect clusters" to scan (needs at least 4 sources with embeddings).</p>
      ) : (
        <div className="space-y-2">
          {candidates.map((c) => (
            <div key={c.candidate_id} className="rounded-lg border border-[var(--neutral-800)] p-3">
              <div className="text-xs font-medium text-[var(--neutral-200)]">{c.suggested_label}</div>
              <ul className="mt-1.5 space-y-0.5">
                {c.titles.map((t, i) => (
                  <li key={i} className="text-xs text-[var(--neutral-400)] truncate">· {t || c.node_ids[i]}</li>
                ))}
              </ul>
              <div className="flex items-center gap-2 mt-2">
                <button onClick={() => onAccept(c.candidate_id)} className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300">
                  <Check size={12} /> Accept
                </button>
                <button onClick={() => onReject(c.candidate_id)} className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300">
                  <X size={12} /> Discard
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Facts sub-view ------------------------------------------------------
// eo/workspace_facts.py, §0.3 — durable per-notebook facts (brand voice,
// target user, tech stack, plus a free-form `custom` bucket) that get
// folded into every agent prompt for this workspace automatically. This
// panel is the "settings-panel-facing surface" the module's docstring
// describes. Agent-proposed additions land in the candidates list below
// instead of overwriting the live facts directly — same accept/reject
// shape as the Suggested Notes tab, so an agent guess never silently
// clobbers something the user set on purpose.

// NEW — exported (not just used internally) so GrowthTab's `voice`
// sub-tab can import this directly instead of re-implementing fact
// editing a second time. Design doc §2.2: "Directly reuse NotebooksTab's
// FactsView component... eo/workspace_facts.py is already
// workspace-scoped, not domain-scoped, so a Growth-stage workspace
// calling the same fetchWorkspaceFacts/saveWorkspaceFacts functions
// NotebooksTab already uses gets brand voice for free." No behavior
// change here — same component, same props contract, just no longer
// module-private.
export function FactsView({ workspaceId, fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate }) {
  const [facts, setFacts] = useState({ brand_voice: "", target_user: "", tech_stack: [], custom: {} });
  const [techStackText, setTechStackText] = useState("");
  const [customEntries, setCustomEntries] = useState([]); // [{key, value}]
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);

  async function load() {
    setLoading(true);
    const [f, c] = await Promise.all([
      fetchWorkspaceFacts(workspaceId),
      fetchFactCandidates(workspaceId),
    ]);
    setFacts(f);
    setTechStackText((f.tech_stack || []).join(", "));
    setCustomEntries(Object.entries(f.custom || {}).map(([key, value]) => ({ key, value: String(value) })));
    setCandidates(c);
    setLoading(false);
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  async function handleSave() {
    setSaving(true);
    const custom = {};
    for (const { key, value } of customEntries) {
      if (key.trim()) custom[key.trim()] = value;
    }
    const tech_stack = techStackText.split(",").map((s) => s.trim()).filter(Boolean);
    const saved = await saveWorkspaceFacts(workspaceId, {
      brand_voice: facts.brand_voice || "",
      target_user: facts.target_user || "",
      tech_stack,
      custom,
    });
    setFacts(saved);
    setSaving(false);
    setSavedAt(Date.now());
  }

  if (loading) {
    return <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5"><Loader2 size={12} className="animate-spin" /> Loading facts…</div>;
  }

  return (
    <div className="space-y-6 max-w-lg">
      <div className="space-y-3">
        <p className="text-xs text-[var(--neutral-500)]">
          Durable facts about this notebook — folded into every agent prompt automatically, so you don't have to re-explain brand voice, audience, or stack in every chat.
        </p>
        <div>
          <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Brand voice</label>
          <textarea
            value={facts.brand_voice || ""}
            onChange={(e) => setFacts((f) => ({ ...f, brand_voice: e.target.value }))}
            rows={2}
            placeholder="e.g. warm, direct, no corporate jargon"
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
        </div>
        <div>
          <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Target user</label>
          <textarea
            value={facts.target_user || ""}
            onChange={(e) => setFacts((f) => ({ ...f, target_user: e.target.value }))}
            rows={2}
            placeholder="e.g. solo devs shipping side projects"
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
        </div>
        <div>
          <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Tech stack (comma-separated)</label>
          <input
            value={techStackText}
            onChange={(e) => setTechStackText(e.target.value)}
            placeholder="e.g. Next.js, FastAPI, Postgres"
            className="w-full mt-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
          />
        </div>
        <div>
          <div className="flex items-center justify-between">
            <label className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Custom facts</label>
            <button
              type="button"
              onClick={() => setCustomEntries((entries) => [...entries, { key: "", value: "" }])}
              className="text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            >
              + Add
            </button>
          </div>
          <div className="space-y-1.5 mt-1">
            {customEntries.map((entry, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <input
                  value={entry.key}
                  onChange={(e) => setCustomEntries((entries) => entries.map((en, j) => (j === i ? { ...en, key: e.target.value } : en)))}
                  placeholder="key"
                  className="w-28 shrink-0 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
                />
                <input
                  value={entry.value}
                  onChange={(e) => setCustomEntries((entries) => entries.map((en, j) => (j === i ? { ...en, value: e.target.value } : en)))}
                  placeholder="value"
                  className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
                />
                <button type="button" onClick={() => setCustomEntries((entries) => entries.filter((_, j) => j !== i))}>
                  <X size={12} className="text-[var(--neutral-600)] hover:text-red-400" />
                </button>
              </div>
            ))}
            {customEntries.length === 0 && (
              <p className="text-[11px] text-[var(--neutral-700)]">No custom facts yet — e.g. deploy_target, repo_url, anything domain-specific.</p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleSave}
            disabled={saving}
            className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save facts"}
          </button>
          {savedAt && !saving && <span className="text-[11px] text-[var(--neutral-600)]">Saved</span>}
        </div>
      </div>

      <div>
        <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2 flex items-center gap-1.5">
          Agent-suggested facts
          {candidates.length > 0 && (
            <span className="text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">{candidates.length}</span>
          )}
        </div>
        {candidates.length === 0 ? (
          <p className="text-xs text-[var(--neutral-600)]">Nothing pending — agents propose a fact here when they spot something durable worth remembering, without overwriting what's above.</p>
        ) : (
          <div className="space-y-2">
            {candidates.map((c, i) => (
              <div key={i} className="rounded-lg border border-[var(--neutral-800)] p-3">
                <div className="text-xs font-medium text-[var(--neutral-200)]">{c.key}</div>
                <p className="text-xs text-[var(--neutral-400)] mt-1 whitespace-pre-wrap">{String(c.value)}</p>
                {c.proposed_by && <p className="text-[10px] text-[var(--neutral-700)] mt-1">proposed by {c.proposed_by}</p>}
                <div className="flex items-center gap-2 mt-2">
                  <button
                    onClick={async () => { await acceptFactCandidate(workspaceId, i); await load(); }}
                    className="flex items-center gap-1 text-[11px] text-green-400 hover:text-green-300"
                  >
                    <Check size={12} /> Accept
                  </button>
                  <button
                    onClick={async () => { await rejectFactCandidate(workspaceId, i); await load(); }}
                    className="flex items-center gap-1 text-[11px] text-red-400 hover:text-red-300"
                  >
                    <X size={12} /> Discard
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// --- Node preview modal ------------------------------------------------------

function NodePreviewModal({ node, onClose }) {
  if (!node) return null;
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] rounded-lg p-4 w-[32rem] max-h-[70vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-medium text-[var(--neutral-200)]">{node.title || node.node_id}</h3>
          <button onClick={onClose}><X size={14} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" /></button>
        </div>
        <p className="text-[10px] text-[var(--neutral-600)] mb-2">{node.node_type} · {timeAgo(node.created_at)}</p>
        <p className="text-xs text-[var(--neutral-300)] whitespace-pre-wrap">{node.content}</p>
      </div>
    </div>
  );
}

// --- Main tab ------------------------------------------------------

export default function NotebooksTab({ onPromoted }) {
   const {
     workspaces, fetchWorkspaces, createWorkspace, chats, promoteWorkspace,
     fetchWorkspaceNodes, deleteWorkspaceNode, fetchGraphEdges, detectBacklinks,
     fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate,
     fetchWorkspaceFacts, saveWorkspaceFacts, fetchFactCandidates, acceptFactCandidate, rejectFactCandidate,
     fetchPanelContent, savePanelContent,
     proposeClusters, fetchClusterCandidates, acceptClusterCandidate, rejectClusterCandidate,
    openScopedSubChat, createNewChat, addWorkspaceChat, switchChat,
  } = useSession();

  // NEW — §8: Notebooks only ever shows note-stage workspaces now — once
  // promoted, a workspace moves to the Research tab instead of appearing
  // in both places.
  const notebooks = workspaces.filter((w) => w.stage === "note");

  const [selectedId, setSelectedId] = useState(null);
  const [subTab, setSubTab] = useState("sources");
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [clusterCandidates, setClusterCandidates] = useState([]);
  const [loadingClusters, setLoadingClusters] = useState(false);
  const [scanningClusters, setScanningClusters] = useState(false);
  const [loadingNodes, setLoadingNodes] = useState(false);
  const [previewNode, setPreviewNode] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  // NEW — §3 fix: which notebook's kebab menu (rename/delete/members) is
  // open. ManageWorkspaceModal already existed fully built, just never
  // wired into any tab's UI.
  const [managingWorkspace, setManagingWorkspace] = useState(null);
  // NEW — §8: promote-to-Research busy/error state for the button next
  // to "Open chat".
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  // NEW — §6.2: right-hand chat dock collapse state, restored from
  // localStorage on mount (same pattern as sidebarCollapsed elsewhere).
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);

  useEffect(() => {
    setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }
  // NEW — §4 fix: guards the auto-select effect below until we've had a
  // chance to read a saved selection from localStorage, so it doesn't
  // jump to workspaces[0] before the restore runs.
  const [restoredSelection, setRestoredSelection] = useState(false);

  // NEW — §4 fix: restore the last-selected notebook and sub-tab on
  // mount.
  useEffect(() => {
    const savedId = localStorage.getItem(SELECTED_NOTEBOOK_KEY);
    const savedSubTab = localStorage.getItem(SUB_TAB_KEY);
    if (savedId) setSelectedId(savedId);
    if (savedSubTab && SUB_TABS.some((t) => t.id === savedSubTab)) setSubTab(savedSubTab);
    setRestoredSelection(true);
  }, []);

  // NEW — §4 fix: persist selection changes. Guarded on restoredSelection
  // so the initial (pre-restore) null/"sources" values don't overwrite
  // what's already saved before the restore effect above has run.
  useEffect(() => {
    if (!restoredSelection || !selectedId) return;
    localStorage.setItem(SELECTED_NOTEBOOK_KEY, selectedId);
  }, [selectedId, restoredSelection]);

  useEffect(() => {
    if (!restoredSelection) return;
    localStorage.setItem(SUB_TAB_KEY, subTab);
  }, [subTab, restoredSelection]);

  useEffect(() => {
    // Falls back to the first workspace once workspaces have loaded, but
    // only after the restore effect above has had a chance to set
    // selectedId from localStorage — and also recovers if a previously
    // saved id no longer exists (e.g. that notebook was deleted).
    if (!restoredSelection || notebooks.length === 0) return;
    const stillExists = selectedId && notebooks.some((w) => w.id === selectedId);
    if (!stillExists) setSelectedId(notebooks[0].id);
  }, [notebooks, selectedId, restoredSelection]);

  async function loadNotebookData(wsId) {
    setLoadingNodes(true);
    setLoadingClusters(true);
    const [nodeList, edgeList, candidateList, clusterCandidateList] = await Promise.all([
      fetchWorkspaceNodes(wsId),
      fetchGraphEdges(wsId),
      fetchNoteCandidates(wsId),
      fetchClusterCandidates(wsId),
    ]);
    // FIX — if the user has since selected a different notebook while
    // this fetch was in flight, this result is stale: drop it instead of
    // overwriting what's currently on screen. (Loading flags only get
    // cleared by whichever call actually IS still relevant.)
    if (selectedIdRef.current !== wsId) return;
    setNodes(nodeList);
    setEdges(edgeList);
    setCandidates(candidateList);
    setClusterCandidates(clusterCandidateList);
    setLoadingNodes(false);
    setLoadingClusters(false);
  }

  // FIX — stale-response guard: `loadNotebookData` is async and can be
  // in flight when the user switches notebooks (e.g. via a slow upload's
  // onIngested callback firing after selectedId has already moved on —
  // see IngestionDropzone). Without this ref, whichever fetch resolves
  // last wins and can silently overwrite the currently-viewed notebook's
  // nodes/edges with a different notebook's data. This ref always holds
  // the *current* selection so loadNotebookData can check "is my result
  // still relevant?" right before committing state.
  const selectedIdRef = useRef(selectedId);
  useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);

  useEffect(() => {
    if (selectedId) loadNotebookData(selectedId);
    else { setNodes([]); setEdges([]); setCandidates([]); setClusterCandidates([]); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  async function handleScanClusters() {
    setScanningClusters(true);
    try {
      setClusterCandidates(await proposeClusters(selected.id));
    } finally {
      setScanningClusters(false);
    }
  }

  async function handleAcceptCluster(candidateId) {
    await acceptClusterCandidate(selected.id, candidateId);
    setClusterCandidates(await fetchClusterCandidates(selected.id));
  }

  async function handleRejectCluster(candidateId) {
    await rejectClusterCandidate(selected.id, candidateId);
    setClusterCandidates(await fetchClusterCandidates(selected.id));
  }

  async function handleCreateNotebook(e) {
    e.preventDefault();
    if (!newName.trim()) return;
    await createWorkspace(newName.trim());
    setNewName("");
    setCreating(false);
    await fetchWorkspaces();
  }
  // NEW — switches the active chat locally and makes sure the dock (or,
  // below `lg`, the full-screen overlay) is showing it — no tab jump,
  // this tab is self-contained regardless of viewport width.
  async function openInDock(chatId) {
    await switchChat(chatId);
    if (chatDockCollapsed) toggleChatDock();
  }

  async function handleOpenChat(wsId) {
    const ws = workspaces.find((w) => w.id === wsId);
    const chatId = ws?.chat_ids?.[0] || null;
    if (chatId) {
      await openInDock(chatId);
    } else {
      const newChatId = await createNewChat();
      await addWorkspaceChat(wsId, newChatId);
      await openInDock(newChatId);
    }
  }

  async function handleOpenSubChat(wsId, prompt) {
    const chatId = await openScopedSubChat(wsId, prompt);
    await openInDock(chatId);
  }

  // NEW — §8: promotes the notebook to Research and hands off navigation
  // to AppShell, which switches tabs and pre-selects it there.
  async function handlePromote(wsId) {
    setPromoting(true);
    setPromoteError(null);
    try {
      await promoteWorkspace(wsId, "research");
      onPromoted?.("research", wsId);
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromoting(false);
    }
  }

  const selected = notebooks.find((w) => w.id === selectedId);
  const ActiveIcon = SUB_TABS.find((t) => t.id === subTab)?.icon || FileText;

  return (
    <div className="flex h-full">
      {/* Notebook picker — this tab's own left column, distinct from the
          chat sidebar (which is hidden while this tab is active). */}
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col h-full">
        <div className="flex items-center justify-between px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <NotebookText size={13} /> Notebooks
          </span>
          <button onClick={() => setCreating((c) => !c)} title="New notebook" className="text-[var(--neutral-400)] hover:text-[var(--neutral-100)]">
            <Plus size={15} />
          </button>
        </div>
        {creating && (
          <form onSubmit={handleCreateNotebook} className="px-3 py-2 border-b border-[var(--neutral-900)] flex gap-1">
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Notebook name"
              className="flex-1 bg-black/30 border border-[var(--neutral-800)] rounded px-1.5 py-1 text-xs outline-none focus:border-[var(--cyber-cyan)]"
            />
            <button type="submit"><Check size={13} className="text-green-400" /></button>
          </form>
        )}
        <div className="flex-1 overflow-y-auto">
          {notebooks.map((ws) => (
            <div
              key={ws.id}
              className={`group flex items-center gap-1 border-b border-[var(--neutral-900)] ${
                ws.id === selectedId ? "bg-[var(--neutral-800-a70)]" : "hover:bg-[var(--neutral-900)]"
              }`}
            >
              <button
                onClick={() => setSelectedId(ws.id)}
                className="flex-1 min-w-0 flex items-center justify-between gap-1 px-3 py-2 text-left"
              >
                <span className="text-xs text-[var(--neutral-200)] truncate">{ws.name}</span>
                {ws.id === selectedId && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
              </button>
              <button
                onClick={() => setManagingWorkspace(ws)}
                title="Rename or delete notebook"
                className="shrink-0 pr-2 text-[var(--neutral-600)] opacity-0 group-hover:opacity-100 hover:text-[var(--neutral-200)]"
              >
                <MoreVertical size={13} />
              </button>
            </div>
          ))}
          {notebooks.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">No notebooks yet — create one to start ingesting sources.</p>
          )}
        </div>
      </div>

      {/* Selected notebook */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!selected ? (
          <div className="h-full flex items-center justify-center text-sm text-[var(--neutral-600)]">
            Select or create a notebook to get started.
          </div>
        ) : (
          <div className="p-5 space-y-4 max-w-3xl">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-medium text-[var(--neutral-100)]">{selected.name}</h2>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleOpenChat(selected.id)}
                  className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium"
                >
                  <MessageSquareText size={13} /> Open chat
                </button>
                <button
                  onClick={() => handlePromote(selected.id)}
                  disabled={promoting}
                  className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
                >
                  {promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
                  Promote to Research →
                </button>
              </div>
            </div>
            {promoteError && <p className="text-xs text-red-400">{promoteError}</p>}

            <nav className="flex gap-1 border-b border-[var(--neutral-800)] pb-2">
              {SUB_TABS.map((t) => (
                <button
                  key={t.id}
                  onClick={() => setSubTab(t.id)}
                  className={`flex items-center gap-1.5 text-xs rounded-lg px-3 py-1.5 ${
                    subTab === t.id ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                  }`}
                >
                  <t.icon size={13} /> {t.label}
                  {t.id === "candidates" && candidates.length > 0 && (
                    <span className="ml-0.5 text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">{candidates.length}</span>
                  )}
                  {t.id === "clusters" && clusterCandidates.length > 0 && (
                    <span className="ml-0.5 text-[10px] bg-amber-500/20 text-amber-300 rounded-full px-1.5">{clusterCandidates.length}</span>
                  )}
                </button>
              ))}
            </nav>

            {subTab === "sources" && (
              <SourcesView
                workspaceId={selected.id}
                nodes={nodes}
                loading={loadingNodes}
                onIngested={() => loadNotebookData(selected.id)}
                onSelectNode={setPreviewNode}
                onDeleteNode={async (nodeId) => {
                  await deleteWorkspaceNode(selected.id, nodeId);
                  await loadNotebookData(selected.id);
                }}
              />
            )}
            {subTab === "mindmap" && (
              <MindMapView
                workspaceId={selected.id}
                onOpenSubChat={handleOpenSubChat}
                fetchPanelContent={fetchPanelContent}
                savePanelContent={savePanelContent}
              />
            )}
            {subTab === "backlinks" && (
              <BacklinksView
                nodes={nodes}
                edges={edges}
                loading={loadingNodes}
                onDetect={async () => { await detectBacklinks(selected.id); await loadNotebookData(selected.id); }}
                onSelectNode={setPreviewNode}
              />
            )}
            {subTab === "study" && <StudyView workspaceId={selected.id} />}
            {subTab === "facts" && (
              <FactsView
                workspaceId={selected.id}
                fetchWorkspaceFacts={fetchWorkspaceFacts}
                saveWorkspaceFacts={saveWorkspaceFacts}
                fetchFactCandidates={fetchFactCandidates}
                acceptFactCandidate={acceptFactCandidate}
                rejectFactCandidate={rejectFactCandidate}
              />
            )}
            {subTab === "clusters" && (
              <ClustersView
                candidates={clusterCandidates}
                loading={loadingClusters}
                scanning={scanningClusters}
                onScan={handleScanClusters}
                onAccept={handleAcceptCluster}
                onReject={handleRejectCluster}
              />
            )}
            {subTab === "candidates" && (
              <CandidatesView
                workspaceId={selected.id}
                candidates={candidates}
                onAccept={async (i) => { await acceptNoteCandidate(selected.id, i); await loadNotebookData(selected.id); }}
                onReject={async (i) => { await rejectNoteCandidate(selected.id, i); await loadNotebookData(selected.id); }}
              />
            )}
          </div>
        )}
      </div>

      {/* Desktop dock — side-by-side, lg+. */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} />
      </div>

      {/* Below lg — full-screen overlay instead of a side dock, so this
          tab never depends on the standalone Chat tab, at any width. */}
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} />
        </div>
      )}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          className="lg:hidden fixed bottom-4 right-4 z-40 bg-[var(--accent)] text-[var(--accent-text)] rounded-full p-3 shadow-lg"
        >
          <MessageSquareText size={18} />
        </button>
      )}

      <NodePreviewModal node={previewNode} onClose={() => setPreviewNode(null)} />
      {managingWorkspace && (
        <ManageWorkspaceModal
          workspace={managingWorkspace}
          allChats={chats}
          onClose={() => setManagingWorkspace(null)}
        />
      )}
    </div>
  );
}
