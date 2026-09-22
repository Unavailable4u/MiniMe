"use client";
// frontend/app/hooks/useDaemonStatus.js — W3.1 (Build Workbench plan).
// Ported from components/tabs/LocalWorkspaceTab.jsx's own inline
// STATUS_POLL_MS/fetchStatus (see that file's header) so
// EditorWorkbench.jsx's Local source can poll the same way without
// duplicating the fetch — a short interval here is fine because
// api/routes/local_workspace.py's local_status() docstring calls it
// out explicitly: "a registry lookup, not a daemon round-trip".
import { useEffect, useRef, useState } from "react";
import { authHeaders } from "../context/SessionContext";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const STATUS_POLL_MS = 5000;

async function fetchStatus(wsId) {
  const res = await fetch(`${API_URL}/api/workspaces/${wsId}/local/status`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return { live: false };
  return res.json();
}

/**
 * @param {string|null} workspaceId - pass null to stop polling (e.g.
 *   Cloud is the active source right now — there's nothing to check)
 * @param {(nowLive: boolean) => void} [onLiveChange] - fires only on a
 *   false -> true transition, the "the daemon just came up, go fetch
 *   the tree" moment LocalWorkspaceTab.jsx's own status effect used
 *   inline. Never fires on a live -> false transition — there's nothing
 *   useful to *do* the instant a daemon drops, only something to *show*
 *   (the `live` flag this hook already returns).
 * @returns {{live: boolean, checked: boolean}} `checked` is false until
 *   the first poll resolves, so callers can avoid a "not connected"
 *   flash before they actually know one way or the other.
 */
export function useDaemonStatus(workspaceId, onLiveChange) {
  const [live, setLive] = useState(false);
  const [checked, setChecked] = useState(false);
  const onLiveChangeRef = useRef(onLiveChange);
  onLiveChangeRef.current = onLiveChange;

  useEffect(() => {
    setLive(false);
    setChecked(false);
    if (!workspaceId) return undefined;
    let cancelled = false;

    const check = async () => {
      const status = await fetchStatus(workspaceId);
      if (cancelled) return;
      setChecked(true);
      setLive((prevLive) => {
        const nowLive = !!status.live;
        if (!prevLive && nowLive) onLiveChangeRef.current?.(nowLive);
        return nowLive;
      });
    };

    check();
    const interval = setInterval(check, STATUS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [workspaceId]);

  return { live, checked };
}
