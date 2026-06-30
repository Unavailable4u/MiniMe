from TaskDefinition import TaskDefinition

class TaskCreationAPI:
    """
    Exposes task creation functionality through a creation API.
    """

    def __init__(self):
        # Simulated maximum allowed time for creation (seconds)
        self._max_creation_time = 2.0

    def create_task(self, task_definition, task_data):
        """
        Create a task based on the provided definition and data.

        Parameters
        ----------
        task_definition : dict or TaskDefinition
            Definition of the task to be created.
        task_data : dict
            Data required to instantiate the task.

        Returns
        -------
        tuple
            (created_task_id, task_status) where:
                - created_task_id (str): Unique identifier for the created task.
                - task_status (str): Status of the task after creation attempt.
        """
        # ----- Normalize task_definition -----
        if isinstance(task_definition, dict):
            definition_dict = task_definition
        elif isinstance(task_definition, TaskDefinition):
            # Validate the TaskDefinition instance and extract its schema
            task_definition.validate()
            definition_dict = task_definition.get_schema()
        else:
            raise ValueError("Invalid task definition: must be a dict or TaskDefinition instance")

        if not definition_dict:
            raise ValueError("Invalid task definition: must be a non-empty dict")
        if not isinstance(task_data, dict) or not task_data:
            raise ValueError("Insufficient data: must be a non-empty dict")

        # ----- Simulate Creation Timeout (if triggered) -----
        if task_data.get("_simulate_timeout", False):
            import time
            start = time.time()
            # Sleep longer than allowed to trigger timeout
            time.sleep(self._max_creation_time + 0.5)
            elapsed = time.time() - start
            if elapsed > self._max_creation_time:
                raise TimeoutError("Creation timeout exceeded")

        # ----- Task Creation Logic (mock) -----
        import uuid
        created_task_id = str(uuid.uuid4())
        task_status = "created"

        return created_task_id, task_status


# Example usage (can be removed or commented out when importing as a module)
if __name__ == "__main__":
    api = TaskCreationAPI()
    try:
        definition = {"type": "example", "priority": 1}
        data = {"payload": "sample"}
        task_id, status = api.create_task(definition, data)
        print(f"Created task ID: {task_id}, Status: {status}")
    except (ValueError, TimeoutError) as e:
        print(f"Error: {e}")