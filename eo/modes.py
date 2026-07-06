"""
eo/modes.py — Execution Modes (blueprint §8). Sits between eo/panel.py's
staff_task() output and eo/executor.py's execute_graph() call. Decides how
many hires from staff_task()'s candidate list actually get used, and what
happens when a mode hits its ceiling.
"""
from eo.router import MODE_CEILINGS

def apply_mode(mode: str, hires: list, assessed_max: int) -> dict:
    """
    Returns {"hires": [...], "ceiling_hit": bool, "action": str|None}.
    `assessed_max` is the top of the Inspector's assumed agent-count range
    for this task (e.g. 4, from a "2-4 agents" assessment).
    """
    mode = mode.lower()
    if mode == "beast":
        target_count = min(round(assessed_max * 2.5), len(hires) if hires else round(assessed_max * 2.5))
        return {"hires": hires[:target_count], "ceiling_hit": False, "action": None}

    ceiling = MODE_CEILINGS.get(mode)
    if ceiling is None or len(hires) <= ceiling:
        return {"hires": hires, "ceiling_hit": False, "action": None}

    # Ceiling hit
    if mode == "auto":
        return {"hires": hires[:ceiling], "ceiling_hit": True,
                "action": "offer_beast_mode"}
    # simple / fast
    return {"hires": [], "ceiling_hit": True,
            "action": "stop_ask_beast_mode"}