import re
from typing import List


class ErrorFixer:
    """
    Generates fixes for identified syntax errors in a given Python source code.
    """

    def __init__(self):
        pass

    def fix_code(self, errors: List[str], code: str) -> str:
        """
        Apply simple heuristic fixes based on the provided error messages.

        Parameters
        ----------
        errors : List[str]
            List of syntax error messages (as strings).
        code : str
            The original source code that contains the errors.

        Returns
        -------
        str
            The corrected source code. If no fix could be applied, the original
            code is returned unchanged.
        """
        self._validate_inputs(errors, code)

        lines = code.splitlines()
        for err in errors:
            # Missing colon at end of statement (common for conditionals, loops, defs, classes)
            if "expected ':'" in err:
                lineno = self._extract_line_number(err)
                if lineno is not None and 0 < lineno <= len(lines):
                    lines[lineno - 1] = self._ensure_colon(lines[lineno - 1])

            # Invalid syntax caused by unmatched parentheses or brackets
            if "invalid syntax" in err and "EOF while scanning" not in err:
                lineno = self._extract_line_number(err)
                if lineno is not None and 0 < lineno <= len(lines):
                    lines[lineno - 1] = self._balance_parentheses(lines[lineno - 1])

            # Import errors – usually missing module or typo in import line
            if "ImportError" in err or "No module named" in err:
                lineno = self._extract_line_number(err)
                if lineno is not None and 0 < lineno <= len(lines):
                    lines[lineno - 1] = self._comment_out_line(lines[lineno - 1])

        return "\n".join(lines)

    @staticmethod
    def _validate_inputs(errors: List[str], code: str) -> None:
        if not isinstance(errors, list):
            raise TypeError("errors must be a list of strings")
        if not all(isinstance(e, str) for e in errors):
            raise TypeError("each error message must be a string")
        if not isinstance(code, str):
            raise TypeError("code must be a string")

    @staticmethod
    def _extract_line_number(error_msg: str) -> int | None:
        """
        Extract a line number from a typical Python SyntaxError message.
        Example message: "SyntaxError: invalid syntax (<string>, line 5)"
        """
        match = re.search(r'line (\d+)', error_msg)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _ensure_colon(line: str) -> str:
        stripped = line.rstrip()
        if not stripped.endswith(":"):
            return stripped + ":"
        return line

    @staticmethod
    def _balance_parentheses(line: str) -> str:
        """
        Very naive balance: if there are more opening '(' than closing ')',
        append missing closing parentheses.
        """
        open_paren = line.count('(')
        close_paren = line.count(')')
        if open_paren > close_paren:
            line += ')' * (open_paren - close_paren)
        return line

    @staticmethod
    def _comment_out_line(line: str) -> str:
        """
        Comment out a line, preserving original indentation.
        """
        leading_ws = re.match(r'\s*', line).group()
        return f"{leading_ws}# {line.lstrip()}"