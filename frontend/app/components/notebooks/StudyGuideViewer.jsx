"use client";
import Markdown from "../Markdown";

// BUGFIX (chat audit bug #7): this used to hand-roll its own block
// parser (headings/lists/paragraphs only) instead of reusing the
// shared Markdown renderer every other surface in the app uses. It had
// no inline-markdown support at all (**bold**, `code`, links) and no
// table support, so study_guide_writer's output — which the role brief
// in eo/registry.py explicitly allows to include Markdown tables and
// bold key terms — rendered as literal '**word**' and raw
// '| Step | Description |' pipe rows instead of formatted HTML. The
// shared <Markdown> component (react-markdown + remark-gfm, already a
// dependency, already used for chat messages) handles all of that for
// free, so this view is now a thin wrapper instead of a second parser
// to keep in sync with the writer role's actual output grammar.
export default function StudyGuideViewer({ markdownText }) {
  if (!markdownText || !markdownText.trim()) {
    return <p className="text-xs text-[var(--neutral-500)]">Couldn't parse a study guide from this text.</p>;
  }
  return (
    <div className="max-w-none [&_.markdown-body]:text-xs">
      <Markdown>{markdownText}</Markdown>
    </div>
  );
}
