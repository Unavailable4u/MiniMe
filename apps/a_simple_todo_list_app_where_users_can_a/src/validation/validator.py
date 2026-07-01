import collections
from typing import Any, Callable, Dict, List, Tuple, Set


class ValidationError(Exception):
    """Raised when validation of a field fails."""
    pass


class CircularDependencyError(Exception):
    """Raised when validation rules contain circular dependencies."""
    pass


class InvalidRuleError(Exception):
    """Raised when a validation rule is malformed."""
    pass


class Validator:
    """
    Determines the correct validation order for a GameBoardModel based on provided
    validation rules and applies the validators to the data.
    """

    @staticmethod
    def _validate_inputs(
        schema: Dict[str, Any],
        data: Dict[str, Any],
        rules: List[Dict[str, Any]]
    ) -> None:
        if not isinstance(schema, dict):
            raise TypeError("schema must be a dict")
        if not isinstance(data, dict):
            raise TypeError("data must be a dict")
        if not isinstance(rules, list):
            raise TypeError("rules must be a list")
        if not rules:
            raise ValueError("validation rules list cannot be empty")

    @staticmethod
    def _build_dependency_graph(
        rules: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Set[str]], Dict[str, Dict[str, Any]]]:
        """
        Returns a tuple (graph, rule_map)

        * graph: mapping from field -> set of dependent fields (edges directed from
          dependency to dependent)
        * rule_map: mapping from field -> rule dict
        """
        graph: Dict[str, Set[str]] = {}
        rule_map: Dict[str, Dict[str, Any]] = {}

        for rule in rules:
            if not isinstance(rule, dict):
                raise InvalidRuleError("Each rule must be a dict")
            if "field" not in rule or "validator" not in rule:
                raise InvalidRuleError("Rule must contain 'field' and 'validator' keys")

            field: str = rule["field"]
            depends: List[str] = rule.get("depends", [])
            if not isinstance(depends, list):
                raise InvalidRuleError("'depends' must be a list if present")

            # store rule
            rule_map[field] = rule

            # ensure node exists in graph
            graph.setdefault(field, set())

            # add edges from dependencies to this field
            for dep in depends:
                if not isinstance(dep, str):
                    raise InvalidRuleError("Dependency names must be strings")
                graph.setdefault(dep, set()).add(field)

        return graph, rule_map

    @staticmethod
    def _topological_sort(
        graph: Dict[str, Set[str]]
    ) -> List[str]:
        """
        Perform Kahn's algorithm to obtain a validation order.
        Raises CircularDependencyError if a cycle is detected.
        """
        # Compute in-degree for each node
        indegree: Dict[str, int] = {node: 0 for node in graph}
        for deps in graph.values():
            for node in deps:
                indegree[node] = indegree.get(node, 0) + 1

        # Queue of nodes with zero indegree
        zero_queue = collections.deque([node for node, deg in indegree.items() if deg == 0])
        order: List[str] = []

        while zero_queue:
            node = zero_queue.popleft()
            order.append(node)
            for dependent in graph.get(node, []):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    zero_queue.append(dependent)

        if len(order) != len(indegree):
            raise CircularDependencyError("Circular dependency detected among validation rules")

        return order

    @staticmethod
    def _apply_validators(
        order: List[str],
        rule_map: Dict[str, Dict[str, Any]],
        data: Dict[str, Any]
    ) -> None:
        """
        Executes validators in the given order.
        If a validator raises an exception, it is wrapped in ValidationError.
        """
        for field in order:
            rule = rule_map.get(field)
            if not rule:
                # No rule for this field; skip
                continue
            validator: Callable[[Any, Dict[str, Any]], Any] = rule["validator"]
            if not callable(validator):
                raise InvalidRuleError(f"Validator for field '{field}' is not callable")
            try:
                # Validator may modify the value; we store the result back.
                data[field] = validator(data.get(field), data)
            except Exception as exc:
                raise ValidationError(f"Validation failed for field '{field}': {exc}") from exc

    @classmethod
    def validate(
        cls,
        schema: Dict[str, Any],
        data: Dict[str, Any],
        rules: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Validate `data` against the provided `schema` using `rules`.

        Returns:
            (validated_data, validation_order)

        Raises:
            TypeError, ValueError, InvalidRuleError, CircularDependencyError,
            ValidationError
        """
        # Basic input validation
        cls._validate_inputs(schema, data, rules)

        # Build dependency graph and rule map
        graph, rule_map = cls._build_dependency_graph(rules)

        # Determine validation order
        order = cls._topological_sort(graph)

        # Apply validators in order
        cls._apply_validators(order, rule_map, data)

        # At this point, data is considered validated.  In real usage you might
        # also compare against `schema`, but that is beyond the current spec.
        return data, order