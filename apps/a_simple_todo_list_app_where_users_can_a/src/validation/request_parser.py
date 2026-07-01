import json
from typing import Any, Dict, List, Optional


class RequestParserError(Exception):
    """Base exception for RequestParser errors."""
    pass


class EmptyInputError(RequestParserError):
    """Raised when the input data is empty."""
    pass


class InvalidInputFormatError(RequestParserError):
    """Raised when the input data cannot be parsed as JSON or violates size limits."""
    pass


class MissingRequiredFieldsError(RequestParserError):
    """Raised when required fields are missing from the parsed input."""
    def __init__(self, missing_fields: List[str]):
        self.missing_fields = missing_fields
        message = f"Missing required fields: {', '.join(missing_fields)}"
        super().__init__(message)


class RequestParser:
    """
    Handles and validates input requests.

    Parameters
    ----------
    required_fields : Optional[List[str]]
        List of field names that must be present in the parsed input.
    """

    # Simple safeguard: reject inputs larger than 1 MB (adjust as needed)
    MAX_INPUT_SIZE = 1_048_576  # bytes

    def __init__(self, required_fields: Optional[List[str]] = None):
        self.required_fields = required_fields or []

    def _to_str(self, raw_input_data: Any) -> str:
        """Convert raw input to a JSON string, handling bytes and ensuring size limits."""
        # Handle bytes / bytearray
        if isinstance(raw_input_data, (bytes, bytearray)):
            try:
                raw_input_data = raw_input_data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidInputFormatError("Input data is not valid UTF-8 encoded JSON.") from exc

        if not isinstance(raw_input_data, str):
            raise InvalidInputFormatError(
                f"Input data must be a JSON string, bytes, or a dict, got {type(raw_input_data).__name__}"
            )

        # Enforce size limit before attempting to parse
        if len(raw_input_data) > self.MAX_INPUT_SIZE:
            raise InvalidInputFormatError("Input data exceeds maximum allowed size.")

        return raw_input_data

    def parse(self, raw_input_data: Any) -> Dict[str, Any]:
        """
        Parses and validates the raw input data.

        Parameters
        ----------
        raw_input_data : Any
            The raw input to be parsed. Expected to be a JSON string, bytes, or a dict.

        Returns
        -------
        Dict[str, Any]
            The validated and parsed input data.

        Raises
        ------
        EmptyInputError
            If the input data is empty.
        InvalidInputFormatError
            If the input cannot be parsed as JSON or violates size limits.
        MissingRequiredFieldsError
            If required fields are not present in the parsed data.
        """
        # Validate empty input
        if raw_input_data is None or (isinstance(raw_input_data, str) and raw_input_data.strip() == ""):
            raise EmptyInputError("Input data is empty.")

        # Convert to dict if it's a JSON string or bytes
        if isinstance(raw_input_data, (str, bytes, bytearray)):
            json_str = self._to_str(raw_input_data)
            try:
                parsed_data = json.loads(json_str)
            except json.JSONDecodeError:
                # Provide a generic error without leaking parser details
                raise InvalidInputFormatError("Input data is not valid JSON.")
        elif isinstance(raw_input_data, dict):
            parsed_data = raw_input_data
        else:
            raise InvalidInputFormatError(
                f"Input data must be a JSON string, bytes, or a dict, got {type(raw_input_data).__name__}"
            )

        # Ensure the parsed data is a dict
        if not isinstance(parsed_data, dict):
            raise InvalidInputFormatError("Parsed JSON must be an object (dictionary).")

        # Check for required fields
        missing_fields = [field for field in self.required_fields if field not in parsed_data]
        if missing_fields:
            raise MissingRequiredFieldsError(missing_fields)

        return parsed_data


# Example usage (can be removed or commented out in production)
if __name__ == "__main__":
    # Define required fields for this example
    required = ["username", "email"]

    parser = RequestParser(required_fields=required)

    # Example valid input
    raw_json = '{"username": "alice", "email": "alice@example.com", "age": 30}'
    try:
        parsed = parser.parse(raw_json)
        print("Parsed data:", parsed)
    except RequestParserError as e:
        print("Error:", e)

    # Example with missing fields
    raw_json_missing = '{"username": "bob"}'
    try:
        parsed = parser.parse(raw_json_missing)
    except RequestParserError as e:
        print("Error:", e)
}