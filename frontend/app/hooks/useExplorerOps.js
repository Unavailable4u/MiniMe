"use client";
// frontend/app/hooks/useExplorerOps.js — W2.4 (Build Workbench plan).
// The async half of the explorer's file operations: create, rename,
// move, duplicate, delete. Each one talks to the FileProvider (never to
// a route directly — see fileProviders.js), then makes the rest of the
// workbench catch up: open tabs follow a rename or move, tabs of
// deleted files close, and the file list is refetched. The decisions —
// what's a legal name, what a drop amounts to, what the copy is called —
// are lib/workbench/explorerOps.js's; this file is only "do it, in
// order, and clean up".
//
// Two error conventions, matching where the person is looking:
//  - createFile / createFolder / rename come from an INLINE input in the
//    tree, so they resolve to an error MESSAGE (or null on success) and
//    the input shows it in place and stays open;
//  - remove / duplicate / move have no input to hang an error on, so
//    they report through `notify` (the workbench's dismissible notice).
// Neither ever rejects: a network failure is a message like any other.
//
// One operation runs at a time. Two overlapping ones (a drop while a
// rename is still in flight) would each plan against a file list the
// other is about to change; refusing the second with a "try again" is
// simpler than reconciling them, and these are sub-second calls.
import { useCallback, useMemo, useRef, useState } from "react";
import { basename, dirname, isSameOrDescendant, remapPath } from "../lib/workbench/fileTree";
import { collapseNested, entryPaths, joinPath, pathsUnder, planDrop, uniqueCopyPath } from "../lib/workbench/explorerOps";

const MAX_DUPLICATE_FILES = 200; // sequential read+write per file — keep a "duplicate folder" bounded
const BUSY_MESSAGE = "Another file operation is still running. Try again in a moment.";
const SAVING_MESSAGE = "A file in there is still saving. Try again in a moment.";

function messageFor(err) {
  return err?.message || "Something went wrong.";
}

/**
 * @param {object} deps
 * @param {object} deps.provider - the active FileProvider
 * @param {{current: object|null}} deps.filesMetaRef - latest provider.list() result
 * @param {{current: object}} deps.stateRef - latest editor store state (for which tabs are open)
 * @param {{current: Set<string>}} deps.savingRef - paths with a save in flight
 * @param {() => Promise<void>} deps.refresh - refetch the list and sync open buffers
 * @param {(path: string) => void} deps.openFile
 * @param {(paths: string[]) => void} deps.dropTabs - close tabs and forget their per-path state
 * @param {(renames: {from: string, to: string}[], versions?: object) => void} deps.renamePaths - store action
 * @param {(renames: {from: string, to: string}[]) => void} deps.onRenamed - re-key per-path state kept outside the store
 * @param {(message: string) => void} deps.notify - show the workbench notice
 */
export function useExplorerOps({
  provider,
  filesMetaRef,
  stateRef,
  savingRef,
  refresh,
  openFile,
  dropTabs,
  renamePaths,
  onRenamed,
  notify,
}) {
  const busyRef = useRef(false);
  const [busy, setBusy] = useState(false);

  // Runs `task` as THE operation in flight. Resolves to the task's own
  // result (an error message or null), or a message if it threw.
  const exclusive = useCallback(async (task) => {
    if (busyRef.current) return BUSY_MESSAGE;
    busyRef.current = true;
    setBusy(true);
    try {
      return (await task()) ?? null;
    } catch (err) {
      return messageFor(err);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, []);

  const currentFiles = useCallback(() => Object.keys(filesMetaRef.current || {}), [filesMetaRef]);
  const anySaving = useCallback(
    (paths) => [...savingRef.current].some((saving) => paths.some((p) => isSameOrDescendant(saving, p))),
    [savingRef]
  );

  // The server has moved these; bring the tabs (and per-path state)
  // along. `moved` is the provider's list of moved-file shapes — their
  // new versions let the store keep a just-moved buffer "in sync".
  const applyRenames = useCallback(
    (renames, moved) => {
      const versions = {};
      for (const file of moved || []) {
        if (file && file.file_path) versions[file.file_path] = file.version;
      }
      renamePaths(renames, versions);
      onRenamed(renames);
    },
    [renamePaths, onRenamed]
  );

  const createFile = useCallback(
    (dir, name) =>
      exclusive(async () => {
        const path = joinPath(dir, name);
        try {
          // baseVersion 0 = "only if nothing is there yet": without it a
          // stale file list could let "New file" silently blank an
          // existing file of the same name.
          await provider.write(path, "", { baseVersion: 0 });
        } catch (err) {
          if (err?.name === "FileConflictError") return `"${name}" already exists here.`;
          throw err;
        }
        await refresh();
        openFile(path);
        return null;
      }),
    [exclusive, provider, refresh, openFile]
  );

  const createFolder = useCallback(
    (dir, name) =>
      exclusive(async () => {
        await provider.mkdir(joinPath(dir, name));
        await refresh();
        return null;
      }),
    [exclusive, provider, refresh]
  );

  const rename = useCallback(
    (path, newName) =>
      exclusive(async () => {
        const to = joinPath(dirname(path), newName);
        if (to === path) return null;
        if (anySaving([path])) return SAVING_MESSAGE;
        const moved = await provider.move(path, to);
        applyRenames([{ from: path, to }], moved);
        await refresh();
        return null;
      }),
    [exclusive, anySaving, provider, applyRenames, refresh]
  );

  const remove = useCallback(
    async (paths) => {
      const error = await exclusive(async () => {
        const roots = collapseNested(paths);
        if (anySaving(roots)) return SAVING_MESSAGE;
        const deleted = [];
        let failure = null;
        // Independent deletes: one failing shouldn't strand the others.
        for (const root of roots) {
          try {
            await provider.remove(root);
            deleted.push(root);
          } catch (err) {
            failure = failure || `Couldn't delete "${basename(root)}": ${messageFor(err)}`;
          }
        }
        const openTabs = stateRef.current.tabs.filter((p) => deleted.some((r) => isSameOrDescendant(p, r)));
        if (openTabs.length > 0) dropTabs(openTabs);
        await refresh();
        return failure;
      });
      if (error) notify(error);
    },
    [exclusive, anySaving, provider, stateRef, dropTabs, refresh, notify]
  );

  const duplicate = useCallback(
    async (path) => {
      const error = await exclusive(async () => {
        const files = currentFiles();
        const under = pathsUnder(files, path);
        if (under.length === 0) return `"${basename(path)}" no longer exists.`;
        if (under.length > MAX_DUPLICATE_FILES) {
          return `That folder has ${under.length} files — more than the ${MAX_DUPLICATE_FILES} that can be duplicated at once.`;
        }
        const isDir = under.some((p) => p !== path);
        const target = uniqueCopyPath(path, entryPaths(files), { isDir });
        let copied = 0;
        try {
          for (const source of under) {
            const file = await provider.read(source);
            await provider.write(remapPath(source, path, target), file.content ?? "", {
              baseVersion: 0,
              language: file.language ?? undefined,
            });
            copied += 1;
          }
        } catch (err) {
          await refresh();
          return `Duplicate stopped after ${copied} of ${under.length} files: ${messageFor(err)}`;
        }
        await refresh();
        if (!isDir) openFile(target);
        return null;
      });
      if (error) notify(error);
    },
    [exclusive, currentFiles, provider, refresh, openFile, notify]
  );

  // Resolves to the moves that actually happened ([{from, to}], possibly
  // fewer than asked for, possibly none) so the explorer can carry its
  // selection and open folders along to the new paths.
  const move = useCallback(
    async (sources, targetDir) => {
      const applied = [];
      const error = await exclusive(async () => {
        const { moves, error: planError } = planDrop({
          sources,
          targetDir,
          taken: entryPaths(currentFiles()),
        });
        if (planError) return planError;
        if (moves.length === 0) return null;
        if (anySaving(moves.map((m) => m.from))) return SAVING_MESSAGE;

        let failure = null;
        for (const m of moves) {
          try {
            const moved = await provider.move(m.from, m.to);
            applyRenames([m], moved);
            applied.push(m);
          } catch (err) {
            failure = `Couldn't move "${basename(m.from)}": ${messageFor(err)}`;
            if (applied.length > 0) failure += ` (${applied.length} of ${moves.length} moved.)`;
            break;
          }
        }
        await refresh();
        return failure;
      });
      if (error) notify(error);
      return applied;
    },
    [exclusive, currentFiles, anySaving, provider, applyRenames, refresh, notify]
  );

  return useMemo(
    () => ({ busy, createFile, createFolder, rename, remove, duplicate, move }),
    [busy, createFile, createFolder, rename, remove, duplicate, move]
  );
}
