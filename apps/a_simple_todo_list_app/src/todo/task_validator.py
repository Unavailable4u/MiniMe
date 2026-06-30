from datetime import datetime
from typing import Any, Tuple, List

class TaskValidator:
    REQUIRED_FIELDS = ["title", "due_date"]
    FIELD_LIMITS = {
        "title": 100,
        "description": 500,
    }
    DATE_FORMAT = "%Y-%m-%d"

    @staticmethod
    def validate(task_data: Any) -> Tuple[bool, List[str]]:
        errors: List[str] = []

        if not isinstance(task_data, dict):
            errors.append("task_data must be a dictionary")
            return False, errors

        # Disallow unexpected keys (minor improvement)
        allowed_keys = set(TaskValidator.REQUIRED_FIELDS) | set(TaskValidator.FIELD_LIMITS.keys()) | {"metadata"}
        unexpected = set(task_data.keys()) - allowed_keys
        if unexpected:
            errors.append(f"Unexpected fields: {sorted(unexpected)}")

        for field in TaskValidator.REQUIRED_FIELDS:
            if field not in task_data:
                errors.append(f"Missing required field: '{field}'")
            elif not isinstance(task_data[field], str):
                errors.append(f"Field '{field}' must be a string")

        for field, max_len in TaskValidator.FIELD_LIMITS.items():
            if field in task_data and isinstance(task_data[field], str):
                if len(task_data[field]) > max_len:
                    errors.append(
                        f"Field '{field}' exceeds maximum length of {max_len} characters"
                    )

        if "due_date" in task_data and isinstance(task_data["due_date"], str):
            try:
                datetime.strptime(task_data["due_date"], TaskValidator.DATE_FORMAT)
            except ValueError:
                errors.append(
                    f"Invalid date format for 'due_date'. Expected {TaskValidator.DATE_FORMAT}"
                )

        return len(errors) == 0, errors