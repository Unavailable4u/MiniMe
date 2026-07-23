"use client";
import { Notebook, Search, ClipboardList, Hammer, FlaskConical, TrendingUp } from "lucide-react";

// NEW — item #2: single source of truth for each stage's icon + accent
// color. Previously this lived only inside ChatSidebar.jsx (as
// STAGE_ICON_MAP, uncolored) and nowhere else — Notebooks' own sidebar
// had an icon next to its section header but no color, every other
// tab's section header had no icon at all, and no tab's individual
// project rows had any icon. Extracted here so ChatSidebar and every
// stage tab (Notebooks/Research/Plan/Build/Test/Growth) render the same
// icon in the same color for a given stage, everywhere that stage shows
// up.
//
// Colors are deliberately six visually-distinct Tailwind accents, one
// per stage, chosen to stay legible against the app's dark neutral
// backgrounds: Notebooks=cyan and Research=red were specified in the
// design doc; Plan/Build/Test/Growth (violet/amber/emerald/fuchsia)
// were not specified, so these were chosen to round out a distinct,
// evenly-spaced set alongside cyan/red.
export const STAGE_THEME = {
  note: { Icon: Notebook, label: "Notebooks", color: "text-cyan-400" },
  research: { Icon: Search, label: "Research", color: "text-red-400" },
  plan: { Icon: ClipboardList, label: "Plan", color: "text-violet-400" },
  build: { Icon: Hammer, label: "Build", color: "text-amber-400" },
  test: { Icon: FlaskConical, label: "Test", color: "text-emerald-400" },
  growth: { Icon: TrendingUp, label: "Growth", color: "text-fuchsia-400" },
};

// Keys/order match AppShell.jsx's STAGE_TAB_MAP — same six promotable
// stages, "chat" excluded on purpose since a workspace's chat-of-origin
// isn't a stage tracked in active_stages.
export const STAGE_ORDER = ["note", "research", "plan", "build", "test", "growth"];

// Compact multi-icon badge cluster showing every stage a workspace/
// project is currently active in, each rendered in that stage's own
// color. Originally only rendered next to a project's row in the global
// Chat sidebar — item #2 now also renders this beside each project row
// in every stage tab's own sidebar, so a project's full stage footprint
// is visible no matter which tab you're looking at it from, not just
// Chat.
export default function WorkspaceStageIcons({ workspace, size = 10 }) {
  const activeStages = workspace.active_stages || [workspace.stage];
  const ordered = STAGE_ORDER.filter((s) => activeStages.includes(s));
  if (ordered.length === 0) return null;
  return (
    <span className="flex items-center gap-1 mr-1.5 shrink-0">
      {ordered.map((stage) => {
        const entry = STAGE_THEME[stage];
        if (!entry) return null;
        const { Icon, label, color } = entry;
        return <Icon key={stage} size={size} className={color} title={label} />;
      })}
    </span>
  );
}
