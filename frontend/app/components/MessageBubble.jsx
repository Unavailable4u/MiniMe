"use client";
import Markdown from "./Markdown";

export default function MessageBubble({ message }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        {/* whitespace-pre-wrap so a multiline/indented user message (e.g.
            pasted code) actually keeps its line breaks and indentation
            instead of collapsing to one line. */}
        <div className="bg-neutral-800 rounded-lg px-3 py-2 text-sm max-w-[80%] whitespace-pre-wrap">
          {message.text}
        </div>
      </div>
    );
  }

  const { data } = message;
  return (
    <div className="flex justify-start">
      <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm max-w-[80%] space-y-1">
        <div className="text-xs text-neutral-500">
          tier {data.tier} · {data.status}
        </div>
        <ResultBody data={data} />
      </div>
    </div>
  );
}

function ResultBody({ data }) {
  if (data.status === "error" || data.message) {
    return <div className="text-red-400 whitespace-pre-wrap">{data.message}</div>;
  }
  if (data.tier === "sga" || data.tier === "cache") {
    return <Markdown>{data.result?.answer}</Markdown>;
  }
  if (data.tier === 0) {
    return <Markdown>{data.result?.answer}</Markdown>;
  }
  if (data.tier === 1) {
    // NOT run through Markdown here on purpose: result.code is raw code
    // text, not markdown prose — parsing it as markdown risks mangling
    // things like underscores (_snake_case_) as italics. Styled the same
    // as Markdown's own fenced-code blocks for visual consistency.
    return (
      <div className="rounded-lg border border-neutral-800 bg-black/50 overflow-hidden">
        <pre className="overflow-x-auto p-3 text-xs text-neutral-300">
          <code>{data.result?.code}</code>
        </pre>
      </div>
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
