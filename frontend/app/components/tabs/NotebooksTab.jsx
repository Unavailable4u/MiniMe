"use client";
import { useEffect, useState } from "react";
import { useSession } from "../../context/SessionContext";
import IngestionDropzone from "../notebooks/IngestionDropzone";
import FlashcardFlipper from "../notebooks/FlashcardFlipper";
import QuizRunner from "../notebooks/QuizRunner";
import StudyGuideViewer from "../notebooks/StudyGuideViewer";
import KnowledgeGraphView from "../KnowledgeGraphView";
import MermaidDiagram from "../MermaidDiagram";
import {
  NotebookText, Plus, MessageSquareText, FileText, GitBranch, Network,
  GraduationCap, Sparkles, X, Check, ChevronRight,
} from "lucide-react";

const SUB_TABS = [
  { id: "sources", label: "Sources", icon: FileText },
  { id: "mindmap", label: "Mind Map", icon: Network },
  { id: "backlinks", label: "Backlinks", icon: GitBranch },
  { id: "study", label: "Study", icon: GraduationCap },
  { id: "candidates", label: "Suggested notes", icon: Sparkles },
];

function timeAgo(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleDateString(); } catch { return ""; }
}

// --- Sources sub-view ------------------------------------------------------

function SourcesView({ workspaceId, nodes, loading, onIngested, onSelectNode }) {
  return (
    <div className="space-y-4">
      <IngestionDropzone workspaceId={workspaceId} onIngested={onIngested} />
      <div>
        <div className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)] mb-2">
          {loading ? "Loading…" : `${nodes.length} source${nodes.length === 1 ? "" : "s"}`}
        </div>
        <div className="space-y-1">
          {nodes.map((n) => (
            <button
              key={n.node_id}
              onClick={() => onSelectNode(n)}
              className="w-full flex items-center justify-between gap-2 px-3 py-2 rounded-lg border border-[var(--neutral-800)] hover:border-[var(--neutral-700)] text-left"
            >
              <span className="text-xs text-[var(--neutral-200)] truncate">{n.title || n.node_id}</span>
              <span className="text-[10px] text-[var(--neutral-600)] shrink-0">{timeAgo(n.created_at)}</span>
            </button>
          ))}
          {!loading && nodes.length === 0 && (
            <p className="text-xs text-[var(--neutral-600)]">No sources ingested yet — drop a file or paste a link above.</p>
          )}
        </div>
      </div>
    </div>
  );
}

// --- Mind Map sub-view ------------------------------------------------------
// §4.7: extends MermaidDiagram.jsx (currently static, non-interactive
// SVG) with click handling that opens a scoped sub-chat. The mind map's
// own Mermaid source comes from the `mapper` role's chat output — pasted
// in here rather than re-fetched, since no dedicated "latest mind map"
// store exists yet (same reasoning the domain doc gives for Video
// Overview's audio lookup-by-title being a deliberate simplification).

function MindMapView({ workspaceId, onOpenSubChat }) {
  const [text, setText] = useState("");
  const [rendered, setRendered] = useState("");

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
      <button
        onClick={() => setRendered(text)}
        className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium"
      >
        Render
      </button>
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
  const [kind, setKind] = useState("flashcards");
  const [text, setText] = useState("");
  const [rendered, setRendered] = useState("");
  const [quizNodeId, setQuizNodeId] = useState("");

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {["flashcards", "quiz", "study_guide"].map((k) => (
          <button
            key={k}
            onClick={() => { setKind(k); setRendered(""); }}
            className={`text-xs rounded-lg px-3 py-1 ${kind === k ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium" : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"}`}
          >
            {k === "flashcards" ? "Flashcards" : k === "quiz" ? "Quiz" : "Study guide"}
          </button>
        ))}
      </div>
      <p className="text-xs text-[var(--neutral-500)]">
        Paste the Markdown from a <code className="text-amber-300">{kind === "flashcards" ? "flashcard_writer" : kind === "quiz" ? "quiz_writer" : "study_guide_writer"}</code> chat run.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={5}
        className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs font-mono outline-none focus:border-[var(--cyber-cyan)]"
      />
      {kind === "quiz" && (
        <input
          value={quizNodeId}
          onChange={(e) => setQuizNodeId(e.target.value)}
          placeholder="Quiz node_id (optional — enables progress tracking)"
          className="w-full bg-black/30 border border-[var(--neutral-800)] rounded px-2 py-1.5 text-xs outline-none focus:border-[var(--cyber-cyan)]"
        />
      )}
      <button
        onClick={() => setRendered(text)}
        className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 py-1.5 font-medium"
      >
        Load
      </button>

      {rendered && (
        <div className="rounded-lg border border-[var(--neutral-800)] p-4">
          {kind === "flashcards" && <FlashcardFlipper markdownText={rendered} />}
          {kind === "quiz" && <QuizRunner quizText={rendered} workspaceId={workspaceId} quizNodeId={quizNodeId || undefined} />}
          {kind === "study_guide" && <StudyGuideViewer markdownText={rendered} />}
        </div>
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

export default function NotebooksTab({ onOpenChat }) {
  const {
    workspaces, fetchWorkspaces, createWorkspace,
    fetchWorkspaceNodes, fetchGraphEdges, detectBacklinks,
    fetchNoteCandidates, acceptNoteCandidate, rejectNoteCandidate,
    openScopedSubChat, createNewChat, addWorkspaceChat,
  } = useSession();

  const [selectedId, setSelectedId] = useState(null);
  const [subTab, setSubTab] = useState("sources");
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [loadingNodes, setLoadingNodes] = useState(false);
  const [previewNode, setPreviewNode] = useState(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");

  useEffect(() => {
    if (!selectedId && workspaces.length > 0) setSelectedId(workspaces[0].id);
  }, [workspaces, selectedId]);

  async function loadNotebookData(wsId) {
    setLoadingNodes(true);
    const [nodeList, edgeList, candidateList] = await Promise.all([
      fetchWorkspaceNodes(wsId),
      fetchGraphEdges(wsId),
      fetchNoteCandidates(wsId),
    ]);
    setNodes(nodeList);
    setEdges(edgeList);
    setCandidates(candidateList);
    setLoadingNodes(false);
  }

  useEffect(() => {
    if (selectedId) loadNotebookData(selectedId);
    else { setNodes([]); setEdges([]); setCandidates([]); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  async function handleCreateNotebook(e) {
    e.preventDefault();
    if (!newName.trim()) return;
    await createWorkspace(newName.trim());
    setNewName("");
    setCreating(false);
    await fetchWorkspaces();
  }

  async function handleOpenChat(wsId) {
    const ws = workspaces.find((w) => w.id === wsId);
    const chatId = ws?.chat_ids?.[0] || null;
    if (chatId) {
      await onOpenChat(chatId);
    } else {
      const newChatId = await createNewChat();
      await addWorkspaceChat(wsId, newChatId);
      await onOpenChat(newChatId);
    }
  }

  async function handleOpenSubChat(wsId, prompt) {
    const chatId = await openScopedSubChat(wsId, prompt);
    await onOpenChat(chatId);
  }

  const selected = workspaces.find((w) => w.id === selectedId);
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
          {workspaces.map((ws) => (
            <button
              key={ws.id}
              onClick={() => setSelectedId(ws.id)}
              className={`w-full flex items-center justify-between gap-1 px-3 py-2 border-b border-[var(--neutral-900)] text-left ${
                ws.id === selectedId ? "bg-[var(--neutral-800-a70)]" : "hover:bg-[var(--neutral-900)]"
              }`}
            >
              <span className="text-xs text-[var(--neutral-200)] truncate">{ws.name}</span>
              {ws.id === selectedId && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
            </button>
          ))}
          {workspaces.length === 0 && (
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
              <button
                onClick={() => handleOpenChat(selected.id)}
                className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium"
              >
                <MessageSquareText size={13} /> Open chat
              </button>
            </div>

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
              />
            )}
            {subTab === "mindmap" && (
              <MindMapView workspaceId={selected.id} onOpenSubChat={handleOpenSubChat} />
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

      <NodePreviewModal node={previewNode} onClose={() => setPreviewNode(null)} />
    </div>
  );
}
