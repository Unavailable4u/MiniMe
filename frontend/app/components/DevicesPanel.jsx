// frontend/app/components/DevicesPanel.jsx
// Patch X01 (M2C build plan, WP X01 -- Devices page in the web app).
//
// The Settings > Devices section: pair a terminal by entering the code
// `m2c login` prints, see the devices paired to this account, revoke one.
// Talks to the S03 routes (backend/api/routes/devices.py); every response
// shape and error this relies on is written up in the header of
// lib/devices.js, which also holds all the logic that can be tested under
// plain node (code normalisation, list splitting, "last seen" wording, error
// text). This file is only state, fetches and markup.
//
// THE PAIRING LOOP, from this page's side:
//   1. The person runs `m2c login`; the terminal shows a code (BCDF-GHJK) and
//      polls the server every ~5 s.
//   2. They open Settings > Devices and type the code. POST /api/devices/approve
//      binds that terminal to this account and answers with the device's name,
//      OS and client version, which is shown back so they can see it is the
//      terminal they meant.
//   3. The device row only exists once the terminal's next poll collects its
//      credential (eo/devices.py poll_pairing creates it in the same
//      transaction), so the list is reloaded a few times in the ~15 s after an
//      approval instead of once; there is also a Refresh button.
//   4. Revoke asks for confirmation (the shared ConfirmDialog), then POSTs
//      /api/devices/{id}/revoke and drops the row. The list endpoint omits
//      revoked devices, and splitDevices() keeps them out even if it did not.
//
// WHAT THIS PAGE DOES NOT DO. There is no online/offline indicator: the API
// has no such field, only `last_seen_at` (refreshed about every 15 minutes
// while a client runs), so the page shows "Last seen" and nothing it would
// have to guess.
//
// SECRETS. The code is only ever sent as a POST body (never in a URL), is not
// logged, is cleared from the field once it has been used, and no error text
// echoes it (deviceErrorMessage returns fixed strings).
"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { Laptop, Loader2, RefreshCw } from "lucide-react";
import { useSession } from "../context/SessionContext";
import { authedFetch, authHeaders } from "../lib/authFetch";
import {
  USER_CODE_LENGTH,
  TRUST_STATEMENT,
  TRUST_STATEMENT_EXTRA,
  normalizeUserCode,
  isCompleteUserCode,
  splitDevices,
  describeDevice,
  describeApproval,
  parseRetryAfter,
  deviceErrorMessage,
} from "../lib/devices";
import ConfirmDialog from "./ConfirmDialog";

// The terminal polls every 5 s (POLL_INTERVAL_SECONDS in eo/devices.py), so
// the new device normally appears by the second of these.
const AFTER_APPROVE_REFRESH_MS = [2000, 6000, 12000];

// One authenticated request -> {ok, status, retryAfter, data}. Never throws:
// a network failure is status 0, which deviceErrorMessage words as such.
async function request(apiUrl, path, { method = "GET", body } = {}) {
  try {
    const res = await authedFetch(`${apiUrl}${path}`, {
      method,
      headers: await authHeaders({ json: body !== undefined }),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      // an empty or non-JSON body: the status is what matters
    }
    return {
      ok: res.ok,
      status: res.status,
      retryAfter: parseRetryAfter(res.headers.get("Retry-After")),
      data,
    };
  } catch {
    return { ok: false, status: 0, retryAfter: null, data: null };
  }
}

export default function DevicesPanel() {
  const { API_URL } = useSession();
  const [devices, setDevices] = useState(null); // null = loading the first time
  const [listError, setListError] = useState(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(null); // "approve" | "deny" | null
  const [notice, setNotice] = useState(null); // {kind: "success" | "error", text}
  const [pendingRevoke, setPendingRevoke] = useState(null); // the device row being confirmed
  const [revokingId, setRevokingId] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const loadSeq = useRef(0); // only the newest list response may set state
  const followUps = useRef([]); // pending post-approve reload timers
  const mounted = useRef(true);

  const loadList = useCallback(async ({ silent = false } = {}) => {
    const seq = ++loadSeq.current;
    const r = await request(API_URL, "/api/devices");
    if (!mounted.current || seq !== loadSeq.current) return;
    if (r.ok) {
      setDevices(splitDevices(r.data && r.data.devices).active);
      setListError(null);
    } else {
      // A quiet background refresh that fails keeps what is on screen; only a
      // load the person asked for (or the first one) reports the problem.
      if (!silent) setListError(deviceErrorMessage("list", r));
      setDevices((prev) => (prev === null ? [] : prev));
    }
  }, [API_URL]);

  useEffect(() => {
    mounted.current = true;
    loadList();
    return () => {
      mounted.current = false;
      followUps.current.forEach(clearTimeout);
      followUps.current = [];
    };
  }, [loadList]);

  function reloadAfterApproval() {
    followUps.current.forEach(clearTimeout);
    followUps.current = AFTER_APPROVE_REFRESH_MS.map((ms) =>
      setTimeout(() => loadList({ silent: true }), ms),
    );
    loadList({ silent: true });
  }

  async function handleRefresh() {
    setRefreshing(true);
    await loadList();
    if (mounted.current) setRefreshing(false);
  }

  const normalized = normalizeUserCode(code);
  const complete = isCompleteUserCode(normalized);

  async function submitCode(action) {
    if (!complete || busy) return;
    setBusy(action);
    setNotice(null);
    const r = await request(API_URL, `/api/devices/${action}`, {
      method: "POST",
      body: { user_code: normalized },
    });
    if (!mounted.current) return;
    if (r.ok) {
      setCode(""); // the code has been used; do not leave it on screen
      if (action === "approve") {
        setNotice({
          kind: "success",
          text: `${describeApproval(r.data)} It will appear in the list below once the terminal finishes signing in.`,
        });
        reloadAfterApproval();
      } else {
        setNotice({ kind: "success", text: "Code denied. The terminal was told the request was refused." });
      }
    } else {
      setNotice({ kind: "error", text: deviceErrorMessage(action, r) });
    }
    setBusy(null);
  }

  async function confirmRevoke() {
    const target = pendingRevoke;
    setPendingRevoke(null);
    if (!target) return;
    setRevokingId(target.id);
    setNotice(null);
    const r = await request(API_URL, `/api/devices/${encodeURIComponent(target.id)}/revoke`, { method: "POST" });
    if (!mounted.current) return;
    // A 404 means the device is already gone (revoked elsewhere, or not this
    // account's): either way it should not be in the list, so it is dropped
    // like a success, with the reason shown.
    if (r.ok || r.status === 404) {
      setDevices((prev) => (prev || []).filter((d) => d.id !== target.id));
      loadList({ silent: true });
    }
    if (!r.ok) setNotice({ kind: "error", text: deviceErrorMessage("revoke", r) });
    setRevokingId(null);
  }

  const rows = (devices || []).map((d) => describeDevice(d));

  return (
    <div className="space-y-4">
      {/* ---- Pair a device ---- */}
      <div className="space-y-2">
        <p className="text-xs text-[var(--neutral-300)]">Pair a device</p>
        <p className="text-[var(--neutral-600)] text-xs">
          Run <code>m2c login</code> in your terminal. It shows a code and waits.
          Enter that code here to approve the terminal. Codes last about 10 minutes.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submitCode("approve");
          }}
          className="flex items-center gap-2"
        >
          <input
            id="device-user-code"
            name="device-user-code"
            type="text"
            aria-label="Code from your terminal"
            placeholder="BCDF-GHJK"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoComplete="off"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            maxLength={32}
            disabled={busy !== null}
            className="flex-1 min-w-0 text-xs font-mono tracking-widest uppercase placeholder:normal-case bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 min-h-[var(--viewport-touch-target)] text-[var(--neutral-300)] disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={!complete || busy !== null}
            className="shrink-0 text-xs rounded-lg px-3 min-h-[var(--viewport-touch-target)] inline-flex items-center justify-center bg-[var(--accent)] text-[var(--accent-text)] font-medium disabled:opacity-50"
          >
            {busy === "approve" ? <Loader2 size={12} className="animate-spin" /> : "Approve"}
          </button>
        </form>
        <button
          type="button"
          disabled={!complete || busy !== null}
          onClick={() => submitCode("deny")}
          className="text-[10px] text-[var(--neutral-600)] hover:text-[var(--neutral-300)] underline disabled:opacity-50 disabled:no-underline"
        >
          {busy === "deny" ? "Denying…" : "Don't recognize this code? Deny it"}
        </button>
        <div aria-live="polite">
          {notice && (
            <p
              role={notice.kind === "error" ? "alert" : "status"}
              className={`text-xs ${notice.kind === "error" ? "text-red-400" : "text-emerald-500"}`}
            >
              {notice.text}
            </p>
          )}
        </div>
        {!complete && normalized.length > 0 && (
          <p className="text-[10px] text-[var(--neutral-600)]">
            {normalized.length < USER_CODE_LENGTH
              ? `${normalized.length} of ${USER_CODE_LENGTH} characters. Spaces and dashes are ignored.`
              : `A code has exactly ${USER_CODE_LENGTH} characters.`}
          </p>
        )}
      </div>

      {/* ---- Paired devices ---- */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-xs text-[var(--neutral-300)]">Paired devices</p>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshing || devices === null}
            aria-label="Refresh devices"
            className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] disabled:opacity-50 p-1"
          >
            <RefreshCw size={12} className={refreshing ? "animate-spin" : ""} />
          </button>
        </div>
        {devices === null ? (
          <p className="text-xs text-[var(--neutral-600)]">Loading devices…</p>
        ) : listError ? (
          <div className="space-y-1">
            <p role="alert" className="text-xs text-red-400">{listError}</p>
            <button
              type="button"
              onClick={handleRefresh}
              className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)] border border-[var(--neutral-800)] rounded-lg px-3 min-h-[var(--viewport-touch-target)] inline-flex items-center"
            >
              Try again
            </button>
          </div>
        ) : rows.length === 0 ? (
          <p className="text-xs text-[var(--neutral-600)]">
            No devices paired yet. Run <code>m2c login</code> in a terminal, then enter its code above.
          </p>
        ) : (
          <ul className="space-y-2">
            {rows.map((d) => {
              const revoking = revokingId === d.id;
              const detail = [d.os, d.version && `version ${d.version}`].filter(Boolean).join(", ");
              return (
                <li
                  key={d.id}
                  className="flex items-center justify-between gap-3 border border-[var(--neutral-800)] rounded-lg px-3 py-2"
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Laptop size={14} className="text-[var(--neutral-500)] shrink-0" />
                    <div className="min-w-0">
                      <p className="text-xs text-[var(--neutral-300)] truncate">{d.name}</p>
                      {detail && <p className="text-[10px] text-[var(--neutral-600)] truncate">{detail}</p>}
                      <p className="text-[10px] text-[var(--neutral-600)]">{d.lastSeenLabel}</p>
                    </div>
                  </div>
                  <button
                    type="button"
                    disabled={revoking}
                    onClick={() => setPendingRevoke(d)}
                    className="shrink-0 text-xs rounded-lg px-3 py-1.5 text-[var(--neutral-500)] hover:text-[var(--neutral-300)] border border-[var(--neutral-800)] disabled:opacity-50"
                  >
                    {revoking ? <Loader2 size={12} className="animate-spin" /> : "Revoke"}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* ---- What a paired device can and cannot do, and where data lives ---- */}
      <div className="space-y-2 text-xs text-[var(--neutral-600)]">
        <p className="text-[var(--neutral-300)]">What a paired device can and can&apos;t do</p>
        <ul className="list-disc pl-4 space-y-1">
          <li>It signs in as you and uses your plan&apos;s shared usage limit.</li>
          <li>
            It works on the projects you run M2C in: reading files, proposing changes and
            running commands there, with the approvals M2C asks you for.
          </li>
          <li>It can&apos;t reach files outside the folders it is allowed to use, and it can&apos;t skip an approval.</li>
          <li>Pairing doesn&apos;t let this website read files on the device.</li>
          <li>It needs an internet connection and your account. There is no offline mode.</li>
        </ul>
        <p className="text-[var(--neutral-300)] pt-1">Where your data lives</p>
        <p>{TRUST_STATEMENT}</p>
        <p>{TRUST_STATEMENT_EXTRA}</p>
      </div>

      <ConfirmDialog
        open={pendingRevoke !== null}
        title="Revoke this device?"
        message={`${pendingRevoke ? describeDevice(pendingRevoke).name : "This device"} will be signed out and won't be able to connect again. To use it again, pair it again with m2c login.`}
        confirmLabel="Revoke"
        tone="danger"
        onConfirm={confirmRevoke}
        onCancel={() => setPendingRevoke(null)}
      />
    </div>
  );
}
