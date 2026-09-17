"use client";
import { useEffect, useRef } from "react";
import { Menu, PanelRightOpen } from "lucide-react";

// Mobile counterpart of ../AppShell.jsx's <header>. ../AppShell.jsx
// still owns every bit of state this needs (activeTab, TABS, the
// sidebar/working-panel open flags) — this component only renders the
// Grok-style bar (hamburger — horizontally-scrolling tab strip — panel
// toggle) and calls back up through the handlers it's given. Split out
// into its own file, rather than ../AppShell.jsx branching its header
// JSX inline, because the interaction model here is genuinely different
// (drawer triggers instead of a persistent sidebar; a scrolling strip
// showing ~3 tabs at a time instead of a row sized to fit all 12) —
// see this folder's README for that cosmetic-vs-structural distinction
// in more detail.
//
// Only the hamburger and the panel-toggle are icon buttons with no
// label — same reasoning Grok's own mobile bar has room for exactly
// those two and nothing else: everything else that lives in the
// desktop header's `ml-auto` cluster (NotificationBell, AccountMenu,
// WorkspaceDataBubble) has to go somewhere else on a phone-width
// screen. Bell + account move into mobile/ChatSidebar.jsx's drawer
// header instead (reachable via the hamburger); WorkspaceDataBubble
// has no mobile home yet — that's a known Phase 1 gap, not an
// oversight, flagged for a follow-up phase rather than silently
// dropped.
export default function MobileHeader({
  tabs,
  activeTab,
  onSelectTab,
  showSidebarButton,
  onOpenSidebar,
  showWorkingPanelButton,
  onOpenWorkingPanel,
}) {
  const scrollerRef = useRef(null);
  const buttonRefs = useRef({});

  // Keep the active tab's button in view when activeTab changes from
  // somewhere OTHER than a direct tap in this strip — e.g.
  // NotificationBell's "open this chat" jump, or handlePromoted()
  // landing on a different tab after a promote action.
  useEffect(() => {
    const btn = buttonRefs.current[activeTab];
    const scroller = scrollerRef.current;
    if (!btn || !scroller) return;
    const btnRect = btn.getBoundingClientRect();
    const scrollerRect = scroller.getBoundingClientRect();
    if (btnRect.left < scrollerRect.left || btnRect.right > scrollerRect.right) {
      btn.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    }
  }, [activeTab]);

  return (
    <header className="flex items-center h-12 border-b border-[var(--neutral-800)] px-1">
      <div className="w-10 shrink-0 flex items-center justify-center">
        {showSidebarButton ? (
          <button
            onClick={onOpenSidebar}
            title="Chats"
            className="text-[var(--neutral-300)] hover:text-white p-2 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
          >
            <Menu size={18} />
          </button>
        ) : (
          // Empty same-size slot so the tab strip doesn't jump/recenter
          // when a tab with no sidebar (e.g. Settings) is active.
          <span aria-hidden="true" />
        )}
      </div>

      <nav
        ref={scrollerRef}
        className="mobile-tab-scroll flex-1 min-w-0 flex gap-1 overflow-x-auto snap-x snap-mandatory px-1"
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              buttonRefs.current[t.id] = el;
            }}
            onClick={() => onSelectTab(t.id)}
            className={`shrink-0 snap-start whitespace-nowrap truncate max-w-[110px] text-xs rounded-lg px-3 py-1.5 transition-colors ${
              activeTab === t.id
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="w-10 shrink-0 flex items-center justify-center">
        {showWorkingPanelButton ? (
          <button
            onClick={onOpenWorkingPanel}
            title="Working Panel"
            className="text-[var(--neutral-300)] hover:text-white p-2 rounded-md hover:bg-[var(--neutral-900)] transition-colors"
          >
            <PanelRightOpen size={18} />
          </button>
        ) : (
          <span aria-hidden="true" />
        )}
      </div>
    </header>
  );
}
