// frontend/app/hooks/useIsVisible.js
// FIX — Recharts' ResponsiveContainer measures its container synchronously
// on mount. AppShell.jsx keeps every visited tab mounted forever (just
// toggling display:none instead of unmounting — see AppShell.jsx's own
// comment on this), and display:none collapses ALL descendant layout to
// 0x0 regardless of any explicit width/height styles set on them. Any
// chart that (re)renders while its tab is hidden — including on the
// initial mount of a previously-visited tab, not just live tab switches —
// hits that 0x0 measurement and logs Recharts' "width(0) and height(0)"
// warning.
//
// IntersectionObserver correctly reports isIntersecting: false for
// anything inside a display:none ancestor (unlike a plain resize/mount
// check), so it's a reliable, self-contained way for a chart to know
// "am I actually being shown right now" without AppShell having to thread
// activeTab down through every tab's component tree just for this.
"use client";
import { useEffect, useRef, useState } from "react";

export function useIsVisible() {
  const ref = useRef(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => setVisible(entry.isIntersecting),
      { threshold: 0 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return [ref, visible];
}
