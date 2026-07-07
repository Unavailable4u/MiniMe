"use client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Shared markdown renderer for agent output (MessageBubble's ResultBody,
// AgentStepList's step bodies). Custom-styled per element instead of
// relying on @tailwindcss/typography, since this repo doesn't have that
// plugin installed — keeps the dependency footprint to just
// react-markdown + remark-gfm (tables, strikethrough, task lists).
export default function Markdown({ children }) {
  if (!children) return null;
  return (
    <div className="markdown-body text-sm leading-relaxed text-neutral-200 space-y-3">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: (p) => <h1 className="text-lg font-semibold text-neutral-100 mt-4 mb-2" {...p} />,
          h2: (p) => <h2 className="text-base font-semibold text-neutral-100 mt-4 mb-2" {...p} />,
          h3: (p) => <h3 className="text-sm font-semibold text-neutral-200 mt-3 mb-1.5" {...p} />,
          p: (p) => <p className="text-neutral-300" {...p} />,
          ul: (p) => <ul className="list-disc pl-5 space-y-1 text-neutral-300" {...p} />,
          ol: (p) => <ol className="list-decimal pl-5 space-y-1 text-neutral-300" {...p} />,
          li: (p) => <li className="marker:text-neutral-600" {...p} />,
          a: (p) => (
            <a className="text-cyan-400 underline underline-offset-2 hover:text-cyan-300" target="_blank" rel="noreferrer" {...p} />
          ),
          blockquote: (p) => (
            <blockquote className="border-l-2 border-neutral-700 pl-3 text-neutral-400 italic" {...p} />
          ),
          hr: () => <hr className="border-neutral-800 my-3" />,
          table: (p) => (
            <div className="overflow-x-auto">
              <table className="min-w-full border-collapse text-xs" {...p} />
            </div>
          ),
          thead: (p) => <thead className="border-b border-neutral-700" {...p} />,
          th: (p) => <th className="text-left px-2 py-1.5 font-medium text-neutral-300" {...p} />,
          td: (p) => <td className="px-2 py-1.5 border-t border-neutral-800/70 text-neutral-400" {...p} />,
          code: ({ inline, className, children, ...rest }) => {
            if (inline) {
              return (
                <code className="bg-neutral-800 rounded px-1 py-0.5 text-[0.85em] text-amber-300" {...rest}>
                  {children}
                </code>
              );
            }
            // Language tag from the fenced block (```python -> "language-python"),
            // shown as a small label above the block.
            const lang = /language-(\w+)/.exec(className || "")?.[1];
            return (
              <div className="rounded-lg border border-neutral-800 bg-black/50 overflow-hidden my-2">
                {lang && (
                  <div className="px-3 py-1 text-[10px] uppercase tracking-wide text-neutral-500 border-b border-neutral-800">
                    {lang}
                  </div>
                )}
                <pre className="overflow-x-auto p-3 text-xs">
                  <code className={className} {...rest}>
                    {children}
                  </code>
                </pre>
              </div>
            );
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
