import typing

class TaskModel:
    """Defines the structure and validation of tasks"""

    def __init__(self, task_data: typing.Dict, validation_rules: typing.Dict):
        """
        Initializes the TaskModel.
        :param task_data: Dictionary containing task attributes.
        :param validation_rules: Dictionary defining required fields and their expected types.
        """
        if not isinstance(task_data, dict):
            raise ValueError("task_data must be a dictionary")
        if not isinstance(validation_rules, dict):
            raise ValueError("validation_rules must be a dictionary")
            
        self.task_data = task_data
        self.validation_rules = validation_rules

    def validate(self) -> typing.Dict:
        """
        Validates the task data against the provided rules.
        :return: The validated task data.
        :raises ValueError: If the task is empty, missing required fields, or has invalid data types.
        """
        # Edge case: empty_task
        if not self.task_data:
            raise ValueError("Task data cannot be empty")

        validated_task = {}

        # Edge case: missing_required_fields and invalid_task_data
        for field, expected_type in self.validation_rules.items():
            if field not in self.task_data:
                raise ValueError(f"Missing required field: {field}")
            
            value = self.task_data[field]
            if not isinstance(value, expected_type):
                raise ValueError(f"Invalid data type for {field}. Expected {expected_type}, got {type(value)}")
            
            validated_task[field] = value

        # Include optional fields that are not in validation_rules but present in task_data
        for field, value in self.task_data.items():
            if field not in validated_task:
                validated_task[field] = value

        return validated_task

def run_task_model(task_data: typing.Dict, validation_rules: typing.Dict) -> typing.Dict:
    """
    Helper function to instantiate the model and return the validated task.
    """
    model = TaskModel(task_data, validation_rules)
    return model.validate()

if __name__ == "__main__":
    # Test cases
    rules = {"title": str, "priority": int}

    # Valid case
    try:
        print(run_task_model({"title": "Finish report", "priority": 1, "note": "Urgent"}, rules))
    except Exception as e:
        print(f"Error: {e}")

    # Edge case: empty_task
    try:
        run_task_model({}, rules)
    except ValueError as e:
        print(f"Caught expected error (empty): {e}")

    # Edge case: missing_required_fields
    try:
        run_task_model({"title": "No priority"}, rules)
    except ValueError as e:
        print(f"Caught expected error (missing): {e}")

    # Edge case: invalid_task_data (wrong type)
    try:
        run_task_model({"title": "Wrong type", "priority": "High"}, rules)
    except ValueError as e:
        print(f"Caught expected error (invalid type): {e}")