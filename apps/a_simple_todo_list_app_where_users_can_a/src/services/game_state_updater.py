import copy
from typing import Any, Dict

class InvalidGameStateError(Exception):
    """Raised when the provided game state is not valid."""
    pass

class TaskConflictError(Exception):
    """Raised when the task cannot be applied due to a conflict with the current game state."""
    pass

class InvalidParsedTaskError(Exception):
    """Raised when the parsed task does not conform to the expected schema."""
    pass

def _validate_game_state(game_state: Any) -> Dict:
    """
    Validate that the game state is a dictionary.
    Returns a shallow copy of the game state for safe modifications.
    Raises InvalidGameStateError if validation fails.
    """
    if not isinstance(game_state, dict):
        raise InvalidGameStateError("Game state must be a dictionary.")
    # The original implementation used deepcopy; keep that behavior for safety.
    return copy.deepcopy(game_state)

def _normalize_parsed_task(parsed_task: Any) -> Dict:
    """Normalize the output of various task parsers.

    The function accepts several common shapes produced by task parsers and
    converts them into a canonical ``{'action': <str>, 'details': <dict>}`` format.
    Supported keys:
        - ``action`` (preferred) or ``type`` – the name of the action.
        - ``details`` (preferred) or ``params`` – a dict of parameters for the action.
    Raises:
        InvalidParsedTaskError: If the task cannot be normalized because required
        information is missing or malformed.
    """
    if not isinstance(parsed_task, dict):
        raise InvalidParsedTaskError("Parsed task must be a dictionary.")

    # Extract action name
    action = parsed_task.get('action') or parsed_task.get('type')
    if not isinstance(action, str):
        raise InvalidParsedTaskError("Parsed task must contain a string 'action' (or 'type') field.")

    # Extract details – must be a dict if present
    details = parsed_task.get('details') or parsed_task.get('params') or {}
    if not isinstance(details, dict):
        raise InvalidParsedTaskError("The 'details' (or 'params') field must be a dictionary.")

    return {'action': action, 'details': details}

def _apply_add_item(state: Dict, details: Dict) -> None:
    """
    Add an item to the game state under the 'items' list.
    If the item already exists, raise TaskConflictError.
    """
    item = details.get('item')
    if item is None:
        raise ValueError("Details for add_item must include 'item'.")
    items = state.setdefault('items', [])
    if item in items:
        raise TaskConflictError(f"Item '{item}' already exists in game state.")
    items.append(item)

def _apply_remove_item(state: Dict, details: Dict) -> None:
    """
    Remove an item from the game state 'items' list.
    If the item does not exist, raise TaskConflictError.
    """
    item = details.get('item')
    if item is None:
        raise ValueError("Details for remove_item must include 'item'.")
    items = state.get('items', [])
    if item not in items:
        raise TaskConflictError(f"Item '{item}' not found in game state.")
    items.remove(item)

def _apply_update_score(state: Dict, details: Dict) -> None:
    """
    Update the 'score' field in the game state.
    If the resulting score would be negative, raise TaskConflictError.
    """
    delta = details.get('delta')
    if not isinstance(delta, (int, float)):
        raise ValueError("Details for update_score must include numeric 'delta'.")
    current_score = state.get('score', 0)
    new_score = current_score + delta
    if new_score < 0:
        raise TaskConflictError("Resulting score cannot be negative.")
    state['score'] = new_score

_ACTION_HANDLERS = {
    'add_item': _apply_add_item,
    'remove_item': _apply_remove_item,
    'update_score': _apply_update_score,
}

def update_game_state(parsed_task: Any, current_game_state: Any) -> Dict:
    """
    Update the game state based on a parsed task.

    Parameters:
        parsed_task: A dict describing the task. It may come from various parsers
                     and therefore can contain different field names. The
                     function normalizes it to an ``action`` and ``details`` pair.
        current_game_state: The current game state as a dict.

    Returns:
        A new dict representing the updated game state.

    Raises:
        InvalidGameStateError: If the provided game state is not a dict.
        InvalidParsedTaskError: If the parsed task is malformed.
        TaskConflictError: If the task cannot be applied due to a conflict.
    """
    # Validate and copy the game state
    state = _validate_game_state(current_game_state)
    # Normalize and validate the parsed task
    task = _normalize_parsed_task(parsed_task)

    action = task['action']
    details = task['details']

    if action not in _ACTION_HANDLERS:
        raise ValueError(f"Unsupported action '{action}'.")

    # Apply the action – details is guaranteed to be a dict by normalization
    handler = _ACTION_HANDLERS[action]
    handler(state, details)

    return state

__all__ = [
    "InvalidGameStateError",
    "TaskConflictError",
    "InvalidParsedTaskError",
    "update_game_state",
]
