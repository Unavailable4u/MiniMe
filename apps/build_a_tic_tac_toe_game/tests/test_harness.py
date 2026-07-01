import sys
import os
import threading
import traceback
import time
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Tuple, Union


class TestHarness:
    """
    TestHarness simulates sandbox test scenarios.

    Parameters
    ----------
    config : dict
        Validated arguments for the harness. Supported keys:
        - ``default_timeout`` (float): seconds to wait for each test (default 5.0).
        - ``environment`` (dict): optional environment variables required for tests.
    scenarios : list
        List of test scenarios. Each item can be:
        - a callable ``func()`` returning a result.
        - a dict with keys:
            - ``name`` (str): identifier
            - ``function`` (callable): test function
            - ``expected`` (any, optional): expected result for validation
    """

    def __init__(self, config: Dict[str, Any], scenarios: List[Union[Callable, Dict[str, Any]]]):
        self._validate_config(config)
        self.config = config
        self.scenarios = self._normalize_scenarios(scenarios)
        # Use a process pool to provide isolation from the main interpreter.
        self.executor = ProcessPoolExecutor(max_workers=5)

    # --------------------------------------------------------------------- #
    # Validation helpers
    # --------------------------------------------------------------------- #
    @staticmethod
    def _validate_config(config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError("config must be a dict")
        timeout = config.get("default_timeout", 5.0)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("default_timeout must be a positive number")
        env = config.get("environment", {})
        if not isinstance(env, dict):
            raise ValueError("environment must be a dict if provided")

    @staticmethod
    def _normalize_scenarios(
        scenarios: List[Union[Callable, Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        if not isinstance(scenarios, list):
            raise ValueError("scenarios must be a list")
        normalized = []
        for idx, item in enumerate(scenarios):
            if callable(item):
                normalized.append(
                    {
                        "name": f"scenario_{idx}",
                        "function": item,
                        "expected": None,
                    }
                )
            elif isinstance(item, dict):
                if "function" not in item or not callable(item["function"]):
                    raise ValueError(
                        f"scenario dict at index {idx} must contain a callable 'function'"
                    )
                name = item.get("name", f"scenario_{idx}")
                expected = item.get("expected")
                normalized.append(
                    {
                        "name": name,
                        "function": item["function"],
                        "expected": expected,
                    }
                )
            else:
                raise ValueError(
                    f"scenario at index {idx} must be a callable or a dict with a callable 'function'"
                )
        return normalized

    # --------------------------------------------------------------------- #
    # Core execution
    # --------------------------------------------------------------------- #
    def run_all(self) -> Dict[str, Any]:
        """
        Execute all registered scenarios.

        Returns
        -------
        dict
            {
                "results": {scenario_name: result or None},
                "errors": {scenario_name: error_report or None}
            }
        """
        results: Dict[str, Any] = {}
        errors: Dict[str, Any] = {}

        for scen in self.scenarios:
            name = scen["name"]
            try:
                res, err = self._run_scenario(scen)
                results[name] = res
                errors[name] = err
            except Exception as exc:  # unexpected harness failure
                results[name] = None
                errors[name] = {
                    "type": "HarnessError",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }

        return {"results": results, "errors": errors}

    def _run_scenario(
        self, scenario: Dict[str, Any]
    ) -> Tuple[Any, Union[Dict[str, Any], None]]:
        """
        Run a single scenario with timeout handling and result validation.

        Returns
        -------
        tuple
            (result, error_report)

            * ``result`` is the value returned by the test function (or ``None`` on error).
            * ``error_report`` is ``None`` if the test succeeded, otherwise a dict
              describing the failure.
        """
        func: Callable = scenario["function"]
        expected = scenario["expected"]
        timeout = self.config.get("default_timeout", 5.0)

        # Perform environment validation *before* executing the test function.
        env_issues = self._check_environment()
        if env_issues:
            return (
                None,
                {"type": "EnvironmentError", "message": env_issues},
            )

        future = self.executor.submit(self._safe_execute, func)

        try:
            result = future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()
            return (
                None,
                {
                    "type": "Timeout",
                    "message": f"Test exceeded timeout of {timeout} seconds",
                },
            )
        except Exception as exc:
            # This captures exceptions raised while retrieving the result
            return (
                None,
                {
                    "type": "ExecutionError",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

        # Validate unexpected result
        if expected is not None and result != expected:
            return (
                result,
                {
                    "type": "UnexpectedResult",
                    "message": "Result did not match expected value",
                    "expected": expected,
                    "actual": result,
                },
            )

        # Success
        return (result, None)

    @staticmethod
    def _safe_execute(func: Callable) -> Any:
        """Execute the test function and capture any exception."""
        return func()

    def _check_environment(self) -> Union[str, None]:
        """Detect simple environment issues based on the supplied config."""
        required_env = self.config.get("environment", {})
        for key, expected_val in required_env.items():
            actual_val = os.getenv(key)
            if actual_val is None:
                return f"Missing required environment variable: {key}"
            if expected_val is not None and actual_val != str(expected_val):
                return f"Environment variable '{key}' has value '{actual_val}' but expected '{expected_val}'"
        return None

    # --------------------------------------------------------------------- #
    # Cleanup
    # --------------------------------------------------------------------- #
    def shutdown(self) -> None:
        """Shutdown internal process pool."""
        self.executor.shutdown(wait=True)


# --------------------------------------------------------------------- #
# Example usage (executed when run as script)
# --------------------------------------------------------------------- #
if __name__ == "__main__":
    # Example test functions
    def fast_success():
        return "ok"

    def slow_failure():
        time.sleep(10)  # will trigger timeout
        return "late"

    def env_dependent():
        return f"VAR={os.getenv('MY_VAR', 'missing')}"

    # Configuration with a short timeout and an environment requirement
    config = {
        "default_timeout": 2.0,
        "environment": {"MY_VAR": "expected_value"},
    }

    scenario_list = [
        {"name": "quick_success", "function": fast_success, "expected": "ok"},
        {"name": "timeout_test", "function": slow_failure, "expected": "late"},
        {"name": "env_test", "function": env_dependent, "expected": "VAR=expected_value"},
    ]

    harness = TestHarness(config, scenario_list)
    outcome = harness.run_all()
    harness.shutdown()

    print("=== Test Results ===")
    for name, result in outcome["results"].items():
        print(f"{name}: {result}")

    print("\n=== Error Reports ===")
    for name, err in outcome["errors"].items():
        if err:
            print(f"{name}: {err}")
        else:
            print(f"{name}: No errors")
