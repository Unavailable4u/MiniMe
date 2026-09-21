"use client";
// frontend/app/components/workbench/ExplorerRow.jsx — W2.4 (Build
// Workbench plan). The pieces Explorer.jsx draws the tree from: one
// row (a file or a folder), the inline name input that a rename and a
// "New file/folder" both use, and the row that hosts that input for a
// new entry. Split out so Explorer.jsx can be about selection,
// keyboard, drag and the menu, and this file about how a row LOOKS.
//
// Everything here is presentational. A row reports what happened to it
// (`onRowClick(event, path, type)`, `onRowDragStart(...)`, …) and never
// decides anything; handlers are passed in as stable callbacks and
// every other prop is a primitive, so `memo` really does skip the rows
// that didn't change (there can be hundreds).
import { memo, useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Database,
  File,
  FileCode,
  FileCog,
  FileImage,
  FileJson,
  FileTerminal,
  FileText,
  Folder,
  FolderOpen,
  Globe,
  Lock,
  Package,
  Palette,
} from "lucide-react";
import { fileIconKey } from "../../lib/workbench/fileIcons";

// fileIconKey() result -> [icon, color]. Muted on purpose: the colors
// only say "what kind of file", they shouldn't compete with the active
// row or the unsaved-changes dot.
const FILE_ICONS = {
  code: [FileCode, "text-sky-400"],
  markup: [Globe, "text-orange-400"],
  style: [Palette, "text-violet-400"],
  json: [FileJson, "text-yellow-400"],
  data: [Database, "text-emerald-400"],
  config: [FileCog, "text-[var(--neutral-500)]"],
  doc: [FileText, "text-[var(--neutral-400)]"],
  image: [FileImage, "text-pink-400"],
  shell: [FileTerminal, "text-green-400"],
  lock: [Lock, "text-[var(--neutral-600)]"],
  package: [Package, "text-red-400"],
  file: [File, "text-[var(--neutral-500)]"],
};

// Indentation. A folder's chevron sits at its depth; a file has no
// chevron, so it's pushed in by the chevron's width to line up with its
// folder's NAME (same numbers the pre-W2.4 tree used).
const INDENT = 14;
const CHEVRON_WIDTH = 16;
const rowIndent = (depth, isDir) => depth * INDENT + (isDir ? 0 : CHEVRON_WIDTH);

function EntryIcon({ type, name, open, plain }) {
  if (type === "dir") {
    const Icon = open ? FolderOpen : Folder;
    return <Icon size={12} className="shrink-0" />;
  }
  const [Icon, tone] = FILE_ICONS[fileIconKey(name)] || FILE_ICONS.file;
  // `plain`: on the accent-colored active row the icon inherits the
  // row's text color instead of adding its own.
  return <Icon size={12} className={`shrink-0 ${plain ? "" : tone}`} />;
}

/**
 * The inline input a rename and "New file/folder" share.
 *
 * Enter commits, Escape cancels, and clicking away commits if there's
 * a valid, changed name and cancels otherwise (an empty or invalid box
 * you walk away from just goes away — no error to dismiss). A name the
 * client-side rules reject shows the reason in a popover under the box
 * and stays put; so does a refusal from the server, which arrives as
 * the resolved value of `onSubmit`.
 *
 * @param {object} props
 * @param {string} [props.initial] - starting text (the current name, when renaming)
 * @param {boolean} [props.isDir] - folders have no extension to keep out of the initial selection
 * @param {string} props.label - aria-label
 * @param {(value: string) => string|null} props.validate - reason it's not OK, or null
 * @param {(name: string, meta: {blurred: boolean}) => Promise<string|null>} props.onSubmit - null = done, string = server's refusal
 * @param {() => void} props.onCancel
 */
export function NameInput({ initial = "", isDir = false, label, validate, onSubmit, onCancel }) {
  const [value, setValue] = useState(initial);
  const [submitError, setSubmitError] = useState(null);
  const [pending, setPending] = useState(false);
  const inputRef = useRef(null);
  const doneRef = useRef(false); // committed or cancelled: ignore anything that fires while unmounting
  const pendingRef = useRef(false);

  // Focus on mount, and when renaming pre-select the name but not its
  // extension — renaming is usually "change the name, keep .jsx".
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.focus();
    const dot = isDir ? -1 : initial.lastIndexOf(".");
    el.setSelectionRange(0, dot > 0 ? dot : initial.length);
  }, [initial, isDir]);

  const trimmed = value.trim();
  // Live feedback only once there's something to judge, and not for the
  // untouched initial text (an unchanged rename isn't an error).
  const liveError = trimmed && value !== initial ? validate(value) : null;
  const error = submitError || liveError;

  function cancel() {
    if (doneRef.current) return;
    doneRef.current = true;
    onCancel();
  }

  async function submit(blurred) {
    if (doneRef.current || pendingRef.current) return;
    if (!trimmed || trimmed === initial) {
      cancel();
      return;
    }
    const invalid = validate(value);
    if (invalid) {
      setSubmitError(invalid);
      return;
    }
    pendingRef.current = true;
    setPending(true);
    const refusal = await onSubmit(trimmed, { blurred });
    pendingRef.current = false;
    if (refusal) {
      setPending(false);
      setSubmitError(refusal);
      inputRef.current?.focus();
      return;
    }
    doneRef.current = true; // the parent unmounts this next
  }

  function onKeyDown(e) {
    // Keep typing away from the tree's own keyboard handling.
    e.stopPropagation();
    if (e.key === "Enter") {
      e.preventDefault();
      submit(false);
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  }

  function onBlur() {
    if (doneRef.current || pendingRef.current) return;
    if (!trimmed || trimmed === initial || validate(value)) cancel();
    else submit(true);
  }

  return (
    <div className="relative min-w-0 flex-1">
      <input
        ref={inputRef}
        value={value}
        disabled={pending}
        onChange={(e) => {
          setValue(e.target.value);
          setSubmitError(null);
        }}
        onKeyDown={onKeyDown}
        onBlur={onBlur}
        // A click in the box mustn't reach the row (it would re-select
        // it, or open/toggle it).
        onClick={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
        onContextMenu={(e) => e.stopPropagation()}
        aria-label={label}
        aria-invalid={error ? true : undefined}
        spellCheck={false}
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        className={`touch-input w-full rounded border bg-[var(--neutral-950)] px-1 py-0 text-xs text-[var(--neutral-100)] outline-none disabled:opacity-60 ${
          error ? "border-red-500/70" : "border-[var(--accent)]"
        }`}
      />
      {error && (
        <div
          role="alert"
          className="absolute left-0 top-full z-20 mt-0.5 w-max max-w-[16rem] rounded border border-red-800 bg-red-950 px-2 py-1 text-[10px] leading-snug text-red-200 shadow-lg"
        >
          {error}
        </div>
      )}
    </div>
  );
}

/** The row that hosts the input while creating a file or folder: an icon, no name yet. */
export function NewEntryRow({ depth, isDir, validate, onSubmit, onCancel }) {
  return (
    <div
      className="flex w-full items-center gap-1 rounded py-0.5 text-xs text-[var(--neutral-300)]"
      style={{ paddingLeft: rowIndent(depth, isDir) }}
    >
      {isDir && <span className="w-3 shrink-0" />}
      <EntryIcon type={isDir ? "dir" : "file"} name="" open={false} />
      <NameInput
        isDir={isDir}
        label={isDir ? "New folder name" : "New file name"}
        validate={validate}
        onSubmit={onSubmit}
        onCancel={onCancel}
      />
    </div>
  );
}

/**
 * One row of the tree. `focusable` marks the tree's single tab stop
 * (roving tabindex: arrow keys move focus between rows, Tab leaves the
 * tree instead of walking every row). `selected` is the explorer's own
 * multi-selection; `active` is "this file is the one open in the
 * editor" — both can be true, and active wins visually.
 */
export const TreeRow = memo(function TreeRow({
  path,
  name,
  type,
  depth,
  open,
  selected,
  active,
  focusable,
  dirty,
  dropTarget,
  draggable,
  renaming,
  validateName,
  onSubmitName,
  onCancelEdit,
  onRowClick,
  onRowContextMenu,
  onRowDragStart,
  onRowDragOver,
  onRowDrop,
  onRowDragEnd,
}) {
  const isDir = type === "dir";
  const tone = dropTarget
    ? "bg-[var(--accent)]/25 ring-1 ring-inset ring-[var(--accent)] text-[var(--neutral-100)]"
    : active
    ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--accent-text)]"
    : selected
    ? "bg-[var(--neutral-800)] text-[var(--neutral-100)] focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--accent)]"
    : `${
        isDir ? "text-[var(--neutral-300)]" : "text-[var(--neutral-400)]"
      } hover:bg-[var(--neutral-900)] hover:text-[var(--neutral-100)] focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-[var(--accent)]`;

  return (
    <div
      role="treeitem"
      aria-level={depth + 1}
      aria-selected={selected || active}
      aria-expanded={isDir ? open : undefined}
      data-row-path={path}
      data-row-type={type}
      tabIndex={focusable ? 0 : -1}
      draggable={draggable && !renaming}
      title={isDir ? undefined : path}
      onClick={renaming ? undefined : (e) => onRowClick(e, path, type)}
      onContextMenu={(e) => onRowContextMenu(e, path, type)}
      onDragStart={(e) => onRowDragStart(e, path)}
      onDragOver={(e) => onRowDragOver(e, path, type)}
      onDrop={(e) => onRowDrop(e, path, type)}
      onDragEnd={onRowDragEnd}
      // select-none + no iOS callout: a long-press should open the
      // menu, not select the filename or show Safari's own sheet.
      className={`touch-row flex w-full cursor-pointer select-none items-center gap-1 rounded py-0.5 text-xs outline-none [-webkit-touch-callout:none] ${tone}`}
      style={{ paddingLeft: rowIndent(depth, isDir) }}
    >
      {isDir &&
        (open ? (
          <ChevronDown size={12} className="shrink-0" />
        ) : (
          <ChevronRight size={12} className="shrink-0" />
        ))}
      <EntryIcon type={type} name={name} open={open} plain={active} />
      {renaming ? (
        <NameInput
          initial={name}
          isDir={isDir}
          label={isDir ? "Folder name" : "File name"}
          validate={validateName}
          onSubmit={onSubmitName}
          onCancel={onCancelEdit}
        />
      ) : (
        <span className="truncate">{name}</span>
      )}
      {dirty && !renaming && (
        <span
          className="ml-auto mr-1 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400"
          role="img"
          aria-label="Unsaved changes"
        />
      )}
    </div>
  );
});
