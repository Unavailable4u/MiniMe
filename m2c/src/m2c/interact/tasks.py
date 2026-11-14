"""
m2c/src/m2c/interact/tasks.py -- Patch L01a: the ``tasks`` list, its validation and the interface a screen implements.

The model keeps a task list for the work in hand and sends the WHOLE list on every call; the new list replaces the old
one and an empty list clears it.  ``parse_tasks`` turns the arguments into a tuple of frozen ``Task`` values or fails
closed with a FIXED message that names the rule and never repeats an id or a title (they are the model's text).

What a JSON schema cannot say is checked here: ids are unique, every ``depends_on`` names a task in the same list and
the dependencies do not form a cycle.  The rules run in a fixed order and the first one that fails is reported:

    list shape -> too many -> each task (shape, id, title, status, depends_on) -> duplicate ids ->
    unknown dependency -> cycle

The list is shown to the person by a ``TaskSink`` (the screen; L01b implements one for the terminal UI) and recorded in
the local event store (type ``tasks``, see the runner).  A task's title is text the model wrote and goes to a terminal,
so it holds no control characters and is a single line.

Standard library only.

Place this file at: m2c/src/m2c/interact/tasks.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from . import has_control_chars

__all__ = [
    "BAD_DEPENDS_ON", "BAD_ID", "BAD_ITEM", "BAD_STATUS", "BAD_TITLE", "CYCLE", "DUPLICATE_IDS", "EVENT_TASKS",
    "MAX_TASKS", "NOT_A_LIST", "STATUSES", "TOO_MANY", "Task", "TaskListError", "TaskList", "TaskSink",
    "UNKNOWN_DEPENDENCY", "parse_tasks", "summarize",
]

# protocol/v1/tools/tasks.json
MAX_TASKS = 30
MAX_ID_CHARS = 64
MAX_TITLE_CHARS = 200
STATUSES = ("pending", "in_progress", "done", "blocked")
# The local event type under which each update is recorded (m2c.store.events).
EVENT_TASKS = "tasks"

_PREFIX = "error: invalid task list: "
NOT_A_LIST = _PREFIX + "tasks must be a list"
TOO_MANY = _PREFIX + f"at most {MAX_TASKS} tasks are allowed"
BAD_ITEM = _PREFIX + "each task must be an object with an id, a title, a status and optionally depends_on, and nothing else"
BAD_ID = _PREFIX + f"a task id must be 1 to {MAX_ID_CHARS} characters of text on one line"
BAD_TITLE = _PREFIX + f"a task title must be 1 to {MAX_TITLE_CHARS} characters of text on one line"
BAD_STATUS = _PREFIX + "a task status must be pending, in_progress, done or blocked"
BAD_DEPENDS_ON = _PREFIX + f"depends_on must be a list of at most {MAX_TASKS - 1} distinct task ids"
DUPLICATE_IDS = _PREFIX + "task ids must be unique"
UNKNOWN_DEPENDENCY = _PREFIX + "depends_on must name tasks that are in the same list"
CYCLE = _PREFIX + "dependencies must not form a cycle"

_KEYS_REQUIRED = {"id", "title", "status"}
_KEYS_ALLOWED = _KEYS_REQUIRED | {"depends_on"}


class TaskListError(Exception):
    """A rule failed.  ``message`` is fixed text (also ``str(error)``) that never repeats the model's words."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    status: str
    depends_on: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """The wire and event shape: ``depends_on`` is always present, as a list."""
        return {"id": self.id, "title": self.title, "status": self.status, "depends_on": list(self.depends_on)}


TaskList = tuple[Task, ...]

# A screen implements this: a plain function that takes the whole new list and shows it.  It is called on the event
# loop and must return quickly; it may raise, and the runner then tells the model the list was not shown.
TaskSink = Callable[[TaskList], None]


def _id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_ID_CHARS and not has_control_chars(value)


def _task(raw: Any) -> Task:
    if not isinstance(raw, dict) or not all(isinstance(k, str) for k in raw):
        raise TaskListError(BAD_ITEM)
    keys = set(raw)
    if not _KEYS_REQUIRED <= keys or not keys <= _KEYS_ALLOWED:
        raise TaskListError(BAD_ITEM)
    if not _id(raw["id"]):
        raise TaskListError(BAD_ID)
    title = raw["title"]
    if not (isinstance(title, str) and title.strip() and len(title) <= MAX_TITLE_CHARS and not has_control_chars(title)):
        raise TaskListError(BAD_TITLE)
    status = raw["status"]
    if not isinstance(status, str) or status not in STATUSES:
        raise TaskListError(BAD_STATUS)
    deps = raw.get("depends_on", [])
    if (not isinstance(deps, list) or len(deps) > MAX_TASKS - 1 or not all(_id(d) for d in deps)
            or len(set(deps)) != len(deps)):
        raise TaskListError(BAD_DEPENDS_ON)
    return Task(raw["id"], title, status, tuple(deps))


def _has_cycle(tasks: tuple[Task, ...]) -> bool:
    """Kahn's algorithm: a cycle is what is left when no task without an unmet dependency remains."""
    waiting = {t.id: len(t.depends_on) for t in tasks}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            dependents[d].append(t.id)
    ready = [i for i, n in waiting.items() if n == 0]
    done = 0
    while ready:
        current = ready.pop()
        done += 1
        for nxt in dependents[current]:
            waiting[nxt] -= 1
            if waiting[nxt] == 0:
                ready.append(nxt)
    return done != len(tasks)


def parse_tasks(raw: Any) -> TaskList:
    """The model's whole list as a tuple of ``Task``, or ``TaskListError``.  An empty list is valid (it clears)."""
    if not isinstance(raw, list):
        raise TaskListError(NOT_A_LIST)
    if len(raw) > MAX_TASKS:
        raise TaskListError(TOO_MANY)
    tasks = tuple(_task(item) for item in raw)
    ids = [t.id for t in tasks]
    if len(set(ids)) != len(ids):
        raise TaskListError(DUPLICATE_IDS)
    known = set(ids)
    if any(d not in known for t in tasks for d in t.depends_on):
        raise TaskListError(UNKNOWN_DEPENDENCY)
    if _has_cycle(tasks):
        raise TaskListError(CYCLE)
    return tasks


def summarize(tasks: TaskList) -> str:
    """The one line the model reads back: counts only, never a title."""
    if not tasks:
        return "Task list cleared."
    count = {s: sum(1 for t in tasks if t.status == s) for s in STATUSES}
    noun = "task" if len(tasks) == 1 else "tasks"
    return (f"Task list updated: {len(tasks)} {noun} ({count['pending']} pending, {count['in_progress']} in progress, "
            f"{count['done']} done, {count['blocked']} blocked).")
