import uuid

class TaskService:
    def __init__(self, task_model):
        """
        Initialize the TaskService.
        :param task_model: An object providing data persistence for tasks. 
                           Must implement get_by_id, save, and check_exists.
        """
        if task_model is None:
            raise ValueError("task_model cannot be None")
        self.task_model = task_model

    def create_task(self, user_input, user_permissions=None):
        """
        Creates a new task based on user input.
        """
        # Input validation
        if not user_input or not isinstance(user_input, dict):
            raise ValueError("Invalid user_input: must be a non-empty dictionary")
        
        if 'title' not in user_input:
            raise ValueError("user_input must contain a 'title'")

        # Insufficient permissions check
        if user_permissions is not None and not user_permissions.get('can_create', False):
            raise PermissionError("insufficient_permissions: User cannot create tasks")

        # Task already exists check (based on title)
        if self.task_model.check_exists(user_input['title']):
            raise KeyError("task_already_exists: A task with this title already exists")

        # Business logic for creation
        task_data = {
            'id': str(uuid.uuid4()),
            'title': user_input['title'],
            'description': user_input.get('description', ''),
            'status': 'pending'
        }
        
        created_task = self.task_model.save(task_data)
        return created_task

    def update_task(self, task_id, user_input, user_permissions=None):
        """
        Updates an existing task.
        """
        # Input validation
        if not task_id or not isinstance(user_input, dict):
            raise ValueError("Invalid inputs: task_id and user_input are required")

        # Insufficient permissions check
        if user_permissions is not None and not user_permissions.get('can_edit', False):
            raise PermissionError("insufficient_permissions: User cannot edit tasks")

        # Check if task exists
        existing_task = self.task_model.get_by_id(task_id)
        if not existing_task:
            raise KeyError(f"Task with id {task_id} not found")

        # Update fields
        existing_task.update(user_input)
        updated_task = self.task_model.save(existing_task)
        return updated_task

# --- Mock Model for runnability ---
class MockTaskModel:
    def __init__(self):
        self.store = {}

    def check_exists(self, title):
        return any(t['title'] == title for t in self.store.values())

    def get_by_id(self, task_id):
        return self.store.get(task_id)

    def save(self, task_data):
        self.store[task_data['id']] = task_data
        return task_data

if __name__ == "__main__":
    # Example usage
    model = MockTaskModel()
    service = TaskService(model)
    
    # Valid creation
    try:
        task = service.create_task({'title': 'Finish Report', 'description': 'Quarterly report'}, {'can_create': True})
        print(f"Created: {task}")
    except Exception as e:
        print(f"Error: {e}")

    # Duplicate creation
    try:
        service.create_task({'title': 'Finish Report'}, {'can_create': True})
    except Exception as e:
        print(f"Caught expected error: {e}")

    # Permission error
    try:
        service.create_task({'title': 'New Task'}, {'can_create': False})
    except Exception as e:
        print(f"Caught expected error: {e}")