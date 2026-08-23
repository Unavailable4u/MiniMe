"use client";
import { useEffect, useState, memo } from "react";
import { useSession, authHeaders } from "../../context/SessionContext";
import { useWorkspaces } from "../../context/WorkspacesContext";   // NEW — Item 2 concern split, slice 3
import { useChatList } from "../../context/ChatListContext";   // NEW — Item 2 concern split, slice 4
import WorkspaceChatPanel from "../WorkspaceChatPanel";
import CreateWorkspaceModal from "../CreateWorkspaceModal"; // NEW — item #10 / B3: native "create project" for this tab, same as ResearchTab's B2
import ConfirmDialog from "../ConfirmDialog"; // NEW — issue #3: same delete-confirmation affordance as ChatSidebar's own per-chat delete
import { useWorkspaceDockActions, useLastActiveChatId } from "../../context/WorkspaceDockContext"; // NEW — item #11 / C2: nested chat list, same as ResearchTab/PlanTab's C1
import { Loader2, ArrowUpRight, ChevronRight, ChevronLeft, ChevronDown, MessageSquare, Plus, Pencil, Check, X, Trash2, RefreshCw, Save, Folder, FolderOpen, FileCode, Download } from "lucide-react"; // CHANGED — patch 10: added ChevronDown/RefreshCw/Save/Folder/FolderOpen/FileCode for the Code sub-tab's file tree + editor. CHANGED — patch 11: added Download for the ZIP button.
import WorkspaceStageIcons, { STAGE_THEME } from "../WorkspaceStageIcons"; // NEW — item #2: colored per-stage icon + per-project stage badges
import InstructionChecklist from "../InstructionChecklist"; // NEW — patch 7 (T2/T3 Plan/Build split): relocated from PlanTab.jsx's Blueprint sub-tab. Same component, same backend read/write path (workspace_facts.custom["instructions"], GET .../device-spec, PATCH .../instructions/steps/{step_id}) -- only the tab it renders in changed.
// Part 8.9: replaces the old static shared-secret x-api-key header
// -- every fetch() below now sends the real per-user Supabase JWT via
// authHeaders(), matching require_auth()'s Authorization: Bearer check.

// §7 fix: Tasks is now scoped to a build-stage workspace instead of
// whatever chat happens to be open. Same left-hand picker pattern as
// NotebooksTab.jsx (localStorage-persisted selection, auto-select first
// item once loaded), filtered to workspace.stage === "build" instead of
// "note".
//
// UPDATED — step 16 (T3) audit: this comment used to warn that
// eo/chat_workspace.py's list_workspaces()/get_workspace() didn't
// SELECT/return w.stage, which would make every workspace read as stage
// "note" and leave this filter showing nothing. Re-checked against the
// current backend: both functions' queries already select w.stage, and
// _row_to_workspace() already returns it (`"stage": row.get("stage",
// "note")`) — confirmed by reading eo/chat_workspace.py directly, plus
// WorkspacesContext.jsx's fetchWorkspaces(), which passes the backend's
// JSON straight through with nothing stripped. That bug is already
// fixed; this comment was just never updated to say so, which risked
// someone re-diagnosing a problem that no longer exists. Left as a
// dated note rather than deleted outright, so anyone reading this file's
// history understands why the filter below was once expected to be broken.
const SELECTED_BUILD_WS_KEY = "minime_tasks_selected_ws_id";
const CHAT_DOCK_KEY = "minime_build_chatdock_collapsed";
// NEW — collapsible project-picker sidebar, same pattern as the chat
// dock's own collapse above.
const PROJECTS_KEY = "minime_build_projects_collapsed";
const PROMOTE_TARGETS = ["test", "growth"];
const PROMOTE_LABELS = {
  test: "Test",
  growth: "Growth",
};

// feature_status's own value vocabulary (see agents/idea_planner.py's
// SYSTEM_PROMPT) -- "done" | "in_progress" | missing. No new taxonomy
// invented here; "missing" is just the absence of a feature_status entry
// for a feature that current_plan["features"] lists.
const COLUMNS = [
  { status: "missing", label: "Missing" },
  { status: "in_progress", label: "In Progress" },
  { status: "done", label: "Done" },
];

// NEW — patch 7 (T2/T3 Plan/Build split): Build now has two sub-views for
// a selected project instead of just the kanban board. Same small
// nested-tab-bar pattern PlanTab.jsx's own BLUEPRINT_VIEWS uses.
// UPDATED — patch 10: third sub-view, Code, added alongside Tasks/
// Instructions. Same nav bar, no new pattern.
const BUILD_VIEWS = [
  { id: "tasks", label: "Tasks" },
  { id: "instructions", label: "Instructions" },
  { id: "code", label: "Code" },
];

function statusFor(featureStatus, featureName) {
  return featureStatus[featureName] || "missing";
}

function Card({ title, action, children }) {
  return (
    <div className="cyber-panel p-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-display text-[11px] uppercase tracking-wide text-cyber-dim">{title}</h3>
        {action}
      </div>
      {children}
    </div>
  );
}

// module_specs (agents/prompt_writer.py) is {"modules": [{name, description,
// inputs, outputs, edge_cases}, ...]} for whichever ONE feature
// current_plan["target_feature"] names this cycle -- it is not keyed by
// feature name, and there is no per-feature history for features that
// haven't been the target yet. So only the target feature card can
// expand to something real; every other card is a plain label.
function FeatureCard({ name, isTarget, targetModules, expanded, onToggle }) {
  const canExpand = isTarget;
  return (
    <div className="cyber-panel p-3 space-y-2">
      <button
        type="button"
        onClick={canExpand ? onToggle : undefined}
        disabled={!canExpand}
        className={`w-full text-left flex items-start justify-between gap-2 ${canExpand ? "" : "cursor-default"}`}
      >
        <span className="text-xs text-cyber-text">{name}</span>
        {isTarget && (
          <span className="shrink-0 font-display text-[9px] uppercase tracking-wide text-cyber-cyan border border-cyber-cyan/40 rounded px-1.5 py-0.5">
            this cycle
          </span>
        )}
      </button>
      {expanded && canExpand && (
        <div className="pt-2 border-t border-cyber-border text-[11px] text-cyber-dim space-y-2">
          {(!targetModules || targetModules.length === 0) && (
            <p>No module_specs recorded for this cycle yet.</p>
          )}
          {targetModules?.map((m, i) => (
            <div key={m.name || i} className="space-y-0.5">
              <p className="text-cyber-text font-mono">{m.name}</p>
              {m.description && <p>{m.description}</p>}
              {m.inputs && <p><span className="text-cyber-dim/70">in:</span> {String(m.inputs)}</p>}
              {m.outputs && <p><span className="text-cyber-dim/70">out:</span> {String(m.outputs)}</p>}
              {Array.isArray(m.edge_cases) && m.edge_cases.length > 0 && (
                <p><span className="text-cyber-dim/70">edge cases:</span> {m.edge_cases.join(", ")}</p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Part 7 §7.3 -- fixed six-category vocabulary, matching
// eo/registry.py's integration_flagger seed brief exactly (no new
// taxonomy invented on the frontend either).
const INTEGRATION_LABELS = {
  auth: "Auth",
  payments: "Payments",
  email_notifications: "Email / Notifications",
  analytics: "Analytics",
  file_storage: "File Storage",
  monitoring: "Monitoring",
};

function IntegrationChecklist({ integrations }) {
  if (!integrations || integrations.length === 0) {
    return (
      <Card title="Integrations flagged">
        <p className="text-[11px] text-cyber-dim">
          None flagged yet -- integration_flagger runs once, early in the
          cycle, and its result is cached for the rest of this session.
        </p>
      </Card>
    );
  }
  return (
    <Card title={`Integrations flagged (${integrations.length})`}>
      <ul className="space-y-1.5">
        {integrations.map((item, i) => (
          <li key={`${item.type}-${i}`} className="text-[11px] flex items-start gap-2">
            <span className="shrink-0 font-display uppercase tracking-wide text-[10px] text-cyber-cyan border border-cyber-cyan/40 rounded px-1.5 py-0.5">
              {INTEGRATION_LABELS[item.type] || item.type}
            </span>
            <span className="text-cyber-dim">{item.evidence}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// NEW — patch 7 (T2/T3 Plan/Build split): InstructionChecklist relocated
// here from PlanTab.jsx's BlueprintView. Same fetch/toggle plumbing that
// lived in BlueprintView -- fetchDeviceSpec/toggleInstructionStep are
// global SessionContext functions, not Plan-tab-owned, so this is a
// straight copy of that logic, scoped to instructions only (Parts/
// Wiring/Mech stay in Plan as design specs, see PlanTab.jsx's own
// comment on BLUEPRINT_VIEWS).
function InstructionsView({ workspaceId, fetchDeviceSpec, toggleInstructionStep }) {
  const [spec, setSpec] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetchDeviceSpec(workspaceId).then((data) => {
      if (cancelled) return;
      setSpec(data);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [workspaceId, fetchDeviceSpec]);

  async function handleToggleStep(phaseId, stepId, done) {
    const result = await toggleInstructionStep(workspaceId, stepId, done);
    // toggle endpoint returns the full updated `instructions` object
    // (api/server.py's toggle_instruction_step) -- swap it in directly
    // rather than re-fetching the whole device spec for a one-step change.
    if (result?.instructions) {
      setSpec((prev) => ({ ...prev, instructions: result.instructions }));
    }
  }

  if (loading) {
    return (
      <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5">
        <Loader2 size={12} className="animate-spin" /> Loading…
      </div>
    );
  }

  const hasPhases = spec?.instructions?.phases?.length > 0;

  if (!hasPhases) {
    return (
      <p className="text-xs text-[var(--neutral-600)]">
        No build instructions generated yet — run hardware_speccer from this project&apos;s chat once a PRD exists.
      </p>
    );
  }

  return <InstructionChecklist phases={spec.instructions.phases} onToggleStep={handleToggleStep} />;
}

// NEW — patch 10 (T3, step 16): Code sub-tab frontend. File-tree view +
// click-to-open + inline edit, wired to patch 8's GET/PUT
// .../code/files endpoints (api/routes/code.py). list_files() returns
// metadata only, keyed by flat file_path -- workspace_code_files.py's
// own docstring calls this the "flat map of paths, no separate
// directory rows" shape and says patch 10 should build its tree
// client-side from it, so that's what buildFileTree() does below.
// get_file() is only called on click-to-open, per that module's
// size/many-files reasoning for keeping list_files() content-free.
function buildFileTree(filesMeta) {
  const root = { type: "dir", name: "", path: "", children: {} };
  for (const path of Object.keys(filesMeta).sort()) {
    const parts = path.split("/");
    let node = root;
    let acc = "";
    parts.forEach((part, i) => {
      acc = acc ? `${acc}/${part}` : part;
      if (i === parts.length - 1) {
        node.children[part] = { type: "file", name: part, path: acc, meta: filesMeta[path] };
      } else {
        if (!node.children[part]) {
          node.children[part] = { type: "dir", name: part, path: acc, children: {} };
        }
        node = node.children[part];
      }
    });
  }
  return root;
}

// Directories first (alphabetical), then files (alphabetical) -- same
// ordering convention as most file-tree UIs, so generated folders like
// `src/`/`tests/` don't get interleaved with loose root files.
function TreeNode({ node, depth, expandedDirs, onToggleDir, selectedPath, onSelectFile }) {
  const entries = Object.values(node.children).sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  return (
    <>
      {entries.map((entry) => {
        if (entry.type === "dir") {
          const isOpen = expandedDirs.has(entry.path);
          return (
            <div key={entry.path}>
              <button
                type="button"
                onClick={() => onToggleDir(entry.path)}
                className="w-full flex items-center gap-1 text-xs text-[var(--neutral-300)] hover:text-[var(--neutral-100)] py-0.5 rounded"
                style={{ paddingLeft: `${depth * 14}px` }}
              >
                {isOpen ? <ChevronDown size={12} className="shrink-0" /> : <ChevronRight size={12} className="shrink-0" />}
                {isOpen ? <FolderOpen size={12} className="shrink-0" /> : <Folder size={12} className="shrink-0" />}
                <span className="truncate">{entry.name}</span>
              </button>
              {isOpen && (
                <TreeNode
                  node={entry}
                  depth={depth + 1}
                  expandedDirs={expandedDirs}
                  onToggleDir={onToggleDir}
                  selectedPath={selectedPath}
                  onSelectFile={onSelectFile}
                />
              )}
            </div>
          );
        }
        const isSelected = entry.path === selectedPath;
        return (
          <button
            key={entry.path}
            type="button"
            onClick={() => onSelectFile(entry.path)}
            title={entry.path}
            className={`w-full flex items-center gap-1 text-xs py-0.5 rounded ${
              isSelected
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-400)] hover:text-[var(--neutral-100)]"
            }`}
            style={{ paddingLeft: `${depth * 14 + 16}px` }}
          >
            <FileCode size={12} className="shrink-0" />
            <span className="truncate">{entry.name}</span>
          </button>
        );
      })}
    </>
  );
}

function CodeView({ workspaceId, apiUrl }) {
  const [filesMeta, setFilesMeta] = useState(null); // {file_path: meta}, from list_files()
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedDirs, setExpandedDirs] = useState(() => new Set());
  const [selectedPath, setSelectedPath] = useState(null);
  const [fileContent, setFileContent] = useState(null); // last-saved shape, from get_file()/write_file()
  const [editedContent, setEditedContent] = useState("");
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [downloading, setDownloading] = useState(false); // NEW — patch 11
  const [downloadError, setDownloadError] = useState(null); // NEW — patch 11

  async function loadFileList({ preserveSelection = true } = {}) {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/files`, {
        headers: await authHeaders(),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const meta = await res.json();
      setFilesMeta(meta);
      // Auto-expand top-level directories the first time files show up,
      // so the tree isn't a single collapsed root on first load.
      setExpandedDirs((prev) => {
        if (prev.size > 0) return prev;
        const next = new Set();
        for (const path of Object.keys(meta)) {
          if (path.includes("/")) next.add(path.split("/")[0]);
        }
        return next;
      });
      if (!preserveSelection || (selectedPath && !(selectedPath in meta))) {
        setSelectedPath(null);
        setFileContent(null);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (workspaceId) loadFileList({ preserveSelection: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  async function openFile(path) {
    setSelectedPath(path);
    setFileLoading(true);
    setFileError(null);
    setSaveError(null);
    try {
      // {file_path:path} on the backend accepts the raw slashes as-is —
      // no encoding needed, file_path is already shape-validated server
      // side (workspace_code_files._validate_file_path).
      const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/files/${path}`, {
        headers: await authHeaders(),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const file = await res.json();
      setFileContent(file);
      setEditedContent(file.content || "");
    } catch (err) {
      setFileError(err.message);
    } finally {
      setFileLoading(false);
    }
  }

  function toggleDir(path) {
    setExpandedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  const isDirty = fileContent != null && editedContent !== (fileContent.content || "");

  async function saveFile() {
    if (!selectedPath || !isDirty) return;
    setSaving(true);
    setSaveError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/files/${selectedPath}`, {
        method: "PUT",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ content: editedContent }),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const saved = await res.json();
      setFileContent(saved);
      setEditedContent(saved.content || "");
      // Swap just this file's tree metadata in-place rather than
      // re-fetching the whole list — same "swap the piece that changed"
      // approach InstructionsView takes with the toggle-step response.
      setFilesMeta((prev) => (prev ? {
        ...prev,
        [selectedPath]: {
          workspace_id: saved.workspace_id,
          file_path: saved.file_path,
          language: saved.language,
          size: saved.content ? saved.content.length : 0,
          updated_at: saved.updated_at,
          updated_by: saved.updated_by,
        },
      } : prev));
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  }

  // NEW — patch 11: server-side ZIP of the current file set
  // (GET .../code/zip). Needs authHeaders() same as every other call
  // here, so this can't be a plain <a href> — fetch as a blob and
  // trigger the download via a throwaway object URL, same technique
  // any auth-gated file download needs in the browser.
  async function downloadZip() {
    setDownloading(true);
    setDownloadError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${workspaceId}/code/zip`, {
        headers: await authHeaders(),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${workspaceId}_code.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setDownloadError(err.message);
    } finally {
      setDownloading(false);
    }
  }

  const tree = filesMeta ? buildFileTree(filesMeta) : null;
  const fileCount = filesMeta ? Object.keys(filesMeta).length : 0;

  return (
    <div className="grid grid-cols-[220px_1fr] gap-4 min-h-[360px]">
      {/* File tree */}
      <div className="border border-[var(--neutral-800)] rounded-lg p-2 overflow-y-auto max-h-[560px]">
        <div className="flex items-center justify-between px-1 pb-1.5 mb-1 border-b border-[var(--neutral-800)]">
          <span className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">
            Files{fileCount ? ` (${fileCount})` : ""}
          </span>
          <div className="flex items-center gap-2">
            {/* NEW — patch 11: server-side ZIP download button, disabled
                until there's at least one saved file. */}
            <button
              type="button"
              onClick={downloadZip}
              disabled={downloading || fileCount === 0}
              aria-label="Download all files as ZIP"
              title="Download as ZIP"
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
            >
              {downloading ? <Loader2 size={11} className="animate-spin" /> : <Download size={11} />}
            </button>
            <button
              type="button"
              onClick={() => loadFileList()}
              disabled={loading}
              aria-label="Refresh file list"
              title="Refresh"
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)] disabled:opacity-50"
            >
              <RefreshCw size={11} className={loading ? "animate-spin" : ""} />
            </button>
          </div>
        </div>
        {downloadError && <p className="text-[10px] text-red-400 px-1 pb-1">{downloadError}</p>}
        {loading && !filesMeta ? (
          <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5 px-1 py-1">
            <Loader2 size={12} className="animate-spin" /> Loading…
          </div>
        ) : error ? (
          <p className="text-xs text-red-400 px-1">{error}</p>
        ) : fileCount === 0 ? (
          <p className="text-xs text-[var(--neutral-600)] px-1">
            No files yet — ask this project&apos;s chat to build something and generated files will show up here.
          </p>
        ) : (
          <TreeNode
            node={tree}
            depth={0}
            expandedDirs={expandedDirs}
            onToggleDir={toggleDir}
            selectedPath={selectedPath}
            onSelectFile={openFile}
          />
        )}
      </div>

      {/* Editor */}
      <div className="border border-[var(--neutral-800)] rounded-lg p-3 flex flex-col min-h-[360px]">
        {!selectedPath ? (
          <p className="text-xs text-[var(--neutral-600)] m-auto">Select a file to view or edit it.</p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2 pb-2 mb-2 border-b border-[var(--neutral-800)]">
              <div className="min-w-0">
                <p className="text-xs text-[var(--neutral-200)] font-medium truncate">{selectedPath}</p>
                {fileContent && (
                  <p className="text-[10px] text-[var(--neutral-600)]">
                    {fileContent.language || "text"}
                    {fileContent.updated_at
                      ? ` · saved ${new Date(fileContent.updated_at).toLocaleString()}`
                      : " · not saved yet"}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={saveFile}
                disabled={!isDirty || saving}
                className="shrink-0 flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-2.5 py-1.5 font-medium disabled:opacity-50"
              >
                {saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                {saving ? "Saving…" : isDirty ? "Save" : "Saved"}
              </button>
            </div>
            {saveError && <p className="text-xs text-red-400 pb-2">{saveError}</p>}
            {fileLoading ? (
              <div className="text-xs text-[var(--neutral-600)] flex items-center gap-1.5 m-auto">
                <Loader2 size={12} className="animate-spin" /> Loading…
              </div>
            ) : fileError ? (
              <p className="text-xs text-red-400">{fileError}</p>
            ) : (
              <textarea
                value={editedContent}
                onChange={(e) => setEditedContent(e.target.value)}
                spellCheck={false}
                aria-label={`Editing ${selectedPath}`}
                className="flex-1 w-full min-h-[300px] bg-black/30 border border-[var(--neutral-800)] rounded-lg p-2.5 text-[11px] font-mono text-[var(--neutral-200)] outline-none focus:border-[var(--accent)] resize-none"
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

// Part 7 §7.6 -- deploy action button + status indicator, three separate
// calls matching the three separate-risk backend endpoints from §7.4
// (propose / write / go-live). "Go Live" is intentionally left
// unwired for now -- see the accompanying chat message: the backend
// endpoint currently blocks on a server-terminal y/N prompt
// (agents/deploy_agent.py's _confirm_deploy()), which a browser fetch()
// can't answer. Wiring it here today would just hang the request.
function DeployPanel({ sessionId, apiUrl, deployConfigPlan, lastDeployConfigSummary, onRefresh }) {
  const [busy, setBusy] = useState(null); // "propose" | "write" | null
  const [error, setError] = useState(null);

  async function call(action) {
    setBusy(action);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/deploy/${sessionId}/${action}`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  const hasConfig = Boolean(lastDeployConfigSummary);
  const hasPlan = Boolean(deployConfigPlan);

  return (
    <Card title="Deploy">
      <div className="space-y-2 text-[11px]">
        {!hasPlan && (
          <p className="text-cyber-dim">
            No deploy plan proposed yet for this project.
          </p>
        )}
        {hasPlan && (
          <div className="text-cyber-text">
            <p>
              <span className="text-cyber-dim/70">platform:</span>{" "}
              {deployConfigPlan.platform}
            </p>
            <p>
              <span className="text-cyber-dim/70">config file:</span>{" "}
              <span className="font-mono">{deployConfigPlan.config_filename}</span>
            </p>
            {deployConfigPlan.reason && (
              <p className="text-cyber-dim">{deployConfigPlan.reason}</p>
            )}
          </div>
        )}
        {hasConfig && (
          <p className="text-cyber-cyan">
            Config written to disk ({lastDeployConfigSummary.config_filename}) --
            ready for a manual deploy, or for &quot;Go Live&quot; once that&apos;s wired up.
          </p>
        )}
        {error && <p className="text-rose-400">{error}</p>}
        <div className="flex gap-2 pt-1">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => call("propose")}
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
          >
            {busy === "propose" ? "Proposing..." : hasPlan ? "Re-propose" : "Propose"}
          </button>
          <button
            type="button"
            disabled={busy !== null || !hasPlan}
            onClick={() => call("write")}
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
          >
            {busy === "write" ? "Writing..." : "Write Config"}
          </button>
          <button
            type="button"
            disabled
            title="Not wired up yet -- see chat"
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-dim/30 text-cyber-dim/50 rounded px-2 py-1 cursor-not-allowed"
          >
            Go Live
          </button>
        </div>
      </div>
    </Card>
  );
}

// Part 7 §7.6 -- monitoring widget. Sentry is read-only status (it's an
// ordinary generated module, nothing for the user to configure here);
// UptimeRobot needs a one-time API key + a URL to register, since
// agents/deploy_agent.py's register_uptimerobot_monitor() deliberately
// takes an explicit url rather than reading one off a real deploy (no
// real host client exists yet -- see that module's docstring).
const SENTRY_STATUS_LABELS = {
  not_planned: "Not planned",
  planned: "Planned this cycle",
  configured: "Configured",
};

function MonitoringWidget({ sessionId, apiUrl, monitoring, onRefresh }) {
  const [apiKey, setApiKey] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(null); // "key" | "register" | null
  const [error, setError] = useState(null);

  const sentryStatus = monitoring?.sentry_status || "not_planned";
  const uptimerobot = monitoring?.uptimerobot || null;

  async function saveKey() {
    if (!apiKey) return;
    setBusy("key");
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/monitoring/${sessionId}/uptimerobot-key`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      setApiKey("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  async function register() {
    if (!url) return;
    setBusy("register");
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/monitoring/${sessionId}/uptimerobot-register`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ url }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail || `${res.status} ${res.statusText}`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="Monitoring">
      <div className="space-y-3 text-[11px]">
        <div>
          <span className="text-cyber-dim/70">Sentry:</span>{" "}
          <span className="text-cyber-text">{SENTRY_STATUS_LABELS[sentryStatus] || sentryStatus}</span>
        </div>

        <div className="space-y-1.5">
          <span className="text-cyber-dim/70">UptimeRobot:</span>{" "}
          {uptimerobot ? (
            uptimerobot.status === "registered" ? (
              <span className="text-cyber-cyan">
                registered -- {uptimerobot.friendly_name} ({uptimerobot.url})
              </span>
            ) : (
              <span className="text-rose-400">{uptimerobot.message}</span>
            )
          ) : (
            <span className="text-cyber-dim">not registered yet</span>
          )}

          <div className="flex flex-wrap gap-1.5 pt-1">
            <input
              type="password"
              id="build-uptimerobot-api-key"
              name="buildUptimerobotApiKey"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              aria-label="UptimeRobot API key"
              placeholder="UptimeRobot API key"
              className="flex-1 min-w-[140px] bg-cyber-bg border border-cyber-border rounded px-2 py-1 text-cyber-text placeholder:text-cyber-dim/50 outline-none"
            />
            <button
              type="button"
              disabled={busy !== null || !apiKey}
              onClick={saveKey}
              className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
            >
              {busy === "key" ? "Saving..." : "Save Key"}
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <input
              type="text"
              id="build-uptimerobot-url"
              name="buildUptimerobotUrl"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              aria-label="Deployed URL"
              placeholder="https://your-deployed-url.example.com"
              className="flex-1 min-w-[180px] bg-cyber-bg border border-cyber-border rounded px-2 py-1 text-cyber-text placeholder:text-cyber-dim/50 outline-none"
            />
            <button
              type="button"
              disabled={busy !== null || !url}
              onClick={register}
              className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
            >
              {busy === "register" ? "Registering..." : "Register"}
            </button>
          </div>
          <p className="text-cyber-dim/70">
            No live URL from a real deploy yet (see the Deploy panel above)
            -- paste the URL to monitor manually for now.
          </p>
        </div>

        {error && <p className="text-rose-400">{error}</p>}
      </div>
    </Card>
  );
}

// Parts pricing — live-fetch panel, same shape as DeployPanel/
// MonitoringWidget above: a direct fetch() with authHeaders(), not the
// paste-panel pattern PlanTab.jsx uses. Parts live in
// workspace_facts.custom.parts (see api/server.py's refresh_part_prices),
// read back via the existing GET /api/workspaces/{ws_id}/facts endpoint
// rather than a new one -- no dedicated parts store exists yet.
function PartsPanel({ wsId, apiUrl }) {
  const [parts, setParts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [newPartName, setNewPartName] = useState("");
  const [newPartQty, setNewPartQty] = useState(1);

  async function loadFacts() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${wsId}/facts`, {
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const facts = await res.json();
      setParts(facts?.custom?.parts || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (wsId) loadFacts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsId]);

  function addPart() {
    if (!newPartName.trim()) return;
    setParts((prev) => [
      ...prev,
      { id: crypto.randomUUID(), name: newPartName.trim(), qty: Number(newPartQty) || 1 },
    ]);
    setNewPartName("");
    setNewPartQty(1);
  }

  function removePart(id) {
    setParts((prev) => prev.filter((p) => p.id !== id));
  }

  async function refreshPrices(forceRefresh = false) {
    if (parts.length === 0) return;
    setRefreshing(true);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${wsId}/parts/refresh-prices`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ parts, force_refresh: forceRefresh }),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const { parts: updated } = await res.json();
      setParts(updated);
    } catch (err) {
      setError(err.message);
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <Card
      title={`Parts${parts.length ? ` (${parts.length})` : ""}`}
      action={
        <button
          type="button"
          disabled={refreshing || parts.length === 0}
          onClick={() => refreshPrices(false)}
          className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
        >
          {refreshing ? "Refreshing..." : "Refresh prices"}
        </button>
      }
    >
      <div className="space-y-2 text-[11px]">
        {loading ? (
          <p className="text-cyber-dim">Loading...</p>
        ) : (
          <>
            {parts.length === 0 && (
              <p className="text-cyber-dim">No parts added yet.</p>
            )}
            {parts.map((p) => (
              <div key={p.id} className="flex items-center justify-between gap-2 border-b border-cyber-border/50 pb-1.5">
                <div className="min-w-0">
                  <p className="text-cyber-text truncate">{p.name} <span className="text-cyber-dim/70">×{p.qty}</span></p>
                  {p.estimated_price_bdt != null ? (
                    <p className="text-cyber-dim">
                      ৳{p.estimated_price_bdt}
                      {p.vendor_url ? (
                        <a href={p.vendor_url} target="_blank" rel="noreferrer" className="text-cyber-cyan ml-1 underline">
                          {p.vendor_name || "source"}
                        </a>
                      ) : null}
                      {p.price_checked_at && (
                        <span className="text-cyber-dim/60"> — checked {new Date(p.price_checked_at).toLocaleDateString()}</span>
                      )}
                    </p>
                  ) : (
                    <p className="text-cyber-dim/60">Not priced yet</p>
                  )}
                </div>
                <button onClick={() => removePart(p.id)} className="text-cyber-dim/60 hover:text-rose-400 shrink-0">✕</button>
              </div>
            ))}
          </>
        )}
        {error && <p className="text-rose-400">{error}</p>}
        <div className="flex gap-1.5 pt-1">
          <input
            id="build-new-part-name"
            name="buildNewPartName"
            value={newPartName}
            onChange={(e) => setNewPartName(e.target.value)}
            aria-label="Part name"
            placeholder="Part name, e.g. HolyBro Kakute H7 V2"
            className="flex-1 min-w-0 bg-black/30 border border-cyber-border rounded px-2 py-1 text-[11px] outline-none focus:border-cyber-cyan"
          />
          <input
            type="number"
            id="build-new-part-qty"
            name="buildNewPartQty"
            min={1}
            value={newPartQty}
            onChange={(e) => setNewPartQty(e.target.value)}
            aria-label="Part quantity"
            className="w-14 bg-black/30 border border-cyber-border rounded px-2 py-1 text-[11px] outline-none focus:border-cyber-cyan"
          />
          <button
            type="button"
            onClick={addPart}
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1"
          >
            Add
          </button>
        </div>
      </div>
    </Card>
  );
}

function BuildTab({ onPromoted, onActiveWorkspaceChange }) {
  // §7 fix: workspaces + promoteWorkspace come from the same
  // SessionContext NotebooksTab/ResearchTab already use — no new context
  // plumbing needed, Tasks just reads the shared list and filters it.
  // NEW — patch 7: fetchDeviceSpec/toggleInstructionStep pulled from the
  // same global SessionContext PlanTab.jsx's BlueprintView already used
  // for these -- confirmed workspace-scoped, not Plan-tab-scoped, so no
  // new plumbing needed here.
  const { promoteWorkspace, API_URL, fetchDeviceSpec, toggleInstructionStep } = useSession();
  const { chats } = useChatList();   // CHANGED — Item 2 concern split, slice 4: was useSession()
  const { workspaces, fetchWorkspaces } = useWorkspaces();   // CHANGED — was useSession()
  // NEW — item #11 / C2: same dock-driven "open chat" + row-highlight
  // pattern as ResearchTab/PlanTab's C1.
  const { switchChat, renameChat, deleteChat, createWorkspaceChat } = useWorkspaceDockActions();
  const activeChatId = useLastActiveChatId();

  // Build-stage workspaces only -- the "picked" list for this tab, same
  // shape as NotebooksTab's `notebooks` / ResearchTab's `researchProjects`.
  const buildProjects = workspaces.filter((w) => (w.active_stages || [w.stage]).includes("build"));

  const [selectedWsId, setSelectedWsId] = useState(null);
  const [restoredSelection, setRestoredSelection] = useState(false);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedFeature, setExpandedFeature] = useState(null);
  // NEW — patch 7: which of BUILD_VIEWS is showing for the selected
  // project. Same pattern as PlanTab's BlueprintView `view` state.
  const [buildView, setBuildView] = useState("tasks");
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  const [promoteTargetStage, setPromoteTargetStage] = useState("test");
  // NEW — §2.6 step 4: "complete" (existing behavior, leaves this tab)
  // vs "partial" (stays active here too, per §2.1/§2.2). Same toggle as
  // Notebooks/Research/Plan.
  const [promoteMode, setPromoteMode] = useState("complete");
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);
  const [projectsCollapsed, setProjectsCollapsed] = useState(false); // NEW — collapsible project-picker sidebar
  // NEW — item #10 / B3: native "create project" trigger, same pattern
  // as ResearchTab's B2. This tab can now create its own build-stage
  // workspace directly, instead of requiring a promotion from Plan or
  // the chat sidebar's folder button — those remain valid paths in,
  // this is just no longer the only one.
  const [showCreateModal, setShowCreateModal] = useState(false);
  // NEW — issue #3: nested-chat create/rename/delete state, same shape as
  // ResearchTab's/NotebooksTab's/PlanTab's own.
  const [creatingChatForWs, setCreatingChatForWs] = useState(null);
  const [editingChatId, setEditingChatId] = useState(null);
  const [editChatTitle, setEditChatTitle] = useState("");
  const [pendingDeleteChat, setPendingDeleteChat] = useState(null);

  useEffect(() => {
    setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
    setProjectsCollapsed(localStorage.getItem(PROJECTS_KEY) === "1"); // NEW — collapsible project sidebar
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  // NEW — collapsible project-picker sidebar, same toggle pattern as
  // toggleChatDock above, its own localStorage key so the two collapse
  // independently.
  function toggleProjects() {
    setProjectsCollapsed((prev) => {
      localStorage.setItem(PROJECTS_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }
  // NEW — item #11 / C2: same "switch + expand, no tab jump" helper as
  // ResearchTab/PlanTab's openInDock.
  async function openInDock(chatId) {
    await switchChat(chatId);
    if (chatDockCollapsed) toggleChatDock();
  }

  // NEW — issue #3: "+" beside a project name. Creates a chat nested
  // directly inside that project and opens it, same mechanic the Chat
  // sidebar uses for "new chat in this group".
  async function handleCreateChatInProject(ws) {
    setCreatingChatForWs(ws.id);
    try {
      if (selectedWsId !== ws.id) setSelectedWsId(ws.id);
      await createWorkspaceChat(ws.id);
      if (chatDockCollapsed) toggleChatDock();
    } finally {
      setCreatingChatForWs(null);
    }
  }

  function startRenameChat(chat) {
    setEditingChatId(chat.id);
    setEditChatTitle(chat.title);
  }

  async function commitRenameChat(chatId) {
    if (editChatTitle.trim()) await renameChat(chatId, editChatTitle.trim());
    setEditingChatId(null);
  }

  function askDeleteChat(chat) {
    setPendingDeleteChat(chat);
  }

  async function confirmDeleteChat() {
    await deleteChat(pendingDeleteChat.id);
    setPendingDeleteChat(null);
  }

  // Restore last-selected build project on mount (same pattern as
  // NotebooksTab's SELECTED_NOTEBOOK_KEY restore effect).
  useEffect(() => {
    const savedId = localStorage.getItem(SELECTED_BUILD_WS_KEY);
    if (savedId) setSelectedWsId(savedId);
    setRestoredSelection(true);
  }, []);

  useEffect(() => {
    if (!restoredSelection || !selectedWsId) return;
    localStorage.setItem(SELECTED_BUILD_WS_KEY, selectedWsId);
  }, [selectedWsId, restoredSelection]);

  // Auto-select the first build project once loaded, or recover if a
  // previously-saved selection was promoted onward / deleted.
  useEffect(() => {
    if (!restoredSelection || buildProjects.length === 0) return;
    const stillExists = selectedWsId && buildProjects.some((w) => w.id === selectedWsId);
    if (!stillExists) setSelectedWsId(buildProjects[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildProjects, selectedWsId, restoredSelection]);

  // data._session_id is the raw chat_id the backend resolved ws_id to
  // (see api/server.py's get_tasks_for_workspace) -- DeployPanel and
  // MonitoringWidget still hit /api/deploy/{session_id}/... and
  // /api/monitoring/{session_id}/... directly, unchanged, so they need
  // that resolved id rather than the workspace id.
  const resolvedSessionId = data?._session_id || null;

  async function refresh() {
    if (!selectedWsId) return;
    const res = await fetch(`${API_URL}/api/tasks/workspace/${selectedWsId}`, {
      headers: await authHeaders(),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const d = await res.json();
    setData(d);
    return d;
  }

  useEffect(() => {
    if (!selectedWsId) {
      setData(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    refresh()
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [API_URL, selectedWsId]);

  async function handlePromote(wsId, toStage = promoteTargetStage, mode = promoteMode) {
    setPromoting(true);
    setPromoteError(null);
    try {
      await promoteWorkspace(wsId, toStage, mode);
      await fetchWorkspaces();
      onPromoted?.(toStage, wsId);
      setPromoteMode("complete");
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromoting(false);
    }
  }

  const selected = buildProjects.find((w) => w.id === selectedWsId);

  // NEW — item #1: the Data bubble now lives in AppShell's top nav, not
  // floating over this tab's own content, so this just reports which
  // project (if any) is selected instead of rendering the bubble itself.
  useEffect(() => {
    onActiveWorkspaceChange?.(selected?.id || null, selected?.name);
  }, [selected?.id, selected?.name, onActiveWorkspaceChange]);

  const features = data?.current_plan?.features || [];
  const featureStatus = data?.feature_status || {};
  const targetFeature = data?.current_plan?.target_feature || null;
  const targetModules = data?.module_specs?.modules || null;
  const byColumn = COLUMNS.map((col) => ({
    ...col,
    features: features.filter((f) => statusFor(featureStatus, f) === col.status),
  }));

  return (
    <div className="flex h-full">
      {/* Build-project picker -- same left column pattern as
          NotebooksTab/ResearchTab, filtered to stage === "build" instead
          of "note"/"research". */}
      {projectsCollapsed ? (
        <div className="w-10 shrink-0 border-r border-[var(--neutral-800)] flex flex-col items-center py-3 gap-3">
          <button onClick={toggleProjects} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]" title="Show projects">
            <ChevronRight size={16} />
          </button>
        </div>
      ) : (
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col h-full">
        <div className="flex items-center justify-between px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <STAGE_THEME.build.Icon size={13} className={STAGE_THEME.build.color} /> Build
          </span>
          {/* NEW — item #10 / B3: native create, same stage-aware modal
              ResearchTab's B2 wired up first. */}
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowCreateModal(true)}
              title="New build project"
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
            >
              <Plus size={14} />
            </button>
            {/* NEW — collapsible sidebar, same affordance as ChatSidebar's
                own ChevronLeft toggle. */}
            <button onClick={toggleProjects} title="Hide projects" className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
              <ChevronLeft size={14} />
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {buildProjects.map((ws) => {
            // NEW — item #11 / C2: nested chat list, same pattern as
            // ResearchTab/PlanTab's C1 — "expand" just means "is the
            // selected project", no separate toggle state needed since
            // this tab already has a single-selection model.
            const isSelected = ws.id === selectedWsId;
            const memberChats = isSelected ? chats.filter((c) => ws.chat_ids.includes(c.id)) : [];
            return (
              <div key={ws.id} className="border-b border-[var(--neutral-900)]">
                <div
                  className={`group flex items-center gap-1 ${
                    isSelected ? "bg-[var(--neutral-800-a70)]" : "hover:bg-[var(--neutral-900)]"
                  }`}
                >
                  <button
                    onClick={() => setSelectedWsId(ws.id)}
                    className="flex-1 min-w-0 flex items-center justify-between gap-1 px-3 py-2 text-left"
                  >
                    <span className="flex items-center min-w-0">
                      <WorkspaceStageIcons workspace={ws} />
                      <span className="text-xs text-[var(--neutral-200)] truncate">
                        {ws.name}
                        <span className="text-[var(--neutral-600)]"> · {ws.chat_ids.length}</span>
                      </span>
                    </span>
                    {isSelected && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
                  </button>
                  {/* NEW — issue #3: "+" creates a chat nested in this
                      project, same idea as starting a new chat under a
                      group in the Chat sidebar. */}
                  <button
                    onClick={(e) => { e.stopPropagation(); handleCreateChatInProject(ws); }}
                    title="New chat in this project"
                    className="shrink-0 pr-2 opacity-0 group-hover:opacity-100 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
                    disabled={creatingChatForWs === ws.id}
                  >
                    {creatingChatForWs === ws.id ? (
                      <Loader2 size={12} className="animate-spin" />
                    ) : (
                      <Plus size={12} />
                    )}
                  </button>
                </div>
                {memberChats.map((chat) => (
                  <div
                    key={chat.id}
                    onClick={() => editingChatId !== chat.id && openInDock(chat.id)}
                    className={`group flex items-center gap-1.5 text-left pl-7 pr-3 py-1.5 text-[11px] cursor-pointer ${
                      chat.id === activeChatId
                        ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                        : "text-[var(--neutral-500)] hover:bg-[var(--neutral-900)] hover:text-[var(--neutral-300)]"
                    }`}
                  >
                    {editingChatId === chat.id ? (
                      <div className="flex items-center gap-1 flex-1 min-w-0" onClick={(e) => e.stopPropagation()}>
                        <input
                          autoFocus
                          id={`chat-title-${chat.id}`}
                          name="chatTitle"
                          aria-label="Chat title"
                          value={editChatTitle}
                          onChange={(e) => setEditChatTitle(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && commitRenameChat(chat.id)}
                          className="flex-1 min-w-0 bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-1.5 py-0.5 text-[11px] outline-none"
                        />
                        <button onClick={() => commitRenameChat(chat.id)}><Check size={12} className="text-green-400" /></button>
                        <button onClick={() => setEditingChatId(null)}><X size={12} className="text-[var(--neutral-500)]" /></button>
                      </div>
                    ) : (
                      <>
                        <MessageSquare size={10} className="shrink-0 text-[var(--neutral-600)]" />
                        <span className="truncate flex-1 min-w-0">{chat.title}</span>
                        {/* NEW — issue #3: rename/delete, same controls
                            ChatSidebar's own chat rows already offer. */}
                        <div className="hidden group-hover:flex items-center gap-1.5 shrink-0">
                          <button onClick={(e) => { e.stopPropagation(); startRenameChat(chat); }} title="Rename chat">
                            <Pencil size={10} className="text-[var(--neutral-500)] hover:text-[var(--neutral-200)]" />
                          </button>
                          <button onClick={(e) => { e.stopPropagation(); askDeleteChat(chat); }} title="Delete chat">
                            <Trash2 size={10} className="text-[var(--neutral-500)] hover:text-red-400" />
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                ))}
              </div>
            );
          })}
          {buildProjects.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No build-stage projects yet — create one above, or promote a project to Build from the Plan tab.
            </p>
          )}
        </div>
      </div>
      )}

      {/* Selected project's board */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!selected ? (
          <div className="h-full flex items-center justify-center text-sm text-[var(--neutral-600)]">
            Select a build project to see its task board.
          </div>
        ) : loading ? (
          <div className="px-4 py-6 max-w-4xl mx-auto">
            <p className="text-xs text-cyber-dim">Loading board...</p>
          </div>
        ) : error ? (
          <div className="px-4 py-6 max-w-4xl mx-auto">
            <p className="text-xs text-rose-400">
              Couldn&apos;t load the task board: {error}. Check that{" "}
              <code className="font-mono">GET /api/tasks/workspace/{"{ws_id}"}</code> is reachable
              and that you&apos;re signed in with a valid session.
            </p>
          </div>
        ) : (
          <div className="relative px-4 py-6 max-w-4xl mx-auto space-y-4">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-base font-medium text-[var(--neutral-100)]">{selected.name}</h2>
              <div className="flex items-center gap-2 shrink-0">
                {(() => {
                  // NEW — §2.2: exclude stages already active for this
                  // workspace — same rule as Notebooks/Research/Plan.
                  const activeHere = selected.active_stages || [selected.stage];
                  const availableTargets = PROMOTE_TARGETS.filter((s) => !activeHere.includes(s));
                  const targetStage = availableTargets.includes(promoteTargetStage)
                    ? promoteTargetStage
                    : availableTargets[0];
                  if (!availableTargets.length) return null;
                  return (
                    <>
                      <label className="sr-only" htmlFor="build-promote-target">Promote to</label>
                      <select
                        id="build-promote-target"
                        value={targetStage}
                        onChange={(e) => setPromoteTargetStage(e.target.value)}
                        disabled={promoting}
                        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-2 py-1.5 text-xs outline-none disabled:opacity-50"
                      >
                        {availableTargets.map((stage) => (
                          <option key={stage} value={stage}>{PROMOTE_LABELS[stage]}</option>
                        ))}
                      </select>
                      {/* NEW — §2.6 step 4: complete/partial toggle. */}
                      <div
                        role="radiogroup"
                        aria-label="Promote mode"
                        className="flex items-center rounded-lg border border-[var(--neutral-700)] overflow-hidden text-xs shrink-0"
                      >
                        <button
                          type="button"
                          role="radio"
                          aria-checked={promoteMode === "complete"}
                          onClick={() => setPromoteMode("complete")}
                          disabled={promoting}
                          title="Move the project fully into the target stage"
                          className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
                            promoteMode === "complete"
                              ? "bg-[var(--accent)] text-[var(--accent-text)]"
                              : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
                          }`}
                        >
                          Complete
                        </button>
                        <button
                          type="button"
                          role="radio"
                          aria-checked={promoteMode === "partial"}
                          onClick={() => setPromoteMode("partial")}
                          disabled={promoting}
                          title="Keep the project active here too"
                          className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
                            promoteMode === "partial"
                              ? "bg-[var(--accent)] text-[var(--accent-text)]"
                              : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
                          }`}
                        >
                          Partial
                        </button>
                      </div>
                      <button
                        onClick={() => handlePromote(selected.id, targetStage)}
                        disabled={promoting}
                        className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
                      >
                        {promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
                        {promoteMode === "partial" ? "Add to" : "Promote to"} {PROMOTE_LABELS[targetStage]} →
                      </button>
                    </>
                  );
                })()}
              </div>
            </div>

            {/* NEW — patch 7: Tasks / Instructions sub-nav, same small
                tab-bar pattern as PlanTab's BlueprintView. */}
            <nav className="flex gap-1">
              {BUILD_VIEWS.map((v) => (
                <button
                  key={v.id}
                  onClick={() => setBuildView(v.id)}
                  className={`text-xs rounded px-2.5 py-1 ${
                    buildView === v.id
                      ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                      : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                  }`}
                >
                  {v.label}
                </button>
              ))}
            </nav>

            {buildView === "instructions" ? (
              <InstructionsView
                workspaceId={selected.id}
                fetchDeviceSpec={fetchDeviceSpec}
                toggleInstructionStep={toggleInstructionStep}
              />
            ) : buildView === "code" ? (
              // NEW — patch 10: Code sub-tab, file tree + inline editor,
              // wired to patch 8's GET/PUT .../code/files endpoints.
              <CodeView workspaceId={selected.id} apiUrl={API_URL} />
            ) : (
              <>
                {promoteError && <p className="text-xs text-red-400">{promoteError}</p>}

                {features.length === 0 ? (
                  <p className="text-cyber-dim text-sm">
                    No plan yet for this project. Once a coding-domain build cycle runs, its features
                    and status will show up here.
                  </p>
                ) : (
                  <>
                    <div className="grid gap-4 sm:grid-cols-3">
                      {byColumn.map((col) => (
                        <Card key={col.status} title={`${col.label} (${col.features.length})`}>
                          <div className="space-y-2">
                            {col.features.length === 0 && (
                              <p className="text-[11px] text-cyber-dim">Nothing here.</p>
                            )}
                            {col.features.map((name) => (
                              <FeatureCard
                                key={name}
                                name={name}
                                isTarget={name === targetFeature}
                                targetModules={name === targetFeature ? targetModules : null}
                                expanded={expandedFeature === name}
                                onToggle={() => setExpandedFeature(expandedFeature === name ? null : name)}
                              />
                            ))}
                          </div>
                        </Card>
                      ))}
                    </div>
                    <IntegrationChecklist integrations={data?.integrations} />
                    <div className="grid gap-4 sm:grid-cols-2">
                      <DeployPanel
                        sessionId={resolvedSessionId}
                        apiUrl={API_URL}
                        deployConfigPlan={data?.deploy_config_plan}
                        lastDeployConfigSummary={data?.last_deploy_config_summary}
                        onRefresh={refresh}
                      />
                      <MonitoringWidget
                        sessionId={resolvedSessionId}
                        apiUrl={API_URL}
                        monitoring={data?.monitoring}
                        onRefresh={refresh}
                      />
                    </div>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* CHANGED — step 3e: was rendered bare (legacy mode, reading the
          global SessionContext sessionId regardless of which build
          project was selected above). Now passes this tab's own
          `selected` project so the dock resolves ws:${selected.id} and
          shows/updates the right project's chat, same fix already
          applied to NotebooksTab. BuildTab never called switchChat
          itself, so no other change was needed here. */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} stacked />
      </div>
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} stacked />
        </div>
      )}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          className="lg:hidden fixed bottom-4 right-4 z-40 bg-[var(--cyber-amber)] text-black rounded-full p-3 shadow-lg"
        >
          <MessageSquare size={18} />
        </button>
      )}

      {/* NEW — item #10 / B3: stage-aware create modal (B1). Auto-selects
          the created project so the user lands straight in it instead of
          having to find it in the list themselves — same as ResearchTab's B2. */}
      {showCreateModal && (
        <CreateWorkspaceModal
          stage="build"
          onClose={(created) => {
            setShowCreateModal(false);
            if (created) setSelectedWsId(created.id);
          }}
        />
      )}

      {/* NEW — issue #3: same delete-confirmation affordance as
          ChatSidebar's own per-chat delete, just scoped to a nested
          project chat here. */}
      <ConfirmDialog
        open={!!pendingDeleteChat}
        title="Delete chat"
        message={`Delete "${pendingDeleteChat?.title}"? Its messages and memory can't be recovered.`}
        confirmLabel="Delete"
        tone="danger"
        onConfirm={confirmDeleteChat}
        onCancel={() => setPendingDeleteChat(null)}
      />
    </div>
  );
}

// Item 6 (perf audit, tab-body pass): BuildTab takes props from its parent
// (onPromoted, onActiveWorkspaceChange). Wrapped in memo() now that
// SessionContext's useCallback pass (item 2) means its stable-identity
// props/callbacks stay stable across unrelated parent re-renders -- prop
// objects/arrays it reads (workspaces, chats, etc.) are only ever replaced,
// never mutated in place, so a shallow prop comparison here is meaningful.
export default memo(BuildTab);
