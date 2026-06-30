class TaskDefinition:
    """Defines the structure and validation rules for tasks."""
    
    VALID_TASK_TYPES = {'basic', 'advanced', 'critical', 'routine'}
    
    REQUIRED_ATTRIBUTES = {
        'basic': {'name', 'description', 'deadline'},
        'advanced': {'name', 'description', 'deadline', 'priority'},
        'critical': {'name', 'description', 'deadline', 'priority', 'assignee'},
        'routine': {'name', 'description', 'schedule'}
    }
    
    def __init__(self, task_type, task_attributes):
        self.task_type = task_type
        self.task_attributes = task_attributes
        self.task_schema = None
        self.validation_rules = None
    
    def validate(self):
        if self.task_type not in self.VALID_TASK_TYPES:
            raise ValueError(f"Invalid task type: '{self.task_type}'. Valid types are: {self.VALID_TASK_TYPES}")
        
        if not isinstance(self.task_attributes, dict):
            raise ValueError("task_attributes must be a dictionary.")
        
        required_attrs = self.REQUIRED_ATTRIBUTES.get(self.task_type, set())
        missing_attrs = required_attrs - set(self.task_attributes.keys())
        
        if missing_attrs:
            raise ValueError(f"Missing required attributes for '{self.task_type}' task: {missing_attrs}")
        
        return self._generate_schema_and_rules()
    
    def _generate_schema_and_rules(self):
        # Simple sanitisation: strip whitespace from string values
        sanitized_attrs = {}
        for k, v in self.task_attributes.items():
            if isinstance(v, str):
                sanitized_attrs[k] = v.strip()
            else:
                sanitized_attrs[k] = v
        
        schema = {
            'task_type': self.task_type,
            'attributes': sanitized_attrs,
            'required_fields': list(self.REQUIRED_ATTRIBUTES[self.task_type])
        }
        
        rules = [
            f"Task type must be one of: {self.VALID_TASK_TYPES}",
            f"All required attributes for '{self.task_type}' must be present: {self.REQUIRED_ATTRIBUTES[self.task_type]}"
        ]
        
        self.task_schema = schema
        self.validation_rules = rules
        
        return self.task_schema, self.validation_rules
    
    def get_schema(self):
        if self.task_schema is None:
            self.validate()
        return self.task_schema
    
    def get_validation_rules(self):
        if self.validation_rules is None:
            self.validate()
        return self.validation_rules

if __name__ == "__main__":
    # Example usage
    try:
        td = TaskDefinition('basic', {'name': 'Test Task', 'description': 'A test', 'deadline': '2024-12-31'})
        print("Schema:", td.get_schema())
        print("Rules:", td.get_validation_rules())
    except ValueError as e:
        print(f"Error: {e}")