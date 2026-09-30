"""
eo/code_element_context.py — W7.1a (Build Workbench plan, "Element chip →
agent context"): what the server keeps of the `element` half of a
`kind: "element"` code-context ref.

The browser builds that object from a click inside the preview iframe
(frontend/app/lib/workbench/elementRef.js: tag, classes, text, computed
styles, `dynamic`, `instanceCount`). It already caps lengths client-side,
but the request body is whatever a caller sends, and the page in the
iframe is the person's own (possibly hostile, possibly just odd) page —
so the server re-validates everything before it is stored on a proposal
or written into a prompt. This module is the one place that happens.

Pure functions, no imports from the rest of the app, so both
agents/code_editor.py (prompt) and eo/code_proposals.py (what is stored
on the proposal row) can use it without an import cycle.
"""
import re

MAX_TAG_CHARS = 40
MAX_CLASSES = 20
MAX_CLASS_CHARS = 80
MAX_TEXT_CHARS = 200
MAX_STYLE_VALUE_CHARS = 120
MAX_INSTANCE_COUNT = 100_000

# The computed-style keys the inspector sends (inspectorRuntime.js /
# elementRef.js's STYLE_KEYS), kept in the client's camelCase so a
# sanitized element has exactly the shape the chip already has.
# css_property() turns one into the name the model should see.
STYLE_KEYS = ("color", "background", "fontSize", "padding", "margin", "display")

def css_property(key: str) -> str:
    """`fontSize` -> `font-size`."""
    return re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), key)


_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _clean(value, max_chars: int) -> str:
    """One line, no control characters, capped. Non-strings become ''."""
    if not isinstance(value, str):
        return ""
    return " ".join(_CONTROL.sub(" ", value).split())[:max_chars]


def sanitize_element(raw):
    """The trustworthy subset of an element chip's `element` object, or
    None when `raw` has nothing usable.

        {"tag": str, "classes": [str, ...], "textPreview": str,
         "styles": {camelCaseKey: value}, "dynamic": bool,
         "instanceCount": int >= 1}

    Same shape and key names the chip carries on the client, so what is
    stored on a proposal can be handed straight back to the UI. Class
    names that contain whitespace are dropped (a DOM class token cannot),
    as is a `tag` that is not a plain element name.
    """
    if not isinstance(raw, dict):
        return None

    # Too long is dropped, not truncated: a cut-off name is a wrong name.
    tag = _clean(raw.get("tag"), MAX_TAG_CHARS + 1)
    if len(tag) > MAX_TAG_CHARS or not _TAG.match(tag):
        tag = ""

    classes: list = []
    raw_classes = raw.get("classes")
    if isinstance(raw_classes, list):
        for c in raw_classes:
            if not isinstance(c, str):
                continue
            c = _CONTROL.sub(" ", c).strip()
            if not c or len(c) > MAX_CLASS_CHARS or any(ch.isspace() for ch in c):
                continue
            if c not in classes:
                classes.append(c)
            if len(classes) >= MAX_CLASSES:
                break

    styles: dict = {}
    raw_styles = raw.get("styles")
    if isinstance(raw_styles, dict):
        for key in STYLE_KEYS:
            value = _clean(raw_styles.get(key), MAX_STYLE_VALUE_CHARS)
            if value:
                styles[key] = value

    text = _clean(raw.get("textPreview"), MAX_TEXT_CHARS)

    count = raw.get("instanceCount")
    if isinstance(count, bool) or not isinstance(count, (int, float)) or count != count \
            or count in (float("inf"), float("-inf")):
        count = 1
    instance_count = min(max(int(count), 1), MAX_INSTANCE_COUNT)

    if not (tag or classes or text or styles):
        return None
    return {
        "tag": tag,
        "classes": classes,
        "textPreview": text,
        "styles": styles,
        "dynamic": raw.get("dynamic") is True,
        "instanceCount": instance_count,
    }


def normalize_element_refs(refs) -> list:
    """`refs` with every `kind: "element"` ref's `element` replaced by its
    sanitized form (or dropped when nothing usable is left), and any stray
    `element` key on other kinds removed. Refs are copied, never mutated;
    everything else on a ref passes through untouched. What
    eo/code_proposals.py stores on the proposal row is this list, so a
    caller cannot park an arbitrary blob in the `refs` column through the
    element field."""
    out = []
    for ref in refs or []:
        if not isinstance(ref, dict) or "element" not in ref:
            out.append(ref)
            continue
        copy = {k: v for k, v in ref.items() if k != "element"}
        if ref.get("kind") == "element":
            clean = sanitize_element(ref.get("element"))
            if clean is not None:
                copy["element"] = clean
        out.append(copy)
    return out
