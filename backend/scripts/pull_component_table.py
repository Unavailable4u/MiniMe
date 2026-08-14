"""
scripts/pull_component_table.py — bridge between the MiniMeMech Google
Sheet (component_dimensions tab, published-to-web as CSV) and the JSON
file agents/component_dimension_table.py actually reads at runtime
(agents/data/component_dimensions_table.json).

This is a MANUAL bridge, not a live sync. component_dimension_table.py
never touches the network or the sheet -- it only ever reads the JSON
file, and that file only ever changes when a human runs this script
and explicitly confirms. That's deliberate: the sheet can be mid-edit
or mid-migration at any moment, so "always reflect the sheet" would be
worse than "reflects the sheet as of whenever I last chose to pull."

Default behaviour is DRY RUN: fetch the sheet, diff it against the
current JSON file, print a summary of what would change, and write
nothing. Pass --apply to actually write. This mirrors the --dry-run
convention already used in scripts/backfill_chat_messages.py, just
defaulted the safer way round here since this script's whole reason
to exist is "don't let a bad/partial sheet silently become the live
data."

Usage:
    python scripts/pull_component_table.py                 # dry-run, shows diff
    python scripts/pull_component_table.py --apply          # applies it
    python scripts/pull_component_table.py --url <csv_url>  # override the default URL
"""
import argparse
import csv
import io
import json
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Matches the convention in scripts/backfill_chat_messages.py -- this
# script lives in scripts/, backend is the project root relative to
# it, so it needs to be on sys.path if this script ever grows to
# import project code directly. Not currently needed (this script is
# deliberately dependency-free: stdlib only, no app imports), but kept
# for consistency / in case that changes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Published-to-web CSV link for the component_dimensions tab specifically
# (NOT "entire document" -- that would include the legend tab, which
# component_dimension_table.py has no use for and which would just be
# noise/extra rows to skip). Publish → Link → pick "component_dimensions"
# in the sheet dropdown → CSV in the format dropdown → Publish.
DEFAULT_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vTRMKx2MW-HFTGtdHavJMO6"
    "OBureXNUn8wuorsXKYlLsXaikndrYsx5lBP88-7P4MqzBY5vOLE1L0OZ/pub"
    "?gid=0&single=true&output=csv"
)

# Same path component_dimension_table.py's own _TABLE_PATH resolves to.
_TABLE_PATH = (
    Path(__file__).resolve().parent.parent
    / "agents" / "data" / "component_dimensions_table.json"
)

# Columns this script actually converts to numbers/lists. Every other
# column from the sheet (category, notes, source_dataset, source_url,
# license, ...) is passed through untouched as a plain string --
# component_dimension_table.py ignores keys it doesn't recognize, so
# there's no need to strip them here, and keeping them means the JSON
# file stays a faithful full copy of the sheet for anyone reading it
# by eye later.
_NUMERIC_COLUMNS = ("dimensions_w_mm", "dimensions_h_mm", "dimensions_d_mm")


def _fetch_csv(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    return raw.decode("utf-8-sig")  # -sig strips a possible BOM Sheets adds


def _to_number_or_none(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        as_float = float(value)
    except ValueError:
        return value  # leave non-numeric junk visible rather than swallowing it
    return int(as_float) if as_float.is_integer() else as_float


def _row_from_csv_dict(raw: dict) -> dict:
    """One CSV row -> one JSON row. Blank cells become None/"" rather
    than the literal string "" Python's csv module would otherwise
    leave in place, since component_dimension_table.py's own
    _row_to_match() treats `is not None` as "this dimension applies"
    -- a stray empty string would incorrectly count as present.
    """
    row = {}
    for key, value in raw.items():
        if key is None:  # csv.DictReader's bucket for any extra columns
            continue
        key = key.strip()
        if not key:
            continue
        if key in _NUMERIC_COLUMNS:
            row[key] = _to_number_or_none(value)
        elif key == "aliases":
            # Loader accepts aliases as a comma-separated string OR a
            # list (component_dimension_table.py's _load_table()
            # branches on isinstance). Sheet stores it as one
            # comma-separated cell already, so pass it through as-is
            # rather than re-splitting -- fewer places for a stray
            # extra comma in someone's alias text to cause harm.
            row[key] = value.strip() if value and value.strip() else None
        else:
            row[key] = value.strip() if value and value.strip() else None
    return row


def fetch_and_convert(url: str) -> list:
    csv_text = _fetch_csv(url)
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    skipped = 0
    for raw in reader:
        row = _row_from_csv_dict(raw)
        if not row.get("id"):
            skipped += 1  # blank/separator row in the sheet -- not real data
            continue
        rows.append(row)
    if skipped:
        print(f"  (skipped {skipped} row(s) with no 'id' -- likely blank rows)")
    return rows


def _load_current() -> list:
    if not _TABLE_PATH.exists():
        return []
    try:
        with open(_TABLE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  WARNING: existing {_TABLE_PATH} is unreadable/invalid ({e}); "
              f"treating current table as empty for diff purposes")
        return []


def _diff(old_rows: list, new_rows: list):
    old_by_id = {r.get("id"): r for r in old_rows if r.get("id")}
    new_by_id = {r.get("id"): r for r in new_rows if r.get("id")}

    added = [rid for rid in new_by_id if rid not in old_by_id]
    removed = [rid for rid in old_by_id if rid not in new_by_id]
    changed = [
        rid for rid in new_by_id
        if rid in old_by_id and old_by_id[rid] != new_by_id[rid]
    ]
    unchanged = len(new_by_id) - len(added) - len(changed)

    return added, removed, changed, unchanged


def main():
    parser = argparse.ArgumentParser(
        description="Pull component_dimensions from the published Google "
                     "Sheet CSV into agents/data/component_dimensions_table.json"
    )
    parser.add_argument("--url", default=DEFAULT_SHEET_URL,
                         help="Override the published CSV URL")
    parser.add_argument("--apply", action="store_true",
                         help="Actually write the file. Without this, only "
                              "a diff is printed and nothing is written.")
    parser.add_argument("--force", action="store_true",
                         help="Override the 'every row would be removed' "
                              "safety check. Only needed the first time you "
                              "replace placeholder/seed data with a real "
                              "pull -- a full wipeout is expected then, not "
                              "a sign of a bad fetch.")
    args = parser.parse_args()

    print(f"Fetching: {args.url}")
    try:
        new_rows = fetch_and_convert(args.url)
    except Exception as e:
        print(f"ERROR: failed to fetch/parse the sheet: {e}")
        sys.exit(1)

    if not new_rows:
        print("ERROR: fetched 0 usable rows -- refusing to touch the "
              "existing file. Check the sheet/URL before retrying.")
        sys.exit(1)

    old_rows = _load_current()
    added, removed, changed, unchanged = _diff(old_rows, new_rows)

    print()
    print(f"Current file:  {len(old_rows)} row(s)  ({_TABLE_PATH})")
    print(f"Fetched sheet: {len(new_rows)} row(s)")
    print()
    print(f"  + {len(added)} added")
    for rid in added[:20]:
        print(f"      {rid}")
    if len(added) > 20:
        print(f"      ... and {len(added) - 20} more")

    print(f"  - {len(removed)} removed")
    for rid in removed[:20]:
        print(f"      {rid}")
    if len(removed) > 20:
        print(f"      ... and {len(removed) - 20} more")

    print(f"  ~ {len(changed)} changed")
    for rid in changed[:20]:
        print(f"      {rid}")
    if len(changed) > 20:
        print(f"      ... and {len(changed) - 20} more")

    print(f"  = {unchanged} unchanged")
    print()

    if not args.apply:
        print("Dry run only -- nothing written. Re-run with --apply to write.")
        return

    if removed and len(removed) == len(old_rows) and old_rows and not args.force:
        print("REFUSING TO APPLY: every existing row would be removed. "
              "This usually means the sheet was fetched mid-edit/mid-migration "
              "or the wrong tab got published. If this is expected (e.g. "
              "replacing placeholder/seed data with a real pull for the "
              "first time), re-run with --force to proceed.")
        sys.exit(1)

    _TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if _TABLE_PATH.exists():
        backup_path = _TABLE_PATH.with_name(
            f"{_TABLE_PATH.stem}.bak.{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        )
        shutil.copy2(_TABLE_PATH, backup_path)
        print(f"Backed up previous file to: {backup_path}")

    tmp_path = _TABLE_PATH.with_suffix(".json.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(new_rows, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(_TABLE_PATH)  # atomic on POSIX and Windows

    print(f"Wrote {len(new_rows)} row(s) to {_TABLE_PATH}")


if __name__ == "__main__":
    main()
