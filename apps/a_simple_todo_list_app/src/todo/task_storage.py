import uuid

class TaskStorage:
    """
    Handles task data storage and retrieval.
    """

    def __init__(self, storage_medium):
        """
        Initializes the TaskStorage.
        
        :param storage_medium: A dictionary-like object used for storage. 
                               Must support __setitem__ and __getitem__, or be None to simulate unavailability.
        """
        if storage_medium is not None:
            if not hasattr(storage_medium, '__setitem__') or not hasattr(storage_medium, '__getitem__'):
                raise ValueError("storage_medium must support dictionary-like operations.")
        self.storage = storage_medium

    def store_task(self, task_data):
        """
        Stores task data and returns a unique task ID.
        
        :param task_data: Dictionary containing task details.
        :return: str: The unique stored_task_id.
        :raises ValueError: If task_data is invalid or empty.
        :raises RuntimeError: If storage_medium is unavailable (simulated).
        """
        if not isinstance(task_data, dict):
            raise ValueError("task_data must be a dictionary.")
        if not task_data:
            raise ValueError("task_data cannot be empty.")

        if self.storage is None:
            raise RuntimeError("storage_medium_unavailable")

        task_id = str(uuid.uuid4())
        while task_id in self.storage:
            task_id = str(uuid.uuid4())

        try:
            self.storage[task_id] = task_data
            return task_id
        except Exception as e:
            raise RuntimeError(f"Failed to store task: {str(e)}")

    def retrieve_task(self, task_id):
        """
        Retrieves task data by ID.
        
        :param task_id: The unique ID of the task.
        :return: dict: The retrieved task data.
        :raises KeyError: If task_id is not found.
        :raises RuntimeError: If storage_medium is unavailable.
        """
        if self.storage is None:
            raise RuntimeError("storage_medium_unavailable")
            
        if task_id not in self.storage:
            raise KeyError(f"Task ID {task_id} not found.")
            
        return self.storage[task_id]

if __name__ == "__main__":
    # Testing the implementation
    mem_storage = {}
    ts = TaskStorage(mem_storage)

    # Case 1: Successful storage and retrieval
    test_data = {"title": "Complete Python implementation", "priority": "High"}
    try:
        t_id = ts.store_task(test_data)
        print(f"Stored Task ID: {t_id}")
        
        retrieved = ts.retrieve_task(t_id)
        print(f"Retrieved Data: {retrieved}")
        assert retrieved == test_data
        print("Test 1 Passed: Success")
    except Exception as e:
        print(f"Test 1 Failed: {e}")

    # Case 2: Data Validation Error (Invalid input type)
    try:
        ts.store_task(["not", "a", "dict"])
        print("Test 2 Failed: Did not raise ValueError")
    except ValueError as e:
        print(f"Test 2 Passed: Caught expected error -> {e}")

    # Case 3: Data Validation Error (Empty dict)
    try:
        ts.store_task({})
        print("Test 3 Failed: Did not raise ValueError for empty dict")
    except ValueError as e:
        print(f"Test 3 Passed: Caught expected error -> {e}")

    # Case 4: Task ID not found (KeyError)
    try:
        ts.retrieve_task("non_existent_id")
        print("Test 4 Failed: Did not raise KeyError")
    except KeyError as e:
        print(f"Test 4 Passed: Caught expected error -> {e}")

    # Case 5: Storage Medium Unavailable
    ts_broken = TaskStorage(None)
    try:
        ts_broken.store_task({"task": "fail"})
    except RuntimeError as e:
        if str(e) == "storage_medium_unavailable":
            print("Test 5 Passed: Caught storage_medium_unavailable")
        else:
            print(f"Test 5 Failed: Unexpected error -> {e}")

    # Case 6: Collision resistance (Simulated via manually setting a key)
    try:
        collision_id = "fixed_id"
        mem_storage[collision_id] = {"collision": True}
        new_id = ts.store_task({"new_task": True})
        assert new_id != collision_id
        print("Test 6 Passed: Collision handled via UUID")
    except Exception as e:
        print(f"Test 6 Failed: {e}")