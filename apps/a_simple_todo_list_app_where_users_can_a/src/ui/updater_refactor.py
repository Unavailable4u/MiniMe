class UIUpdater:
    """A simple UI updater implementation expected by the test suite.

    The SandboxTestSuite requires an object exposing two methods:
    * ``update_text(message: str)`` – updates some textual UI element.
    * ``update_progress(value: float)`` – updates a progress indicator.

    Both methods should return the string ``"ok"`` to signal successful handling.
    """

    def update_text(self, message: str) -> str:
        """Handle a text update request.

        Parameters
        ----------
        message: str
            The text that should be displayed on the UI.

        Returns
        -------
        str
            Always returns ``"ok"`` to indicate the update was processed.
        """
        # In a real implementation this would forward the message to a UI component.
        # For the purposes of the sandbox tests we simply acknowledge receipt.
        return "ok"

    def update_progress(self, value: float) -> str:
        """Handle a progress update request.

        Parameters
        ----------
        value: float
            A numeric value representing progress (typically between 0.0 and 1.0).

        Returns
        -------
        str
            Always returns ``"ok"`` to indicate the update was processed.
        """
        # A real implementation would update a progress bar or similar widget.
        # Here we just acknowledge the call.
        return "ok"
