class TaskDefinitionValidator:
    REQUIRED_FIELDS = ["name", "type", "version"]
    ALLOWED_TYPES = ["shell", "http", "docker", "schedule"]
    MIN_VERSION = 1
    MAX_VERSION = 100
    
    def __init__(self):
        self.error_messages = []
    
    def validate(self, task_definition):
        self.error_messages = []
        
        if not isinstance(task_definition, dict):
            self.error_messages.append("Task definition must be a dictionary")
            return False, self.error_messages
        
        self._validate_schema(task_definition)
        self._validate_required_fields(task_definition)
        self._validate_compatibility(task_definition)
        
        validation_status = len(self.error_messages) == 0
        return validation_status, self.error_messages
    
    def _validate_schema(self, task_definition):
        if task_definition is None:
            self.error_messages.append("Task definition cannot be None")
            return
        
        if not isinstance(task_definition, dict):
            self.error_messages.append("Invalid schema: task_definition must be an object")
            return
        
        unknown_fields = set(task_definition.keys()) - set(self.REQUIRED_FIELDS) - {"parameters", "schedule", "timeout"}
        if unknown_fields:
            self.error_messages.append(f"Unknown fields detected: {', '.join(unknown_fields)}")
    
    def _validate_required_fields(self, task_definition):
        for field in self.REQUIRED_FIELDS:
            if field not in task_definition:
                self.error_messages.append(f"Missing required field: {field}")
    
    def _validate_compatibility(self, task_definition):
        if "name" in task_definition:
            if not isinstance(task_definition["name"], str) or not task_definition["name"].strip():
                self.error_messages.append("Task name must be a non-empty string")
        
        if "type" in task_definition:
            if task_definition["type"] not in self.ALLOWED_TYPES:
                self.error_messages.append(f"Invalid task type. Allowed types: {self.ALLOWED_TYPES}")
        
        if "version" in task_definition:
            version = task_definition["version"]
            if not isinstance(version, int) or version < self.MIN_VERSION or version > self.MAX_VERSION:
                self.error_messages.append(f"Version must be an integer between {self.MIN_VERSION} and {self.MAX_VERSION}")
        
        if "parameters" in task_definition:
            if not isinstance(task_definition["parameters"], dict):
                self.error_messages.append("Parameters must be a dictionary")
        
        if "schedule" in task_definition and task_definition.get("type") == "schedule":
            schedule = task_definition["schedule"]
            if not isinstance(schedule, str) or not schedule.strip():
                self.error_messages.append("Schedule definition must be a non-empty string")
        
        if "timeout" in task_definition:
            timeout = task_definition["timeout"]
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                self.error_messages.append("Timeout must be a positive number")