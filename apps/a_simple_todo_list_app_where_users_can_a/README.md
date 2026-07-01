# Tic Tac Toe Game

A simple Tic Tac Toe game built in Python. Players take turns marking spaces on a 3x3 grid, aiming to get three of their marks in a row horizontally, vertically, or diagonally.

## Features

### ✅ Done
- **UIUpdater**: Handles rendering the game board and updates the UI after each move.
- **RequestParser**: Parses and validates player input (e.g., move coordinates).
- **TaskService**: Manages game tasks such as move processing and win condition checks.
- **TaskModel**: Defines the data structure for game tasks (e.g., moves, state updates).
- **GameStateUpdater**: Updates the game state after each move and checks for win/draw conditions.

### 🚧 In Progress
- **Add Task**: General task addition functionality (likely for future enhancements or modularity).

### 🔜 Planned
- Player vs. AI mode
- Score tracking
- Game history/replay

## How to Run

1. Ensure you have Python 3.8+ installed.
2. Clone this repository:
   ```bash
   git clone <repository-url>
   cd <repository-folder>
   ```
3. Install dependencies (if any):
   ```bash
   pip install -r requirements.txt
   ```
4. Run the game:
   ```bash
   python src/main.py
   ```

## Recent Changes

This cycle focused on core game infrastructure:
- Implemented `UIUpdater` for rendering the game board.
- Added `RequestParser` to handle and validate player input.
- Developed `TaskService` and `TaskModel` for move processing and state management.
- Introduced `GameStateUpdater` to track game progress and determine outcomes.

Validation utilities (`SyntaxAnalyzer`, `CodeVerifier`, `ErrorFixer`, `TaskValidator`, `Validator`) were also added to ensure robust input handling and error management.