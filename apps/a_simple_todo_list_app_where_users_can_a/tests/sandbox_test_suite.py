import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Dict, Callable, List


class SandboxTestSuite:
    """
    Runs sandbox tests to validate UIUpdater functionality.

    Inputs:
        - parsed_input_data: dict containing test parameters.
        - UIUpdater_module: module or object that provides UI update methods.

    Output:
        - test_results: dict summarizing each test outcome.
    """

    def __init__(self, parsed_input_data: Dict[str, Any], UIUpdater_module: Any):
        self.parsed_input_data = parsed_input_data
        self.UIUpdater = UIUpdater_module
        self._validate_inputs()
        self._tests: List[Callable[[], Dict[str, Any]]] = [
            self._test_update_text,
            self._test_update_progress,
            self._test_invalid_call,
        ]

    def _validate_inputs(self) -> None:
        if not isinstance(self.parsed_input_data, dict):
            raise ValueError("parsed_input_data must be a dictionary.")
        if not hasattr(self.UIUpdater, "update_text") or not callable(getattr(self.UIUpdater, "update_text")):
            raise ValueError("UIUpdater_module must have a callable 'update_text' method.")
        if not hasattr(self.UIUpdater, "update_progress") or not callable(getattr(self.UIUpdater, "update_progress")):
            raise ValueError("UIUpdater_module must have a callable 'update_progress' method.")

    def run(self, timeout_per_test: float = 2.0) -> Dict[str, Any]:
        """
        Executes all sandbox tests with a per-test timeout.

        Returns:
            test_results dict with keys:
                - results: list of per-test dictionaries
                - summary: overall pass/fail status
        """
        results = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            for test in self._tests:
                future = executor.submit(test)
                try:
                    test_result = future.result(timeout=timeout_per_test)
                    results.append(test_result)
                except TimeoutError:
                    results.append({
                        "test": test.__name__,
                        "status": "timeout",
                        "message": f"Test exceeded timeout of {timeout_per_test}s."
                    })
                except Exception as exc:
                    results.append({
                        "test": test.__name__,
                        "status": "failure",
                        "message": f"Exception raised: {exc}"
                    })

        summary = "pass" if all(r.get("status") == "success" for r in results) else "fail"
        return {"results": results, "summary": summary}

    # ---------------------- Individual Tests ---------------------- #

    def _test_update_text(self) -> Dict[str, Any]:
        """
        Calls UIUpdater.update_text with expected parameters and verifies output.
        """
        test_name = "test_update_text"
        try:
            text = self.parsed_input_data.get("sample_text", "default")
            result = self.UIUpdater.update_text(text)
            if result != "ok":
                return {"test": test_name, "status": "failure",
                        "message": f"Unexpected output: {result}"}
            return {"test": test_name, "status": "success", "message": "Returned ok."}
        except Exception as e:
            return {"test": test_name, "status": "failure", "message": str(e)}

    def _test_update_progress(self) -> Dict[str, Any]:
        """
        Calls UIUpdater.update_progress with a numeric value and checks response.
        """
        test_name = "test_update_progress"
        try:
            progress = self.parsed_input_data.get("sample_progress", 50)
            if not isinstance(progress, (int, float)):
                raise ValueError("Progress must be numeric.")
            result = self.UIUpdater.update_progress(progress)
            if result != "ok":
                return {"test": test_name, "status": "failure",
                        "message": f"Unexpected output: {result}"}
            return {"test": test_name, "status": "success", "message": "Returned ok."}
        except Exception as e:
            return {"test": test_name, "status": "failure", "message": str(e)}

    def _test_invalid_call(self) -> Dict[str, Any]:
        """
        Attempts an invalid operation to ensure UIUpdater handles errors gracefully.
        """
        test_name = "test_invalid_call"
        try:
            # Assume UIUpdater has a method that raises on bad input
            result = self.UIUpdater.update_text(None)  # passing None should be invalid
            return {"test": test_name, "status": "failure",
                    "message": f"Expected exception, got result: {result}"}
        except Exception:
            # Expected path
            return {"test": test_name, "status": "success", "message": "Properly raised exception."}