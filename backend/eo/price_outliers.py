"""
eo/price_outliers.py — Patch K.3 (MiniMe reliability guide, Phase K --
Pricing Pipeline): "After all part prices are collected, flag (not
silently drop) any line item whose price is more than ~5x the median of
same-category items, or whose price is missing while its duplicate-part
sibling ... has a price." This is the pricing-summary assembly step the
guide's own K.3 wording refers to -- nothing wired prices into any kind
of summary/aggregate before this patch (agents/hardware_speccer.py's own
_populate_prices() and api/routes/workspace_data.py's
refresh_part_prices() both just attach a price to each part and stop;
the Parts tab total itself is computed client-side, PartsTable.jsx's own
plain `.reduce()`).

Same "pure function first, plain dict/list in and out, no LLM, no
network" build order every other eo/ estimation module in this package
already establishes (eo/mech_material.py's resolve_material()/
estimate_print_cost_bdt(), eo/mech_mass.py's lookup_mass()) -- this
module only ever looks at prices/names/categories already sitting on
`parts`, never fetches or computes a new price itself.

Two independent flag conditions, per the guide's own "Change" wording
-- a part can only ever match the SECOND one (missing a price), never
the first (having an outlier price), so there's no real overlap/
precedence question between them in practice:

1. Outlier: `estimated_price_bdt > 5 * category_median`, where
   `category_median` is the median of every OTHER priced item in
   the same `category` (see _category_medians() below for exactly
   which items count). A category with fewer than two priced items
   has no meaningful median to compare against and never flags
   anything.

2. Asymmetric duplicate: a part with no price whose "duplicate-part
   sibling" (identical `category` + name once a standalone "left"/
   "right" word is stripped -- see _sibling_key() below) DOES have a
   price. Deliberately narrow: only whole-word "left"/"right" are
   stripped, not "l"/"r" abbreviations or any other side-indicating
   vocabulary -- same "narrow, documented keyword match, not a
   guessed-at fuzzy rule" posture eo/mech_material.py's own
   _is_strap_or_band() already holds itself to for a comparable
   free-text matching job.

Sets exactly two fields on every part it processes, per the guide's own
"surface flagged items to the UI with a badge" instruction --
`price_flagged` (bool) and `price_flag_reason` (str | None, a
human-readable sentence for a UI tooltip/badge title, not a bare error
code) -- overwriting whatever those two fields held before, so a part
that WAS flagged on a stale, unrefreshed price set correctly clears the
flag once a fresh price run resolves it. Every other field on each part
is left untouched.

Mutates `parts` in place AND returns it, same convention
agents/hardware_speccer.py's own _populate_prices() already establishes
for this exact pricing pipeline (a caller can use either the return
value or the list it already held a reference to).
"""
import re
import statistics

# Per this module's own docstring: ~5x is the guide's literal threshold
# ("more than ~5x the median"). Kept as a named constant, not a magic
# number inline, so a future patch adjusting the sensitivity has exactly
# one place to change it.
OUTLIER_RATIO_THRESHOLD = 5.0

# Whole-word only (via \b) so a part legitimately named "Leftover
# Bracket Stock" or "Right-Angle Header" doesn't get its real
# distinguishing word stripped out and collide with an unrelated part --
# same "cheap keyword check, not a guessed-at fuzzy rule" scope this
# docstring's own module-level comment above already flags.
_SIDE_WORD_RE = re.compile(r"\b(left|right)\b", re.IGNORECASE)


def _priced_value(part):
    """A part's own price as a positive float, or None for anything
    that isn't a genuine priced value yet (missing key, null, zero,
    negative, or a non-numeric value that slipped past upstream
    validation -- same "never trust a field's type, always re-check"
    posture PartsTable.jsx's own T2b bug-fix comment already documents
    for this identical field on the frontend side).
    """
    price = part.get("estimated_price_bdt") if isinstance(part, dict) else None
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return price if price > 0 else None


def _sibling_key(part):
    """(category, normalized_name) grouping key for asymmetric-duplicate
    detection -- two parts share a key when they're the same category
    and their own `name` is identical once a standalone "left"/"right"
    word is removed (case/whitespace-insensitive), e.g. "Left Motor
    Mounting Bracket" and "Right Motor Mounting Bracket" both normalize
    to "motor mounting bracket".

    Falls back to `generic_name` when `name` is missing/blank, same
    "canonical generic_name/aliases" fallback order eo/mech_material.py's
    own _is_strap_or_band() already uses for a comparable free-text
    match -- a part with neither field returns a key built from its own
    `id` instead, so it never spuriously collides with another
    similarly-nameless part.
    """
    category = part.get("category") if isinstance(part, dict) else None
    name = None
    if isinstance(part, dict):
        raw_name = part.get("name")
        name = raw_name if isinstance(raw_name, str) and raw_name.strip() else None
        if name is None:
            raw_generic = part.get("generic_name")
            name = raw_generic if isinstance(raw_generic, str) and raw_generic.strip() else None

    if name is None:
        # No usable free-text name at all -- key off `id` so this part
        # only ever "groups" with itself, never falsely matches another
        # equally-nameless part.
        return (category, f"__no_name__:{part.get('id') if isinstance(part, dict) else id(part)}")

    normalized = _SIDE_WORD_RE.sub("", name)
    normalized = " ".join(normalized.split()).lower()
    return (category, normalized)


def _category_medians(parts):
    """{category: median_price} for every category that has at least
    two priced items -- a category with 0 or 1 priced items has no
    meaningful median to flag anything against, so it's simply absent
    from the returned dict (callers treat a missing category the same
    as "never flag anything here").
    """
    by_category = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        price = _priced_value(part)
        if price is None:
            continue
        by_category.setdefault(part.get("category"), []).append(price)

    return {
        category: statistics.median(prices)
        for category, prices in by_category.items()
        if len(prices) >= 2
    }


def flag_price_outliers(parts: list) -> list:
    """K.3 entry point. See this module's own docstring above for the
    two flag conditions. Never raises: a malformed `parts` (not a list,
    or containing non-dict entries) is treated the same as "no anomalies
    to find" for the offending entries, same fail-safe posture every
    other pure function in this pricing pipeline already holds itself
    to (eo/mech_material.py's resolve_material()/
    estimate_print_cost_bdt()).
    """
    if not isinstance(parts, list):
        return parts

    medians = _category_medians(parts)

    # Group by sibling key ONLY (not also requiring >= 2 members up
    # front) so a lone part with no sibling at all correctly never
    # flags -- same "absence is the safe default" reasoning
    # _category_medians() above already applies for a too-small
    # category.
    sibling_groups = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        sibling_groups.setdefault(_sibling_key(part), []).append(part)

    for part in parts:
        if not isinstance(part, dict):
            continue

        price = _priced_value(part)
        flagged = False
        reason = None

        if price is not None:
            median = medians.get(part.get("category"))
            if median is not None and median > 0 and price > OUTLIER_RATIO_THRESHOLD * median:
                ratio = price / median
                flagged = True
                reason = (
                    f"Price is {ratio:.1f}\u00d7 the {part.get('category')} category "
                    f"median (\u09f3{median:,.0f}) \u2014 verify before trusting this figure."
                )
        else:
            # Only relevant when THIS part has no price of its own --
            # an already-priced part is never flagged as an "asymmetric
            # duplicate" just because a sibling also happens to be priced.
            siblings = sibling_groups.get(_sibling_key(part), [])
            priced_sibling = next(
                (sib for sib in siblings if sib is not part and _priced_value(sib) is not None),
                None,
            )
            if priced_sibling is not None:
                flagged = True
                sib_price = _priced_value(priced_sibling)
                reason = (
                    f"No price found, but \u2018{priced_sibling.get('name')}\u2019 "
                    f"(same part, other side) has \u09f3{sib_price:,.0f}."
                )

        part["price_flagged"] = flagged
        part["price_flag_reason"] = reason

    return parts
