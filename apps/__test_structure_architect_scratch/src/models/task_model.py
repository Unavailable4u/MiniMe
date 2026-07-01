import copy

class TaskModel:
    def __init__(self, attributes, validation_rules):
        """
        Defines and validates task data structure.

        Args:
            attributes (dict): The data representing the task.
            validation_rules (dict): Rules for validation.
                                     Expected keys: 'required' (list of str),
                                                    'types' (dict mapping str to type).
        """
        if not isinstance(attributes, dict):
            raise ValueError("Task attributes must be a dictionary.")
        if not isinstance(validation_rules, dict):
            raise ValueError("Validation rules must be a dictionary.")

        # Store a deep copy to prevent external mutation after validation
        self._attributes = copy.deepcopy(attributes)
        self._validation_rules = validation_rules
        self._validate()

    def _validate(self):
        required_fields = self._validation_rules.get('required', [])
        type_rules = self._validation_rules.get('types', {})

        # Validate missing required fields
        for field in required_fields:
            if field not in self._attributes:
                raise ValueError(f"Validation Error: Missing required field '{field}'")

        # Validate types
        for field, expected_type in type_rules.items():
            if field in self._attributes:
                if not isinstance(self._attributes[field], expected_type):
                    raise ValueError(
                        f"Validation Error: Invalid type for field '{field}'. "
                        f"Expected {expected_type.__name__}, "
                        f"got {type(self._attributes[field]).__name__}"
                    )

    def get_attributes(self):
        """Returns a deep copy of the validated task attributes to maintain immutability."""
        return copy.deepcopy(self._attributes)

    def to_dataclass(self, dataclass_type):
        """Converts the stored attributes to an instance of the provided dataclass.

        This helper method offers a compatibility layer for code that expects a
        specific dataclass representation (e.g., a ``Task`` dataclass used by a
        repository). The caller must supply the dataclass type; the method will
        instantiate it using the validated attributes.
        """
        if not hasattr(dataclass_type, "__dataclass_fields__"):
            raise TypeError("Provided type is not a dataclass.")
        # Only pass fields that the dataclass expects to avoid unexpected
        # keyword arguments.
        valid_fields = {k: v for k, v in self._attributes.items() if k in dataclass_type.__dataclass_fields__}
        return dataclass_type(**valid_fields)
