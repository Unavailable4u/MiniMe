import json
from typing import Any, Dict, List, Tuple


class TaskValidator:
    """
    Validates a parsed task against the provided game state.

    The validator checks:
    - `parsed_task` is a dictionary with required keys.
    - `current_game_state` is a dictionary.
    - The task's action is permitted by the game state.
    - Any additional constraints defined in the game state.

    The validation result is a dictionary:
        {
            "is_valid": bool,
            "errors": List[str]
        }
    """

    REQUIRED_TASK_KEYS = {"action"}

    @staticmethod
    def _ensure_dict(obj: Any, name: str) -> Tuple[bool, str]:
        if isinstance(obj, dict):
            return True, ""
        return False, f"{name} must be a dictionary."

    @staticmethod
    def _validate_required_keys(task: Dict[str, Any]) -> List[str]:
        missing = [k for k in TaskValidator.REQUIRED_TASK_KEYS if k not in task]
        if missing:
            # Return a separate error message for each missing key
            return [f"Missing required task key: {k}" for k in missing]
        return []

    @staticmethod
    def _validate_action(task: Dict[str, Any], game_state: Dict[str, Any]) -> List[str]:
        action = task.get("action")
        allowed_actions = game_state.get("allowed_actions")
        if allowed_actions is None:
            return ["Game state does not define 'allowed_actions'."]
        if not isinstance(allowed_actions, (list, set, tuple)):
            return ["'allowed_actions' in game state must be a list, set, or tuple."]
        if action not in allowed_actions:
            return [f"Action '{action}' is not allowed in the current game state."]
        return []

    @staticmethod
    def _validate_constraints(task: Dict[str, Any], game_state: Dict[str, Any]) -> List[str]:
        """
        Placeholder for additional rule checks.
        For now, this method checks for a simple resource constraint:
        - If task includes a 'cost' field, ensure the player has enough resources.
        """
        errors: List[str] = []
        cost = task.get("cost")
        if cost is None:
            return errors

        if not isinstance(cost, dict):
            return ["Task 'cost' must be a dictionary mapping resource names to amounts."]

        resources = game_state.get("player_resources", {})
        if not isinstance(resources, dict):
            return ["Game state 'player_resources' must be a dictionary."]

        for res, amount in cost.items():
            available = resources.get(res, 0)
            if not isinstance(amount, (int, float)):
                errors.append(f"Cost amount for resource '{res}' must be numeric.")
                continue
            if available < amount:
                errors.append(
                    f"Insufficient '{res}': required {amount}, available {available}."
                )
        return errors

    @classmethod
    def validate(cls, parsed_task: Any, current_game_state: Any) -> Dict[str, Any]:
        """
        Perform validation and return a result dict.

        Parameters
        ----------
        parsed_task : Any
            The task to validate, expected to be a dict.
        current_game_state : Any
            The current game state, expected to be a dict.

        Returns
        -------
        dict
            {"is_valid": bool, "errors": List[str]}
        """
        errors: List[str] = []

        # Basic type checks
        ok, msg = cls._ensure_dict(parsed_task, "parsed_task")
        if not ok:
            errors.append(msg)
            return {"is_valid": False, "errors": errors}
        ok, msg = cls._ensure_dict(current_game_state, "current_game_state")
        if not ok:
            errors.append(msg)
            return {"is_valid": False, "errors": errors}

        # Work with mutable copies to avoid mutating caller data
        task: Dict[str, Any] = dict(parsed_task)
        game_state: Dict[str, Any] = dict(current_game_state)

        # Compatibility shim: some parsers may use "type" instead of "action"
        if "action" not in task and "type" in task:
            task["action"] = task["type"]

        # Required keys
        errors.extend(cls._validate_required_keys(task))

        # Action validation
        errors.extend(cls._validate_action(task, game_state))

        # Additional constraints
        errors.extend(cls._validate_constraints(task, game_state))

        is_valid = not errors
        return {"is_valid": is_valid, "errors": errors}


# Example usage (can be removed or adapted as needed)
if __name__ == "__main__":
    # Sample data
    sample_task = {
        "action": "build_settlement",
        "cost": {"wood": 1, "brick": 1, "wheat": 1, "sheep": 1}
    }

    sample_game_state = {
        "allowed_actions": ["build_settlement", "trade", "roll_dice"],
        "player_resources": {"wood": 2, "brick": 2, "wheat": 0, "sheep": 1}
    }

    result = TaskValidator.validate(sample_task, sample_game_state)
    print(json.dumps(result, indent=2))
