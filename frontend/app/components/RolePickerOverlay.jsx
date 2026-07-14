"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { categorize, DEFAULT_CATEGORY } from "./agentRoleIcons";
import { authHeaders } from "../context/SessionContext";
import { Plus, X, ChevronUp, ChevronDown, Check } from "lucide-react";

// Searchable multi-select combobox for building an ordered role
// pipeline — replaces TemplateBuilder's old plain comma-separated text
// input. Known roles come from the same GET /api/roles RoleLibraryTab.jsx
// already uses, grouped through the same categorize() buckets so a
// role's color/icon here matches what's shown everywhere else.
//
// Order matters (this becomes a template's execution_order), so instead
// of a plain checklist, selected roles render as reorderable chips
// above the trigger with their own up/down controls. "Requires
// approval" — previously a second free-text field the person had to
// keep in sync with the first by hand — is now a per-chip toggle (✋),
// which can only ever reference a role that's actually in the pipeline.
//
// A role typed into the search box that doesn't match any known role
// can still be added — role briefs get created on first hire (see
// eo/registry.py), so "known roles" is a convenience list, not a
// closed set.
export default function RolePickerOverlay({
  apiUrl,
  roles,
  onRolesChange,
  approvalRoles,
  onApprovalRolesChange,
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [knownRoles, setKnownRoles] = useState(null);
  const [knownError, setKnownError] = useState(null);
  const containerRef = useRef(null);
  const searchInputRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setKnownError(null);
      try {
        const res = await fetch(`${apiUrl}/api/roles`, {
          headers: await authHeaders(),
        });
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const data = await res.json();
        if (!cancelled) setKnownRoles(data);
      } catch (e) {
        if (!cancelled) setKnownError(e.message);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [apiUrl]);

  // Click-outside + Escape both close the overlay, same convention as
  // ChatTab's mode picker dropdown.
  useEffect(() => {
    if (!open) return;
    function onMouseDown(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false);
        setSearch("");
      }
    }
    function onKeyDown(e) {
      if (e.key === "Escape") {
        setOpen(false);
        setSearch("");
      }
    }
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (open) searchInputRef.current?.focus();
  }, [open]);

  function addRole(roleName) {
    const trimmed = roleName.trim();
    if (!trimmed || roles.some((r) => r.toLowerCase() === trimmed.toLowerCase())) return;
    onRolesChange([...roles, trimmed]);
    setSearch("");
  }

  function removeRole(roleName) {
    onRolesChange(roles.filter((r) => r !== roleName));
    if (approvalRoles.includes(roleName)) {
      onApprovalRolesChange(approvalRoles.filter((r) => r !== roleName));
    }
  }

  function moveRole(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= roles.length) return;
    const next = [...roles];
    [next[index], next[target]] = [next[target], next[index]];
    onRolesChange(next);
  }

  function toggleApproval(roleName) {
    if (approvalRoles.includes(roleName)) {
      onApprovalRolesChange(approvalRoles.filter((r) => r !== roleName));
    } else {
      onApprovalRolesChange([...approvalRoles, roleName]);
    }
  }

  function handleSearchKeyDown(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (showCustomOption) addRole(search);
    }
  }

  const grouped = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = (knownRoles || []).filter(
      (r) => !q || r.role.toLowerCase().includes(q)
    );
    const groups = {};
    for (const entry of filtered) {
      const category = categorize(entry.role) || DEFAULT_CATEGORY;
      (groups[category.key] ||= { category, entries: [] }).entries.push(entry);
    }
    return Object.values(groups).sort((a, b) => a.category.key.localeCompare(b.category.key));
  }, [knownRoles, search]);

  const trimmedSearch = search.trim();
  const showCustomOption =
    trimmedSearch.length > 0 &&
    !roles.some((r) => r.toLowerCase() === trimmedSearch.toLowerCase()) &&
    !(knownRoles || []).some((r) => r.role.toLowerCase() === trimmedSearch.toLowerCase());

  return (
    <div className="relative" ref={containerRef}>
      <label className="block text-xs text-[var(--neutral-500)] mb-1">Roles, in order</label>

      <div className="min-h-[2.25rem] w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2 py-1.5 flex flex-wrap items-center gap-1.5">
        {roles.length === 0 && (
          <span className="text-xs text-[var(--neutral-600)] px-1">No roles selected yet</span>
        )}
        {roles.map((roleName, i) => {
          const category = categorize(roleName) || DEFAULT_CATEGORY;
          const requiresApproval = approvalRoles.includes(roleName);
          return (
            <span
              key={roleName}
              className="flex items-center gap-1 text-[11px] rounded border border-[var(--neutral-800)] pl-1 pr-1.5 py-0.5"
              style={{ color: category.color }}
            >
              <span className="flex flex-col -my-1">
                <button
                  type="button"
                  onClick={() => moveRole(i, -1)}
                  disabled={i === 0}
                  title="Move earlier"
                  className="text-[var(--neutral-600)] hover:text-[var(--neutral-200)] disabled:opacity-20 disabled:hover:text-[var(--neutral-600)] leading-none"
                >
                  <ChevronUp size={9} />
                </button>
                <button
                  type="button"
                  onClick={() => moveRole(i, 1)}
                  disabled={i === roles.length - 1}
                  title="Move later"
                  className="text-[var(--neutral-600)] hover:text-[var(--neutral-200)] disabled:opacity-20 disabled:hover:text-[var(--neutral-600)] leading-none"
                >
                  <ChevronDown size={9} />
                </button>
              </span>
              <span>
                {category.icon} {roleName}
              </span>
              <button
                type="button"
                onClick={() => toggleApproval(roleName)}
                title={requiresApproval ? "Requires approval — click to unset" : "Click to require approval after this role"}
                className={requiresApproval ? "text-amber-500" : "text-[var(--neutral-700)] hover:text-amber-500"}
              >
                ✋
              </button>
              <button
                type="button"
                onClick={() => removeRole(roleName)}
                title="Remove"
                className="text-[var(--neutral-600)] hover:text-red-400"
              >
                <X size={10} />
              </button>
            </span>
          );
        })}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-1 text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-1.5 py-0.5 rounded border border-dashed border-[var(--neutral-800)]"
        >
          <Plus size={11} />
          Add role
        </button>
      </div>

      {open && (
        <div className="absolute z-20 mt-1 w-full max-h-80 overflow-y-auto rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900)] shadow-xl p-2 space-y-2">
          <input
            ref={searchInputRef}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={handleSearchKeyDown}
            placeholder="Search or type a new role name…"
            className="w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
          />

          {knownError && (
            <p className="text-[11px] text-red-400">Couldn't load known roles: {knownError}</p>
          )}

          {showCustomOption && (
            <button
              type="button"
              onClick={() => addRole(search)}
              className="w-full flex items-center gap-1.5 text-left text-xs rounded-md px-2 py-1.5 text-[var(--neutral-300)] hover:bg-[var(--neutral-800)] border border-dashed border-[var(--neutral-700)]"
            >
              <Plus size={12} />
              Add "{trimmedSearch}" as a new role
            </button>
          )}

          {knownRoles === null && !knownError && (
            <p className="text-[11px] text-[var(--neutral-500)] px-1">Loading roles…</p>
          )}

          {knownRoles !== null && grouped.length === 0 && !showCustomOption && (
            <p className="text-[11px] text-[var(--neutral-500)] px-1">No roles match.</p>
          )}

          <div className="space-y-2">
            {grouped.map(({ category, entries }) => (
              <div key={category.key}>
                <div className="text-[10px] font-medium text-[var(--neutral-500)] flex items-center gap-1 px-1 mb-0.5">
                  <span>{category.icon}</span>
                  {category.key}
                </div>
                {entries.map((entry) => {
                  const selected = roles.includes(entry.role);
                  return (
                    <button
                      key={entry.role}
                      type="button"
                      onClick={() => (selected ? removeRole(entry.role) : addRole(entry.role))}
                      className={`w-full flex items-center justify-between gap-2 text-left text-xs rounded-md px-2 py-1.5 transition-colors ${
                        selected
                          ? "bg-[var(--neutral-800-a70)] text-[var(--neutral-100)]"
                          : "text-[var(--neutral-300)] hover:bg-[var(--neutral-800)]"
                      }`}
                    >
                      <span className="truncate">{entry.role}</span>
                      {selected && <Check size={12} className="shrink-0 text-emerald-400" />}
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}