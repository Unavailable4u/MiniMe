"use client";
// frontend/app/components/workbench/PreviewColumn.jsx — W2.3b (Build
// Workbench plan). The column at the right of the editor: a titled
// frame with a close button around whatever preview is showing. Empty
// for now — W6.1's PreviewPane is passed in as `children` and replaces
// the placeholder, without this frame or the splitter/toggle/persisted
// width around it changing.
//
// Presentational, like the other panes. Whether it's open, and its
// width, are the workbench's business (editor store `layout` +
// useSplitter); this only draws the frame.
import { memo } from "react";
import { X } from "lucide-react";

/**
 * @param {object} props
 * @param {() => void} props.onClose
 * @param {import("react").ReactNode} [props.children] - the preview itself; a placeholder shows when omitted
 */
function PreviewColumn({ onClose, children }) {
  return (
    <aside aria-label="Preview" className="h-full min-h-0 flex flex-col bg-[var(--neutral-950)]">
      {/* h-9 to line up with the explorer header and the editor tab strip */}
      <div className="shrink-0 flex items-center justify-between px-3 h-9 border-b border-[var(--neutral-800)]">
        <span className="text-[10px] uppercase tracking-wide text-[var(--neutral-500)]">Preview</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close preview"
          title="Close preview"
          className="touch-target text-[var(--neutral-500)] hover:text-[var(--neutral-200)]"
        >
          <X size={12} />
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-auto">
        {children ?? (
          <div className="h-full flex flex-col items-center justify-center gap-1 px-6 text-center">
            <p className="text-xs text-[var(--neutral-400)]">Nothing to preview yet</p>
            <p className="max-w-xs text-[11px] leading-relaxed text-[var(--neutral-600)]">
              A live preview of this project will appear here.
            </p>
          </div>
        )}
      </div>
    </aside>
  );
}

export default memo(PreviewColumn);
