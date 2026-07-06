"use client";
import RoutingTraceCard from "./RoutingTraceCard";
import AgentStepList from "./AgentStepList";

export default function MessageBubble({ message }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="bg-neutral-800 rounded-lg px-3 py-2 text-sm max-w-[80%]">
          {message.text}
        </div>
      </div>
    );
  }

  const { data, steps } = message;
  return (
    <div className="flex justify-start">
      <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm max-w-[80%] space-y-1">
        <div className="text-xs text-neutral-500">
          tier {data.tier} · {data.status}
        </div>
        <RoutingTraceCard decision={data.decision} />
        <AgentStepList steps={steps} />
        <ResultBody data={data} />
      </div>
    </div>
  );
}

function ResultBody({ data }) {
  if (data.status === "error" || data.message) {
    return <div className="text-red-400">{data.message}</div>;
  }
  if (data.tier === "sga" || data.tier === "cache") {
    return <div>{data.result?.answer}</div>;
  }
  if (data.tier === 0) {
    return <div>{data.result?.answer}</div>;
  }
  if (data.tier === 1) {
    return (
      <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
        {data.result?.code}
      </pre>
    );
  }
  if (data.tier === 2) {
    return (
      <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
        {JSON.stringify(data.result?.output, null, 2)}
      </pre>
    );
  }
  return (
    <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}
