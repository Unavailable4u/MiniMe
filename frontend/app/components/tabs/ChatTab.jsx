"use client";
import { useRef, useEffect } from "react";
import { useSession } from "../../context/SessionContext";
import MessageBubble from "../MessageBubble";
import LiveActivity from "../LiveActivity";

export default function ChatTab() {
  const {
    messages, loading, sendTask, mode, setMode,
    liveDecision, liveSteps, routeTrace, dependencyMap, structurePlan,
  } = useSession();
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function handleSubmit(e) {
    e.preventDefault();
    const text = inputRef.current.value.trim();
    if (!text || loading) return;
    inputRef.current.value = "";
    sendTask(text);
  }

  return (
    <div className="flex flex-col h-full max-w-3xl mx-auto">
      <div className="flex-1 overflow-y-auto px-4 py-6 space-y-4">
        {messages.length === 0 && (
          <p className="text-neutral-500 text-sm">
            Send a task — the EO layer will classify it and route it through
            the appropriate tier.
          </p>
        )}
        {messages.map((m, i) => (
          <MessageBubble key={i} message={m} />
        ))}
        {loading && (
          <LiveActivity
            decision={liveDecision}
            steps={liveSteps}
            routeTrace={routeTrace}
            dependencyMap={dependencyMap}
            structurePlan={structurePlan}
          />
        )}
        <div ref={bottomRef} />
      </div>
      <form onSubmit={handleSubmit} className="border-t border-neutral-800 p-4 flex gap-2">
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          disabled={loading}
          className="bg-neutral-900 border border-neutral-800 rounded-lg px-2 py-2 text-sm outline-none"
        >
          <option value="auto">Auto</option>
          <option value="simple">Simple</option>
          <option value="fast">Fast</option>
          <option value="expert">Expert</option>
          <option value="beast">Beast</option>
        </select>
        <input
          ref={inputRef}
          placeholder="Describe a task..."
          disabled={loading}
          className="flex-1 bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm outline-none focus:border-neutral-600 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading}
          className="bg-neutral-100 text-neutral-900 rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
