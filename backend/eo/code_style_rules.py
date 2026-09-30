"""
eo/code_style_rules.py — W7.1a (Build Workbench plan, "Element chip →
agent context"): the plan's `findStyleRules(classes)` helper. Given the
classes of an element the person clicked in the live preview, find the
stylesheet rules in the workspace that style it, so agents/code_editor.py
can show the model the rule (with real line numbers) and let it edit the
CSS when that is where the style lives — instead of guessing a
`className` change that a stylesheet rule then overrides.

Pure functions, no DB and no imports from the rest of the app (same
family as eo/code_edit_apply.py): the caller reads the files and hands
this module `{path: text}`.

What counts as a match (deliberately conservative — a wrong rule shown
to the model is worse than a missing one):

  - only `.class` selectors. Tag/`#id`/attribute selectors are not
    searched (the chip only carries classes).
  - the class must be in the SUBJECT of a selector — its last compound
    selector, i.e. the thing the rule actually styles. `.btn span` does
    not style a `.btn`; `.card .btn` and `.btn:hover` do. Classes inside
    `:not(...)` do not count.
  - a class name is matched whole: `.btn` never matches `.btn-primary`.
  - classes that aren't plain CSS identifiers (Tailwind's `md:px-4`,
    `w-[10px]`, `w-1/2`) are never searched: they are not defined in the
    project's stylesheets — the change for those is in the element's own
    `className`, which is already in the model's view.

Known limits (documented rather than half-handled):
  - CSS Modules: the DOM carries the hashed name (`Button_btn__x7f`), the
    stylesheet carries `.btn`. Not matched.
  - Sass suffix nesting (`.btn { &-primary {} }`) is not resolved; the
    parent `.btn { ... }` block is matched, its nested child is not
    matched separately.
  - CSS-in-JS template literals are not stylesheets and are not scanned.

The scanner is a tolerant single pass (comments, strings, `url()`,
`#{}` interpolation, unbalanced braces); it never raises — a file it
cannot make sense of simply yields fewer rules.
"""
import re
from dataclasses import dataclass

STYLESHEET_EXTS = (".css", ".scss", ".sass", ".less")
# `<style>` blocks live inside these.
EMBEDDED_STYLE_EXTS = (".html", ".htm", ".vue", ".svelte")

MAX_STYLE_FILES = 30
# A file bigger than this is not scanned (the caller should also skip
# reading it — see agents/code_editor.py).
MAX_STYLE_FILE_CHARS = 200_000
MAX_RULES = 12
MAX_RULE_LINES = 80
MAX_SELECTOR_CHARS = 160
# Blocks nested deeper than this are not real stylesheets (real Sass rarely
# passes 6). Scanning stops there instead of tracking an unbounded stack.
MAX_NEST_DEPTH = 40

_SKIP_DIRS = {"node_modules", "dist", "build", ".next", ".nuxt", ".svelte-kit",
              "out", "coverage", "vendor", ".git"}

# A plain CSS class identifier. Anything else (`md:px-4`, `w-[10px]`) is a
# utility-framework class and is not searched for in stylesheets.
_CLASS_NAME = re.compile(r"^-?[_A-Za-z][_A-Za-z0-9-]*$")
_NOT_GROUP = re.compile(r":not\([^()]*\)", re.IGNORECASE)
# `<style ...>` opening tag; the attribute run is bounded so a file full of
# `<style` with no `>` cannot make every attempt scan to the end of the text.
_STYLE_OPEN = re.compile(r"<style\b[^>]{0,300}>", re.IGNORECASE)
_STYLE_CLOSE = re.compile(r"</style\s*>", re.IGNORECASE)


def _style_blocks(text: str):
    """Yield (start, end) offsets of the text inside each `<style>…</style>`.
    One forward pass: when an opening tag has no closing tag, no later one
    can have either, so scanning stops (a regex `<style>(.*?)</style>`
    re-scans to the end of the file for every unclosed opening tag)."""
    pos = 0
    while True:
        m = _STYLE_OPEN.search(text, pos)
        if m is None:
            return
        c = _STYLE_CLOSE.search(text, m.end())
        if c is None:
            return
        yield m.end(), c.start()
        pos = c.end()


@dataclass(frozen=True)
class StyleRule:
    path: str
    start_line: int            # 1-based, inclusive: first line of the selector text
    end_line: int              # 1-based, inclusive: the closing `}`
    selector: str              # the selector text, whitespace-collapsed and capped
    at_rules: tuple = ()       # enclosing at-rule preludes, outermost first ("@media (...)")
    classes: tuple = ()        # which of the asked-for classes this rule styles
    truncated: bool = False    # end_line was cut to MAX_RULE_LINES

    def label(self) -> str:
        where = f"line {self.start_line}" if self.start_line == self.end_line \
            else f"lines {self.start_line}-{self.end_line}"
        inside = f" (inside {' > '.join(self.at_rules)})" if self.at_rules else ""
        more = " (rule continues past what is shown)" if self.truncated else ""
        return f"{self.path} {where}: {self.selector}{inside}{more}"


def valid_class_name(name) -> bool:
    return isinstance(name, str) and len(name) <= 80 and bool(_CLASS_NAME.match(name))


def candidate_style_paths(paths, limit: int = MAX_STYLE_FILES) -> list:
    """The workspace paths worth scanning, stylesheets before files that
    merely embed a `<style>` block, each group sorted for determinism.
    Build output, vendored code and minified CSS are skipped."""
    sheets, embedded = [], []
    for p in paths:
        if not isinstance(p, str):
            continue
        parts = p.split("/")
        if any(seg in _SKIP_DIRS for seg in parts[:-1]):
            continue
        lower = p.lower()
        if lower.endswith((".min.css", ".min.scss")):
            continue
        if lower.endswith(STYLESHEET_EXTS):
            sheets.append(p)
        elif lower.endswith(EMBEDDED_STYLE_EXTS):
            embedded.append(p)
    return (sorted(sheets) + sorted(embedded))[:limit]


# ---------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------
@dataclass
class _Block:
    prelude: str
    start_line: int
    at_chain: tuple            # enclosing at-rule preludes, outermost first
    end_line: int = 0

    @property
    def child_chain(self) -> tuple:
        """`at_chain` as seen by a block nested directly inside this one."""
        return self.at_chain + (self.prelude,) if self.prelude.startswith("@") else self.at_chain


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _scan_blocks(css: str, line_offset: int = 0) -> list:
    """Every `prelude { ... }` block in `css`, nested ones included, each
    with its 1-based line span (shifted by `line_offset`, for `<style>`
    blocks inside a larger file). Line counting treats \\r\\n, \\n and a
    lone \\r as one break each — the same rule agents/code_editor.py uses
    to number the lines the model sees."""
    out: list = []
    stack: list = []
    buf: list = []
    buf_line = 0
    line = 1 + line_offset
    paren = 0
    i, n = 0, len(css)

    def reset():
        nonlocal buf, buf_line
        buf = []
        buf_line = 0

    while i < n:
        ch = css[i]
        nxt = css[i + 1] if i + 1 < n else ""

        if ch == "\n":
            line += 1
            if buf:
                buf.append(ch)
            i += 1
            continue
        if ch == "\r":
            if nxt != "\n":
                line += 1
                if buf:
                    buf.append("\n")
            i += 1
            continue

        if ch == "/" and nxt == "*":
            end = css.find("*/", i + 2)
            end = n if end == -1 else end + 2
            line += css.count("\n", i, end) + _lone_cr(css, i, end)
            i = end
            continue
        # `//` is a comment in Sass/Less only, and a slash-pair inside
        # `url(http://…)` or after a colon is not one.
        if ch == "/" and nxt == "/" and paren == 0 and (i == 0 or css[i - 1] != ":"):
            end = css.find("\n", i)
            i = n if end == -1 else end
            continue

        if ch in ("'", '"'):
            j = i + 1
            while j < n and css[j] not in ("\n", "\r"):
                if css[j] == "\\":
                    j += 2
                    continue
                if css[j] == ch:
                    j += 1
                    break
                j += 1
            j = min(j, n)
            if not buf:
                buf_line = line
            buf.append(css[i:j])
            i = j
            continue

        # Sass interpolation `#{...}` is part of a selector, not a block.
        if ch == "#" and nxt == "{":
            depth, j = 0, i + 1
            while j < n:
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            chunk = css[i:j]
            if not buf:
                buf_line = line
            buf.append(chunk)
            line += chunk.count("\n") + _lone_cr(chunk, 0, len(chunk))
            i = j
            continue

        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif paren == 0 and ch == "{":
            if len(stack) >= MAX_NEST_DEPTH:
                break           # not a real stylesheet; keep what was found
            prelude = _collapse("".join(buf))
            stack.append(_Block(prelude, buf_line or line,
                                stack[-1].child_chain if stack else ()))
            reset()
            i += 1
            continue
        elif paren == 0 and ch == "}":
            if stack:
                blk = stack.pop()
                blk.end_line = line
                out.append(blk)
            reset()
            i += 1
            continue
        elif paren == 0 and ch == ";":
            reset()
            i += 1
            continue

        if not buf and not ch.isspace():
            buf_line = line
        if buf or not ch.isspace():
            buf.append(ch)
        i += 1

    # Unbalanced input: close whatever is open at the last line.
    while stack:
        blk = stack.pop()
        blk.end_line = line
        out.append(blk)
    return out


def _lone_cr(text: str, start: int, end: int) -> int:
    """Count lone '\\r' (not followed by '\\n') in text[start:end]."""
    count = 0
    j = text.find("\r", start, end)
    while j != -1:
        if j + 1 >= len(text) or text[j + 1] != "\n":
            count += 1
        j = text.find("\r", j + 1, end)
    return count


# ---------------------------------------------------------------------------
# Selector matching
# ---------------------------------------------------------------------------
def _split_top_level(text: str, separators: str) -> list:
    """Split on any char in `separators` that is outside () and []."""
    parts, cur, depth = [], [], 0
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and ch in separators:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _subject_compound(selector: str) -> str:
    """The last compound selector of one complex selector (`.a > .b:hover`
    -> `.b:hover`), with `:not(...)` groups removed."""
    compounds = [c for c in _split_top_level(selector.strip(), " \t\n>+~") if c.strip()]
    if not compounds:
        return ""
    return _NOT_GROUP.sub("", compounds[-1])


def _class_regex(name: str):
    return re.compile(r"\." + re.escape(name) + r"(?![\w\\-])")


def _matched_classes(prelude: str, regexes: dict) -> list:
    subjects = [_subject_compound(s) for s in _split_top_level(prelude, ",")]
    return [name for name, rx in regexes.items() if any(rx.search(s) for s in subjects)]


def _rules_in_text(path: str, css: str, regexes: dict, line_offset: int,
                   max_rule_lines: int) -> list:
    rules = []
    for blk in _scan_blocks(css, line_offset):
        if not blk.prelude or blk.prelude.startswith("@"):
            continue
        hit = _matched_classes(blk.prelude, regexes)
        if not hit:
            continue
        end = blk.end_line
        truncated = False
        if end - blk.start_line + 1 > max_rule_lines:
            end = blk.start_line + max_rule_lines - 1
            truncated = True
        selector = blk.prelude
        if len(selector) > MAX_SELECTOR_CHARS:
            selector = selector[:MAX_SELECTOR_CHARS] + "…"
        rules.append(StyleRule(path, blk.start_line, end, selector, blk.at_chain,
                               tuple(hit), truncated))
    return rules


def find_style_rules(files: dict, classes, *, max_rules: int = MAX_RULES,
                     max_rule_lines: int = MAX_RULE_LINES) -> list:
    """Rules in `files` ({path: text}) whose subject selector includes one
    of `classes`. Returned best-first: rules matching more of the asked
    classes, then path and line order. A rule nested inside another
    returned rule is dropped (the outer block already contains it), and
    at most `max_rules` are returned. `.css/.scss/.sass/.less` files are
    scanned whole; `.html/.htm/.vue/.svelte` files only inside their
    `<style>` blocks. Anything else in `files` is ignored."""
    regexes = {c: _class_regex(c) for c in dict.fromkeys(classes or []) if valid_class_name(c)}
    if not regexes:
        return []
    # A rule can only match if `.name` appears literally in the text, so a
    # substring test skips the (much slower) scan for files that cannot.
    needles = tuple("." + c for c in regexes)
    found: list = []
    for path in sorted(files):
        text = files[path]
        if not isinstance(text, str) or not text or len(text) > MAX_STYLE_FILE_CHARS:
            continue
        if not any(n in text for n in needles):
            continue
        lower = path.lower()
        if lower.endswith(STYLESHEET_EXTS):
            found += _rules_in_text(path, text, regexes, 0, max_rule_lines)
        elif lower.endswith(EMBEDDED_STYLE_EXTS):
            for start, end in _style_blocks(text):
                offset = text.count("\n", 0, start) + _lone_cr(text, 0, start)
                found += _rules_in_text(path, text[start:end], regexes, offset, max_rule_lines)

    # Drop a rule that sits inside another returned rule of the same file
    # (the outer block already contains it). One sweep per file over spans
    # sorted outermost-first: a span is contained iff an earlier, different
    # span already reaches at least as far.
    found.sort(key=lambda r: (r.path, r.start_line, -r.end_line))
    kept: list = []
    i = 0
    while i < len(found):
        path, reach, j = found[i].path, 0, i
        while j < len(found) and found[j].path == path:
            k, span = j, (found[j].start_line, found[j].end_line)
            while k < len(found) and found[k].path == path \
                    and (found[k].start_line, found[k].end_line) == span:
                k += 1
            if span[1] > reach:
                kept.extend(found[j:k])
            reach = max(reach, span[1])
            j = k
        i = j
    seen, unique = set(), []
    for r in kept:
        key = (r.path, r.start_line, r.end_line)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.sort(key=lambda r: (-len(r.classes), r.path, r.start_line))
    return unique[:max(0, max_rules)]
