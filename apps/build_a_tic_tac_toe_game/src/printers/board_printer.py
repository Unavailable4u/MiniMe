class BoardPrinter:
    """
    BoardPrinter prints the current state of the game board.

    Methods
    -------
    print_board(game_board):
        Validates and returns a string representation of the board.
    """

    @staticmethod
    def _validate_board(game_board):
        """
        Validate the game board.

        Parameters
        ----------
        game_board : list of list
            The board to validate.

        Raises
        ------
        ValueError
            If the board is empty, not a list of lists, rows have inconsistent lengths,
            or if a cell contains an unsupported type.
        """
        if not isinstance(game_board, list):
            raise ValueError("Game board must be a list.")
        if len(game_board) == 0:
            raise ValueError("Game board cannot be empty.")

        row_length = None
        for idx, row in enumerate(game_board):
            if not isinstance(row, list):
                raise ValueError(f"Row {idx} is not a list.")
            if row_length is None:
                row_length = len(row)
                if row_length == 0:
                    raise ValueError("Rows in the game board cannot be empty.")
            elif len(row) != row_length:
                raise ValueError("All rows in the game board must have the same length.")

        # Ensure each cell is of an allowed type.
        # Allowed types: str, int, and None (as a placeholder).
        # Floats and other types are rejected to avoid formatting surprises.
        for i, row in enumerate(game_board):
            for j, cell in enumerate(row):
                if not isinstance(cell, (str, int)) and cell is not None:
                    raise ValueError(
                        f"Invalid cell type at ({i}, {j}): {type(cell).__name__}. "
                        "Allowed types are str, int, or None."
                    )

    @staticmethod
    def print_board(game_board):
        """
        Return a printable string representation of the game board.

        Parameters
        ----------
        game_board : list of list
            The current state of the game board.

        Returns
        -------
        printed_board : str
            A formatted string of the board.

        Raises
        ------
        ValueError
            If the input board is invalid.
        """
        BoardPrinter._validate_board(game_board)

        # Convert None placeholders to empty strings for display purposes.
        display_board = [
            ["" if cell is None else cell for cell in row]
            for row in game_board
        ]

        # Determine column widths for nice alignment
        col_widths = []
        for col in zip(*display_board):
            max_width = max(len(str(cell)) for cell in col)
            col_widths.append(max_width)

        rows_str = []
        for row in display_board:
            formatted_cells = [
                str(cell).rjust(col_widths[idx]) for idx, cell in enumerate(row)
            ]
            rows_str.append(" | ".join(formatted_cells))

        printed_board = "\n".join(rows_str)
        return printed_board


# Example usage (can be removed or commented out in production)
if __name__ == "__main__":
    sample_board = [
        ["X", "O", "X"],
        ["O", "X", "O"],
        [None, "X", None]
    ]
    try:
        output = BoardPrinter.print_board(sample_board)
        print(output)
    except ValueError as e:
        print(f"Error: {e}")
