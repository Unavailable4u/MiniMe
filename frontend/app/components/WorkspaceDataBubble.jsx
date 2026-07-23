"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useSession } from "../context/SessionContext";
import { Database, PanelRightClose, ChevronDown, Loader2 } from "lucide-react";

function formatValue(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((item) => formatValue(item)).filter(Boolean).join(", ");
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function formatLedgerStamp(at) {
  if (!at) return "";
  try {
    return new Date(at).toLocaleString();
  } catch {
    return String(at);
  }
}

export default function WorkspaceDataBubble({
  workspaceId,
  workspaceName,
  storageKey,
  title = "Data bubble",
}) {
  const { fetchWorkspaceFacts } = useSession();
  const [collapsed, setCollapsed] = useState(true);
  const [loading, setLoading] = useState(false);
  const [facts, setFacts] = useState(null);
  const containerRef = useRef(null);
  // NEW — item #8 fix: guards against an in-flight fetch resolving
  // after a newer one was kicked off (e.g. a visibility-triggered
  // refetch landing while the mount fetch is still pending).
  const requestSeqRef = useRef(0);

  useEffect(() => {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) setCollapsed(saved === "1");
  }, [storageKey]);

  function loadFacts(id) {
    if (!id) return;
    const seq = ++requestSeqRef.current;
    setLoading(true);
    fetchWorkspaceFacts(id)
      .then((next) => {
        if (seq === requestSeqRef.current) setFacts(next || null);
      })
      .finally(() => {
        if (seq === requestSeqRef.current) setLoading(false);
      });
  }

  useEffect(() => {
    if (!workspaceId) {
      requestSeqRef.current += 1; // invalidate any in-flight fetch
      setFacts(null);
      setLoading(false);
      return;
    }
    loadFacts(workspaceId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  // NEW — item #8 fix: AppShell keeps every visited tab mounted
  // (display:none instead of unmounting) rather than remounting it,
  // so simply revisiting a tab does not rerun the [workspaceId] effect
  // above, even though the facts may have changed while this tab was
  // hidden. A tab regaining focus flips its wrapper from
  // display:none -> display:contents, which removes/restores this
  // node's layout box — IntersectionObserver reports that exact
  // transition as isIntersecting going false -> true. We skip the
  // very first callback (it just reports the initial mount, already
  // covered by the effect above) and refetch on every later
  // "became visible again" transition.
  useEffect(() => {
    if (!workspaceId || !containerRef.current) return;
    let firstCallback = true;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (firstCallback) {
          firstCallback = false;
          return;
        }
        if (entry.isIntersecting) loadFacts(workspaceId);
      },
      { threshold: 0 }
    );
    observer.observe(containerRef.current);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  function toggle() {
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem(storageKey, next ? "1" : "0");
      return next;
    });
  }

  const sectionRows = useMemo(() => {
    const sections = facts?.sections || {};
    return Object.entries(sections)
      .filter(([, bucket]) => bucket && Array.isArray(bucket.order) && bucket.order.length > 0)
      .map(([sectionName, bucket]) => ({ sectionName, bucket }));
  }, [facts]);

  if (!workspaceId) return null;

  // NEW — items #5/#13: this used to float over the tab's own content
  // (`absolute top-3 right-3` against a tab-level `relative` wrapper).
  // Now it mounts in the top nav next to the notification bell instead,
  // so the outer node just needs to anchor its own dropdown panel, not
  // the whole page. AppShell positions the trigger itself; this wrapper
  // only needs `relative` so the panel below can hang off it.
  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={toggle}
        className={`flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs shadow-lg backdrop-blur-sm transition-colors ${
          collapsed
            ? "border-[var(--neutral-800)] bg-[var(--neutral-950-a90)] text-[var(--neutral-300)] hover:text-[var(--neutral-100)]"
            : "border-[var(--accent)] bg-[var(--neutral-950-a95)] text-[var(--accent-text)]"
        }`}
      >
        <Database size={13} />
        <span>{title}</span>
        {workspaceName && <span className="max-w-[8rem] truncate text-[10px] opacity-75">{workspaceName}</span>}
        <ChevronDown size={12} className={`transition-transform ${collapsed ? "-rotate-90" : "rotate-0"}`} />
      </button>

      {!collapsed && (
        <div className="absolute right-0 top-full z-30 mt-2 w-[min(22rem,calc(100vw-1.5rem))] rounded-xl border border-[var(--neutral-800)] bg-[var(--neutral-950)]/96 shadow-2xl backdrop-blur-sm">
          <div className="flex items-center justify-between gap-2 border-b border-[var(--neutral-800)] px-3 py-2">
            <div className="min-w-0">
              <p className="text-xs font-medium text-[var(--neutral-200)]">{title}</p>
              <p className="text-[11px] text-[var(--neutral-500)] truncate">{workspaceName || workspaceId}</p>
            </div>
            <button
              type="button"
              onClick={toggle}
              className="rounded-md p-1 text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
              title="Collapse data bubble"
            >
              <PanelRightClose size={14} />
            </button>
          </div>

          <div className="max-h-[min(72vh,42rem)] overflow-y-auto px-3 py-3 text-[11px] leading-relaxed text-[var(--neutral-300)] space-y-3">
            {loading && (
              <div className="flex items-center gap-1.5 text-[var(--neutral-500)]">
                <Loader2 size={12} className="animate-spin" />
                Loading facts…
              </div>
            )}

            {!loading && facts && (
              <>
                <section className="space-y-1.5">
                  <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Overview</p>
                  {facts.brand_voice ? <p><span className="text-[var(--neutral-500)]">Brand voice:</span> {facts.brand_voice}</p> : null}
                  {facts.target_user ? <p><span className="text-[var(--neutral-500)]">Target user:</span> {facts.target_user}</p> : null}
                  {Array.isArray(facts.tech_stack) && facts.tech_stack.length > 0 ? (
                    <p><span className="text-[var(--neutral-500)]">Tech stack:</span> {facts.tech_stack.join(", ")}</p>
                  ) : null}
                  {facts.custom && Object.keys(facts.custom).length > 0 ? (
                    <div className="space-y-0.5">
                      <p className="text-[var(--neutral-500)]">Custom</p>
                      <div className="space-y-0.5 pl-2 border-l border-[var(--neutral-800)]">
                        {Object.entries(facts.custom).map(([key, value]) => (
                          <p key={key} className="break-words">
                            <span className="text-[var(--neutral-500)]">{key}:</span> {formatValue(value)}
                          </p>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </section>

                <section className="space-y-2">
                  <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Sections</p>
                  {sectionRows.length === 0 ? (
                    <p className="text-[var(--neutral-500)]">No structured sections yet.</p>
                  ) : (
                    <div className="space-y-2">
                      {sectionRows.map(({ sectionName, bucket }) => {
                        const entries = bucket.entries || {};
                        const orderedKeys = Array.isArray(bucket.order) ? bucket.order : [];
                        return (
                          <div key={sectionName} className="rounded-lg border border-[var(--neutral-800)] px-2.5 py-2 space-y-1">
                            <p className="font-medium text-[var(--neutral-200)]">{sectionName}</p>
                            <div className="space-y-1 pl-2 border-l border-[var(--neutral-800)]">
                              {orderedKeys.slice(0, 4).map((key) => {
                                const entry = entries[key];
                                if (!entry) return null;
                                return (
                                  <div key={key} className="space-y-0.5">
                                    <p className="text-[var(--neutral-400)] break-words">
                                      <span className="text-[var(--neutral-500)]">{entry.title || key}:</span> {entry.summary || entry.text || key}
                                    </p>
                                    {entry.text && entry.text !== entry.summary ? (
                                      <p className="text-[var(--neutral-500)] whitespace-pre-wrap break-words">{entry.text}</p>
                                    ) : null}
                                  </div>
                                );
                              })}
                              {orderedKeys.length > 4 && <p className="text-[var(--neutral-600)]">+{orderedKeys.length - 4} more</p>}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>

                <section className="space-y-2">
                  <p className="text-[10px] uppercase tracking-wide text-[var(--neutral-600)]">Ledger</p>
                  {Array.isArray(facts.ledger) && facts.ledger.length > 0 ? (
                    <div className="space-y-1.5">
                      {facts.ledger.slice(-6).reverse().map((entry) => (
                        <div key={entry.event_id || `${entry.at}-${entry.key}`} className="rounded-lg border border-[var(--neutral-800)] px-2.5 py-2 space-y-0.5">
                          <p className="text-[var(--neutral-500)]">
                            {formatLedgerStamp(entry.at)} · {entry.event || "update"} · {entry.section || "section"}/{entry.key || "item"}
                          </p>
                          <p className="text-[var(--neutral-300)] break-words">{entry.summary || entry.title || entry.key || "(no summary)"}</p>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-[var(--neutral-500)]">No ledger entries yet.</p>
                  )}
                </section>
              </>
            )}

            {!loading && !facts && <p className="text-[var(--neutral-500)]">No facts available.</p>}
          </div>
        </div>
      )}
    </div>
  );
}