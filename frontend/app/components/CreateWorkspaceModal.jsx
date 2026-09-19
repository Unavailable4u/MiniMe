"use client";
import { useCallback, useEffect, useState } from "react";
import { useSession } from "../context/SessionContext";
import ResponsiveSheet from "./mobile/ResponsiveSheet";   // NEW — Phase 4 (mobile modal primitive, see MOBILE_PLAN.md)
import { useViewport } from "../hooks/useViewport";

// NEW — item #10 / B1: stage labels for the modal header. Omitting
// `stage` entirely (existing Chat/Notebooks callers) keeps the old
// generic copy — only callers that pass a stage (the new per-tab
// "New project" triggers) get stage-aware wording.
const STAGE_LABELS = {
  research: "Research",
  plan: "Plan",
  build: "Build",
  test: "Test",
  growth: "Growth",
};

// CHANGED — Phase 4 retrofit: the backdrop/centered-w-80-box this
// hand-rolled itself now comes from ResponsiveSheet — same visual box on
// desktop (max-w-xs is the same 20rem as the old w-80; only the backdrop
// went from black/50 to ResponsiveSheet's black/60, same as ConfirmDialog
// and ManageWorkspaceModal already did), a bottom sheet on mobile. Also
// picks up ResponsiveSheet's Escape-to-close and body scroll-lock.
// Mobile-only touches on top of that: the name field is 16px (iOS Safari
// zooms the page on focus of any control under 16px — and autoFocus means
// that happens the instant the sheet opens), buttons get real touch
// targets, and the sheet pads for the home indicator.
export default function CreateWorkspaceModal({ onClose, initialName = "", sourceChatIds = [], stage }) {
  const { createWorkspace, createWorkspaceWithChats } = useSession();
  const [viewport] = useViewport();
  const isMobile = viewport === "mobile";
  const [name, setName] = useState(initialName);
  const [submitting, setSubmitting] = useState(false);

  const wrappingChats = sourceChatIds.length > 0;
  const stageLabel = stage ? STAGE_LABELS[stage] : null;

  useEffect(() => {
    setName(initialName);
  }, [initialName]);

  // Deliberately NOT `onClose={onClose}`: ResponsiveSheet hands its
  // dismiss handler straight to a backdrop onClick, so the caller's
  // onClose(created) would receive the click EVENT as `created` — and
  // callers like ResearchTab/PlanTab/TestTab do `if (created)
  // setActiveWsId(created.id)`. Wrapped so a dismiss always means
  // onClose() with no argument, same as the old `onClick={() => onClose()}`.
  const dismiss = useCallback(() => onClose(), [onClose]);

  async function save() {
    if (!name.trim() || submitting) return;
    setSubmitting(true);
    try {
      // NEW — item #10 / B1: pass the created workspace back to the
      // caller so a tab can auto-select it (e.g. Research selecting
      // the project it just created instead of leaving nothing chosen).
      let created;
      if (wrappingChats) {
        created = await createWorkspaceWithChats(name.trim(), sourceChatIds, stage);
      } else {
        created = await createWorkspace(name.trim(), stage);
      }
      onClose(created);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <ResponsiveSheet
      open
      onClose={dismiss}
      maxWidth="max-w-xs"
      className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] p-4 pb-[calc(1rem+env(safe-area-inset-bottom))]"
    >
      <h3 className="text-sm font-medium text-[var(--neutral-200)] mb-1">
        {wrappingChats
          ? "Create project from selection"
          : stageLabel
          ? `New ${stageLabel} project`
          : "New project"}
      </h3>
      {wrappingChats && (
        <p className="text-[11px] text-[var(--neutral-500)] mb-3">
          {sourceChatIds.length === 1 ? "Wrap 1 chat" : `Wrap ${sourceChatIds.length} chats`} into a new project.
        </p>
      )}
      <input
        autoFocus
        id="project-name"
        name="projectName"
        aria-label="Project name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && save()}
        placeholder="Project name"
        disabled={submitting}
        className={`w-full bg-[var(--neutral-950)] border border-[var(--neutral-700)] rounded px-2 outline-none mb-4 disabled:opacity-60 ${
          isMobile ? "py-2.5 text-base" : "py-1.5 text-xs"
        }`}
      />
      <div className="flex justify-end gap-2">
        <button
          onClick={dismiss}
          disabled={submitting}
          className={`text-xs text-[var(--neutral-400)] px-3 disabled:opacity-60 ${isMobile ? "min-h-[var(--viewport-touch-target)]" : "py-1.5"}`}
        >
          Cancel
        </button>
        <button
          onClick={save}
          disabled={submitting || !name.trim()}
          className={`text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded px-3 font-medium disabled:opacity-60 ${
            isMobile ? "min-h-[var(--viewport-touch-target)] px-5" : "py-1.5"
          }`}
        >
          {submitting ? "Creating…" : wrappingChats ? "Create project" : "Create"}
        </button>
      </div>
    </ResponsiveSheet>
  );
}
