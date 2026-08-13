"use client";
import { useEffect, useRef, useState, useCallback } from "react";
import { authHeaders } from "../context/SessionContext";
import { getPusherClient } from "../lib/pusherClient";
import { Play, Loader2 } from "lucide-react";

/**
 * F2 Part 7 — real terminal, last piece of the F2 build. Renders
 * daemon/tools.py's execute_command output live via xterm.js, using
 * Part 4's existing propose/confirm HTTP surface (there is no separate
 * "run" endpoint -- typing a command and hitting Enter here calls the
 * exact same POST .../local/propose the F2 plan always specified for
 * execute_command, then waits for a human to hit Confirm on
 * PendingActionBar, same as an agent-proposed command would). What's
 * new in Part 7 specifically:
 *   - daemon/tools.py's execute_command now streams (on_chunk callback,
 *     one call per output line) instead of only returning once the
 *     whole command finishes.
 *   - daemon/connection.py forwards each chunk as a
 *     {"type": "tool_stream", ...} message the instant it's produced.
 *   - eo/local_workspace.py forwards those onto the workspace's Pusher
 *     channel as local_tool_stream_chunk events.
 *   - This component subscribes to that channel and writes each chunk
 *     into the terminal as it arrives.
 * The confirm step itself (still required -- see daemon/tools.py's
 * execute_command docstring on why this tool never runs freely) is
 * PendingActionBar's job, not this component's; this component only
 * proposes and renders. A local `runs` map keyed by action_id is what
 * lets it tell "an action_id I proposed" apart from "some other
 * proposal on this workspace channel I have no terminal line for" --
 * the stream/confirmed/result events for every OTHER pending action
 * (e.g. a write_file/delete a person is confirming from the Files
 * view, or a command proposed by an agent) are simply ignored here.
 *
 * Loads @xterm/xterm dynamically (no SSR: it touches `window`/`document`
 * directly), same "browser-only library" handling
 * MechView.jsx already does for three.js's OrbitControls-adjacent bits.
 *
 * Place this file at: frontend/app/components/TerminalPanel.jsx
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function apiPost(path, body) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

const PROMPT = "\x1b[38;5;245m$\x1b[0m ";

function TerminalPanel({ workspaceId, live }) {
  const containerRef = useRef(null);
  const termRef = useRef(null);
  const fitRef = useRef(null);
  const inputBufRef = useRef("");
  const runsRef = useRef(new Map()); // action_id -> true, for commands THIS component proposed
  const [ready, setReady] = useState(false);
  const [pending, setPending] = useState(false); // a propose is in flight, or a proposed run hasn't been confirmed/denied yet

  // --- xterm setup -------------------------------------------------
  useEffect(() => {
    let disposed = false;
    let term;
    let fit;
    (async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed) return;
      await import("@xterm/xterm/css/xterm.css");

      term = new Terminal({
        convertEol: true,
        fontSize: 12,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        theme: {
          background: "#0a0a0a",
          foreground: "#d4d4d4",
          cursor: "#d4d4d4",
        },
        cursorBlink: true,
        disableStdin: false,
      });
      fit = new FitAddon();
      term.loadAddon(fit);
      term.open(containerRef.current);
      fit.fit();
      term.writeln("MiniMe local terminal -- commands run on your paired machine.");
      term.writeln("Every command requires a Confirm click (see the bar above) before it runs.\r\n");
      term.write(PROMPT);

      termRef.current = term;
      fitRef.current = fit;
      setReady(true);
    })();

    const onResize = () => fitRef.current?.fit();
    window.addEventListener("resize", onResize);

    return () => {
      disposed = true;
      window.removeEventListener("resize", onResize);
      termRef.current?.dispose();
      termRef.current = null;
      fitRef.current = null;
      setReady(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const proposeCommand = useCallback(async (command) => {
    const term = termRef.current;
    if (!command.trim()) {
      term.write(`\r\n${PROMPT}`);
      return;
    }
    setPending(true);
    try {
      const action = await apiPost(`/api/workspaces/${workspaceId}/local/propose`, {
        tool: "execute_command",
        params: { command },
      });
      runsRef.current.set(action.action_id, true);
      term.writeln("");
      term.writeln(`\x1b[38;5;178mProposed -- confirm above to run:\x1b[0m ${command}`);
    } catch (e) {
      term.writeln("");
      term.writeln(`\x1b[38;5;196mCouldn't propose command: ${e.message}\x1b[0m`);
      term.write(PROMPT);
      setPending(false);
    }
  }, [workspaceId]);

  // --- keyboard input: builds a line, proposes it on Enter ---------
  useEffect(() => {
    const term = termRef.current;
    if (!ready || !term) return undefined;

    const disposable = term.onData((data) => {
      if (pending) return; // one in-flight proposal at a time -- keeps runsRef unambiguous
      const code = data.charCodeAt(0);
      if (data === "\r") {
        const command = inputBufRef.current;
        inputBufRef.current = "";
        proposeCommand(command);
        return;
      }
      if (code === 127) { // backspace
        if (inputBufRef.current.length > 0) {
          inputBufRef.current = inputBufRef.current.slice(0, -1);
          term.write("\b \b");
        }
        return;
      }
      if (code < 32) return; // ignore other control chars (arrow keys, etc.) -- no history/editing in this first cut
      inputBufRef.current += data;
      term.write(data);
    });

    return () => disposable.dispose();
  }, [ready, pending, proposeCommand]);

  // --- live output: stream chunks + final result --------------------
  useEffect(() => {
    if (!workspaceId) return undefined;
    const pusher = getPusherClient();
    if (!pusher) return undefined;

    const channelName = `workspace-${workspaceId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);

    const handler = (eventType, data) => {
      const term = termRef.current;
      if (!term) return;
      const payload = data?.payload || {};
      const actionId = payload.action_id;

      if (eventType === "local_tool_stream_chunk") {
        if (!actionId || !runsRef.current.has(actionId)) return; // not a run this terminal started
        const prefix = payload.stream === "stderr" ? "\x1b[38;5;203m" : "";
        const suffix = payload.stream === "stderr" ? "\x1b[0m" : "";
        term.write(`${prefix}${(payload.chunk || "").replace(/\n/g, "\r\n")}${suffix}`);
        return;
      }
      if (eventType === "local_tool_denied" && actionId && runsRef.current.has(actionId)) {
        runsRef.current.delete(actionId);
        term.writeln("");
        term.writeln("\x1b[38;5;245m(denied -- not run)\x1b[0m");
        term.write(PROMPT);
        setPending(false);
        return;
      }
      if (eventType === "local_tool_result" && actionId && runsRef.current.has(actionId)) {
        runsRef.current.delete(actionId);
        const exitOk = payload.ok !== false;
        term.writeln("");
        term.writeln(
          exitOk
            ? "\x1b[38;5;108m(command finished)\x1b[0m"
            : `\x1b[38;5;196m(command failed: ${payload.error || "unknown error"})\x1b[0m`
        );
        term.write(PROMPT);
        setPending(false);
      }
    };
    channel.bind_global(handler);

    return () => {
      channel.unbind_global(handler);
      pusher.unsubscribe(channelName);
    };
  }, [workspaceId]);

  return (
    <div className="h-full flex flex-col min-h-0 bg-[#0a0a0a]">
      <div className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 border-b border-[var(--neutral-800)] text-[10px] text-[var(--neutral-600)]">
        <Play size={11} />
        <span>Type a command and press Enter -- it runs only after you Confirm it above.</span>
        {pending && <Loader2 size={11} className="animate-spin ml-1" />}
        {!live && <span className="ml-auto text-amber-600">No daemon connected</span>}
      </div>
      <div className="flex-1 min-h-0 px-2 py-1.5" ref={containerRef} />
    </div>
  );
}

export default TerminalPanel;
