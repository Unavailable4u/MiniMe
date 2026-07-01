import ast
import importlib
import builtins
from typing import List, Set


class SyntaxAnalyzer:
    """
    Identifies syntax errors, missing imports, and undefined variables in the given
    RequestParser code string.
    """

    def analyze(self, code: str) -> List[str]:
        """
        Analyze the provided Python code and return a list of detected errors.

        Parameters
        ----------
        code : str
            The source code of RequestParser to analyze.

        Returns
        -------
        List[str]
            A list of error messages. Empty list means no detectable errors.
        """
        errors: List[str] = []

        # Basic input validation
        if not isinstance(code, str):
            raise TypeError("code must be a string")
        if code.strip() == "":
            errors.append("Error: Code is empty.")
            return errors

        # ----------- Syntax checking -----------
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            errors.append(
                f"SyntaxError: {e.msg} (line {e.lineno}, column {e.offset})"
            )
            return errors

        # ----------- Import checking -----------
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    if not self._can_import(module_name):
                        errors.append(
                            f"ImportError: cannot import module '{module_name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                # Handle relative imports by ignoring the level (could be more complex)
                module_name = node.module if node.module else ""
                if module_name and not self._can_import(module_name):
                    errors.append(
                        f"ImportError: cannot import module '{module_name}'"
                    )

        # ----------- Undefined variable checking -----------
        defined_names = self._collect_defined_names(tree)
        used_names = self._collect_used_names(tree)

        builtin_names = set(dir(builtins))
        undefined_names = used_names - defined_names - builtin_names

        for name in sorted(undefined_names):
            errors.append(f"NameError: undefined variable '{name}'")

        return errors

    def _can_import(self, module_name: str) -> bool:
        """
        Attempt to import a module by name to verify its availability.

        Parameters
        ----------
        module_name : str

        Returns
        ----------
        bool
            True if import succeeds, False otherwise.
        """
        try:
            importlib.import_module(module_name)
            return True
        except ImportError:
            return False

    def _collect_defined_names(self, tree: ast.AST) -> Set[str]:
        """
        Collect names that are defined within the code (assignments, function
        parameters, class names, imports, etc.).

        Parameters
        ----------
        tree : ast.AST

        Returns
        -------
        Set[str]
        """
        defined: Set[str] = set()

        class DefVisitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node: ast.FunctionDef):
                defined.add(node.name)
                for arg in node.args.args:
                    defined.add(arg.arg)
                if node.args.vararg:
                    defined.add(node.args.vararg.arg)
                if node.args.kwarg:
                    defined.add(node.args.kwarg.arg)
                self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
                self.visit_FunctionDef(node)  # reuse logic

            def visit_ClassDef(self, node: ast.ClassDef):
                defined.add(node.name)
                self.generic_visit(node)

            def visit_Import(self, node: ast.Import):
                for alias in node.names:
                    defined.add(alias.asname or alias.name.split('.')[0])

            def visit_ImportFrom(self, node: ast.ImportFrom):
                for alias in node.names:
                    defined.add(alias.asname or alias.name)

            def visit_Name(self, node: ast.Name):
                if isinstance(node.ctx, ast.Store):
                    defined.add(node.id)

            def visit_Assign(self, node: ast.Assign):
                for target in node.targets:
                    self._handle_target(target)
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign):
                self._handle_target(node.target)
                self.generic_visit(node)

            def visit_AugAssign(self, node: ast.AugAssign):
                self._handle_target(node.target)
                self.generic_visit(node)

            def _handle_target(self, target):
                if isinstance(target, ast.Name):
                    defined.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for elt in target.elts:
                        self._handle_target(elt)

        DefVisitor().visit(tree)
        return defined

    def _collect_used_names(self, tree: ast.AST) -> Set[str]:
        """
        Collect names that are read (loaded) in the code.

        Parameters
        ----------
        tree : ast.AST

        Returns
        -------
        Set[str]
        """
        used: Set[str] = set()

        class UseVisitor(ast.NodeVisitor):
            def visit_Name(self, node: ast.Name):
                if isinstance(node.ctx, ast.Load):
                    used.add(node.id)
                self.generic_visit(node)

        UseVisitor().visit(tree)
        return used


if __name__ == "__main__":
    # Simple demonstration; this block can be removed or adapted for unit testing.
    sample_code = """
import json
import missing_module

def foo(x):
    return x + y
"""
    analyzer = SyntaxAnalyzer()
    result = analyzer.analyze(sample_code)
    for err in result:
        print(err)