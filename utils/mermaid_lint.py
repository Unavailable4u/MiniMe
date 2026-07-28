"""
utils/mermaid_lint.py — rendering audit fix.

agents/mind_mapper.py and agents/workflow_suggester.py both extract a
```mermaid fenced block from an LLM's answer with nothing stronger than
"does the fence exist" (see each file's own _MERMAID_BLOCK_RE). Neither
module validates what's actually *inside* the fence, and both say so
explicitly in their docstrings/comments -- a real Mermaid syntax error
"isn't something regex validation can catch anyway." That's true for a
full grammar-level check (this repo has no Mermaid parser available on
the Python side, and standing one up via a Node subprocess for a
best-effort lint is a lot of moving parts for what's ultimately a
best-effort check), but it understates what a *heuristic* check can
still catch cheaply: empty content, a missing/unrecognized diagram-type
header, unbalanced brackets/quotes, and a flowchart/graph with no edges
at all are all common, cheap-to-detect ways an LLM's Mermaid answer
comes back broken -- and they're exactly the kind of thing that was
previously sailing straight through to MermaidDiagram.jsx, which would
then hit mermaid.render()'s reject path and show the user a "couldn't
render this diagram" fallback with no server-side retry ever having been
attempted.

This is intentionally NOT a full parser. It can have false negatives
(a diagram that passes here can still fail to render -- e.g. a typo'd
node shape or an unknown arrow variant) and, much more rarely, false
positives are avoided by keeping every check conservative (only reject
things that are unambiguously wrong, never "looks unusual"). The goal is
just to catch the common, cheap cases before they're saved, so a retry
has a chance to produce something better instead of the fallback UI
being the first time anyone finds out the diagram is broken.
"""
import re

# Recognized top-of-diagram declarations. Mermaid is case-sensitive here
# (e.g. "flowchart", not "Flowchart"), and a leading %%{init: ...}%%
# directive or comment/blank lines are allowed before it.
_DIAGRAM_TYPE_RE = re.compile(
    r"^(flowchart|graph|mindmap|sequenceDiagram|classDiagram|stateDiagram(-v2)?|"
    r"erDiagram|journey|gantt|pie|gitGraph|quadrantChart|timeline|requirementDiagram)\b"
)

# Diagram types whose whole point is connecting nodes with edges -- a
# "flowchart" or "graph" with zero arrows is almost never a real answer,
# just prose that happened to get wrapped in a fence.
_EDGE_REQUIRED_TYPES = {"flowchart", "graph"}
_EDGE_TOKEN_RE = re.compile(r"--[->.]|===|-\.-")

_BRACKET_PAIRS = {"[": "]", "(": ")", "{": "}"}


def _strip_leading_directives(text: str) -> str:
    """Drops %%{init: ...}%% directives and %% comment lines, and blank
    lines, so the diagram-type check looks at the first real content
    line -- same allowance Mermaid itself makes."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("%%"):
            i += 1
            continue
        break
    return "\n".join(lines[i:])


def _brackets_balanced(text: str) -> bool:
    """Conservative bracket/quote balance check. Ignores brackets inside
    quoted labels (a label like ']' or '(' inside "..." is legal Mermaid
    and shouldn't trip this), and just checks each bracket type's count
    matches outside of quotes -- not full nesting order, since Mermaid's
    own grammar allows constructs (e.g. subgraph blocks) this module has
    no need to fully model just to catch "the model dropped a closing
    bracket."
    """
    in_quotes = False
    counts = {ch: 0 for pair in _BRACKET_PAIRS.items() for ch in pair}
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_quotes = not in_quotes
        elif not in_quotes and ch in counts:
            counts[ch] += 1
        i += 1
    if in_quotes:
        return False  # odd number of quotes -- an unterminated label
    for open_ch, close_ch in _BRACKET_PAIRS.items():
        if counts[open_ch] != counts[close_ch]:
            return False
    return True


def looks_valid_mermaid(text: str) -> bool:
    """Best-effort, deliberately conservative check that `text` (the
    bare Mermaid source, fence already stripped) is plausibly valid --
    NOT a substitute for mermaid.render() actually succeeding client
    side, just a cheap enough filter to make a server-side retry worth
    attempting. Returns False for things that are unambiguously broken;
    when in doubt, returns True (a diagram this function can't confidently
    call broken is still handed to the frontend, same as before -- this
    only tightens the obvious cases, it doesn't add new failure modes).
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if "```" in stripped:
        return False  # a leftover fence means the extraction regex grabbed something malformed

    body = _strip_leading_directives(stripped)
    match = _DIAGRAM_TYPE_RE.match(body)
    if not match:
        return False

    if not _brackets_balanced(stripped):
        return False

    diagram_type = match.group(1)
    if diagram_type in _EDGE_REQUIRED_TYPES and not _EDGE_TOKEN_RE.search(stripped):
        return False

    return True
