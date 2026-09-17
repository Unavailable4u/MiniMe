"use client";
import { X } from "lucide-react";
import MobileDrawer from "./MobileDrawer";

// Mobile counterpart of the non-`stacked` "docked" branch inside
// ../WorkspaceChatPanel.jsx's own render (see that file's comment at
// the `hidden lg:flex shrink-0` wrapper this replaces below the mobile
// breakpoint). WorkspaceChatPanel still owns workingPanelCollapsed /
// toggleWorkingPanel and still renders the exact same <WorkingPanel/>
// instance either way — this only changes where that instance ends up
// in the DOM/layout, same "presentation forks, logic doesn't" split as
// mobile/ChatSidebar.jsx.
//
// Only wired up for the standalone Chat tab's dock today (the one
// WorkspaceChatPanel instance rendered without `stacked` — see that
// prop's own comment in that file). The six `stacked` domain-tab docks
// (Research/Plan/Test/Growth/Build/Notebooks) still render their
// top-docked row unchanged on every viewport for now — that's Phase
// 5/6 territory, not this one.
export default function WorkingPanelDrawer({ open, onClose, children }) {
  return (
    <MobileDrawer side="right" open={open} onClose={onClose}>
      <div className="flex flex-col h-full w-[85vw] max-w-sm">
        <div className="h-10 px-4 border-b border-[var(--neutral-800)] flex items-center justify-between shrink-0">
          <span className="text-xs font-medium text-[var(--neutral-400)]">Working Panel</span>
          <button
            onClick={onClose}
            title="Close"
            className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
          >
            <X size={16} />
          </button>
        </div>
        <div className="flex-1 min-h-0">{children}</div>
      </div>
    </MobileDrawer>
  );
}
