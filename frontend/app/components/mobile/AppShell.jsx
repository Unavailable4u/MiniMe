"use client";
import { useEffect, useRef, useState } from "react";
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
//
// CHANGED — carousel behavior: the strip used to just be a plain
// horizontally-scrolling row (snap-start, i.e. each tab snapped to the
// LEFT edge as it scrolled into view) that only ever recentered the
// active tab programmatically (NotificationBell's "open this chat"
// jump, handlePromoted() landing elsewhere) — a manual swipe never
// changed activeTab at all, so getting past the first 2-3 tabs meant
// scrolling one-handed AND still tapping the label once it arrived.
// Below now behaves like a real carousel: every button snaps to the
// CENTER of the strip (snap-center, not snap-start), phantom spacer
// elements before the first/after the last tab make it possible for
// those two to reach center at all (without them there's simply no
// scroll room left/right of the ends), and a scroll-settle listener
// promotes whichever button ends up nearest center to activeTab —
// so a swipe left/right *is* how you change tabs now, not just how
// you reveal a label to tap. Tapping a button directly still works
// exactly as before (onSelectTab fires immediately, no need to wait
// for the swipe to settle).
export default function MobileHeader({
  tabs,
  activeTab,
  onSelectTab,
  showSidebarButton,
  onOpenSidebar,
  sidebarLabel = "Chats", // NEW — picker-drawer generalization: Notebooks/Research/etc pass their own noun instead of the Chat-only default
  showWorkingPanelButton,
  onOpenWorkingPanel,
}) {
  const scrollerRef = useRef(null);
  const buttonRefs = useRef({});
  // Width of the leading/trailing spacer, computed so the first and
  // last tab buttons each have exactly as much empty scroll room on
  // their outer side as every other tab has on both sides — i.e. just
  // enough for either one to land dead-center too. Recomputed whenever
  // the strip resizes (rotation, browser chrome show/hide) or the tab
  // set itself changes.
  const [edgePad, setEdgePad] = useState({ start: 0, end: 0 });
  // Guards against the settle-scroll listener re-firing onSelectTab
  // for a centering scroll THIS component just issued itself (tap, or
  // an external activeTab change) — that scroll already lands on the
  // right tab, so treating its own settle as a fresh "user swiped"
  // signal would be redundant, not wrong, but there's no reason to
  // call onSelectTab a second time for a tab that's already active.
  const programmaticScrollRef = useRef(false);

  useEffect(() => {
    function recomputeEdgePad() {
      const scroller = scrollerRef.current;
      const firstBtn = buttonRefs.current[tabs[0]?.id];
      const lastBtn = buttonRefs.current[tabs[tabs.length - 1]?.id];
      if (!scroller || !firstBtn || !lastBtn) return;
      const half = scroller.clientWidth / 2;
      setEdgePad({
        start: Math.max(0, half - firstBtn.offsetWidth / 2),
        end: Math.max(0, half - lastBtn.offsetWidth / 2),
      });
    }
    recomputeEdgePad();
    window.addEventListener("resize", recomputeEdgePad);
    return () => window.removeEventListener("resize", recomputeEdgePad);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.map((t) => t.id).join("|")]);

  // Keep the active tab's button centered whenever activeTab changes —
  // from a direct tap in this strip, from a swipe settling on a new
  // tab (below), or from somewhere else entirely (NotificationBell's
  // "open this chat" jump, handlePromoted() landing on a different
  // tab). Unconditional (not just "if it's scrolled out of view") so a
  // tap on a tab that's already fully visible but off-center still
  // glides to the middle instead of staying wherever it happened to be
  // sitting in the strip.
  useEffect(() => {
    const btn = buttonRefs.current[activeTab];
    const scroller = scrollerRef.current;
    if (!btn || !scroller) return;
    programmaticScrollRef.current = true;
    btn.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    // Scroll-snap settle time is short; clear the guard generously
    // after it so a real subsequent swipe is never mistaken for the
    // tail end of this programmatic one.
    const clear = setTimeout(() => {
      programmaticScrollRef.current = false;
    }, 400);
    return () => clearTimeout(clear);
  }, [activeTab]);

  // Carousel core: once the strip stops moving, find whichever button
  // is nearest the strip's horizontal center and — if it isn't already
  // the active tab — promote it. This is what makes "swipe left" go to
  // the tab now centered rather than just revealing it unselected.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    let settleTimer;

    function handleScroll() {
      clearTimeout(settleTimer);
      settleTimer = setTimeout(() => {
        if (programmaticScrollRef.current) return;
        const scrollerRect = scroller.getBoundingClientRect();
        const centerX = scrollerRect.left + scrollerRect.width / 2;
        let nearestId = null;
        let nearestDist = Infinity;
        for (const t of tabs) {
          const btn = buttonRefs.current[t.id];
          if (!btn) continue;
          const btnRect = btn.getBoundingClientRect();
          const dist = Math.abs(btnRect.left + btnRect.width / 2 - centerX);
          if (dist < nearestDist) {
            nearestDist = dist;
            nearestId = t.id;
          }
        }
        if (nearestId && nearestId !== activeTab) {
          onSelectTab(nearestId);
        }
      }, 120); // roughly one frame past when scroll-snap settles
    }

    scroller.addEventListener("scroll", handleScroll, { passive: true });
    return () => {
      scroller.removeEventListener("scroll", handleScroll);
      clearTimeout(settleTimer);
    };
  }, [tabs, activeTab, onSelectTab]);

  return (
    <header className="flex items-center h-12 border-b border-[var(--neutral-800)] px-1">
      <div className="w-10 shrink-0 flex items-center justify-center">
        {showSidebarButton ? (
          <button
            onClick={onOpenSidebar}
            title={sidebarLabel}
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
        className="mobile-tab-scroll flex-1 min-w-0 flex items-center gap-1 overflow-x-auto snap-x snap-mandatory px-1 [overscroll-behavior-x:contain]"
      >
        {/* Leading phantom spacer — see edgePad comment above. */}
        <span aria-hidden="true" className="shrink-0" style={{ width: edgePad.start }} />
        {tabs.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              buttonRefs.current[t.id] = el;
            }}
            onClick={() => onSelectTab(t.id)}
            className={`shrink-0 snap-center whitespace-nowrap truncate max-w-[110px] text-xs rounded-lg px-3 py-1.5 transition-colors ${
              activeTab === t.id
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            {t.label}
          </button>
        ))}
        {/* Trailing phantom spacer — mirrors the leading one so the last
            tab can reach center too. */}
        <span aria-hidden="true" className="shrink-0" style={{ width: edgePad.end }} />
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
