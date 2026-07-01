import ast
import sys
import types
import multiprocessing

class VerificationError(Exception):
    """Custom exception for verification failures."""
    pass

class CodeVerifier:
    """
    Verifies the fixed RequestParser code for correctness.

    Methods
    -------
    verify(code_str: str) -> dict
        Returns a verification result containing success status and messages.
    """

    MAX_CODE_SIZE = 1024 * 1024  # 1 MB limit for performance safety

    @staticmethod
    def _syntax_check(code_str: str) -> None:
        """Parse the code with ast to ensure it is syntactically correct."""
        try:
            ast.parse(code_str)
        except SyntaxError as e:
            raise VerificationError(f"SyntaxError: {e.msg} (line {e.lineno})")

    @staticmethod
    def _structural_check(module: types.ModuleType) -> None:
        """
        Ensure that the module defines a class named 'RequestParser'
        with its own '__init__' and 'parse' methods (not merely inherited).
        Also ensure those members are callable.
        """
        if not hasattr(module, "RequestParser"):
            raise VerificationError("Missing class 'RequestParser'.")

        cls = getattr(module, "RequestParser")
        if not isinstance(cls, type):
            raise VerificationError("'RequestParser' is not a class.")

        required_methods = {"__init__", "parse"}
        # Check only methods defined directly on the class, not inherited ones
        defined_methods = set(cls.__dict__)
        missing = required_methods - defined_methods
        if missing:
            raise VerificationError(f"RequestParser missing methods: {', '.join(missing)}")

        # Verify that the required members are callable
        for method_name in required_methods:
            method = getattr(cls, method_name, None)
            if not callable(method):
                raise VerificationError(f"'{method_name}' of RequestParser is not callable.")

    @staticmethod
    def _execute_code_safely(code_str: str, timeout: float = 2.0) -> types.ModuleType:
        """
        Execute the code string in an isolated process with a timeout.
        The execution environment has a stripped-down ``__builtins__`` to
        prevent importing or accessing unsafe functionality.
        Returns the created module object.
        """
        def target(queue):
            try:
                # Provide an empty builtins dictionary – no imports, no file I/O, etc.
                safe_globals = {"__builtins__": {}}
                namespace: dict = {}
                exec(code_str, safe_globals, namespace)
                queue.put({"namespace": namespace, "exception": None})
            except Exception as exc:  # pragma: no cover – caught via queue
                queue.put({"namespace": None, "exception": exc})

        queue = multiprocessing.Queue()
        process = multiprocessing.Process(target=target, args=(queue,))
        process.start()
        process.join(timeout)
        if process.is_alive():
            process.terminate()
            process.join()
            raise VerificationError("Execution timeout exceeded.")

        result = queue.get()
        queue.close()
        if result["exception"] is not None:
            raise VerificationError(f"Runtime error during execution: {result['exception']}")

        module = types.ModuleType("_verifier_temp_module")
        module.__dict__.update(result["namespace"])
        return module

    def verify(self, code_str: str) -> dict:
        """
        Verify the provided RequestParser code.

        Parameters
        ----------
        code_str : str
            The source code of the fixed RequestParser.

        Returns
        -------
        dict
            {
                "success": bool,
                "messages": list of str
            }
        """
        messages = []
        # Input validation
        if not isinstance(code_str, str):
            return {"success": False, "messages": ["Input must be a string containing source code."]}

        if len(code_str) == 0:
            return {"success": False, "messages": ["Input code is empty."]}

        if len(code_str) > self.MAX_CODE_SIZE:
            return {"success": False, "messages": ["Input code exceeds size limit."]}

        # Step 1: Syntax check
        try:
            self._syntax_check(code_str)
            messages.append("Syntax check passed.")
        except VerificationError as ve:
            return {"success": False, "messages": [str(ve)]}

        # Step 2: Safe execution and structural validation
        try:
            module = self._execute_code_safely(code_str)
            messages.append("Code executed safely.")
            self._structural_check(module)
            messages.append("Structural check passed.")
        except VerificationError as ve:
            return {"success": False, "messages": [str(ve)]}
        except Exception as e:
            # Catch any unexpected errors
            return {"success": False, "messages": [f"Unexpected error: {e}"]}

        return {"success": True, "messages": messages}


__all__ = ["CodeVerifier", "VerificationError"]