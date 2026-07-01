import os
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional, Any


class DatabaseConnectionError(Exception):
    """Raised when the database cannot be accessed."""


class TaskNotFoundError(Exception):
    """Raised when a task with the given ID does not exist."""


@dataclass
class Task:
    title: str
    description: str
    status: str = "pending"
    id: Optional[int] = None  # Assigned by the database


class TaskRepository:
    def __init__(self, db_path: str):
        # Basic validation of the path to avoid directory traversal or absolute paths.
        if not isinstance(db_path, str) or not db_path:
            raise ValueError("db_path must be a non-empty string.")
        # Disallow absolute paths.
        if os.path.isabs(db_path):
            raise ValueError("db_path must be a relative filename, not an absolute path.")
        # Disallow any directory components (e.g., "../" or "subdir/file.db").
        if os.path.dirname(db_path):
            raise ValueError("db_path must not contain directory components.")
        # Disallow parent directory references.
        if ".." in db_path.split(os.path.sep):
            raise ValueError("db_path must not contain parent directory references.")

        self.db_path = db_path
        try:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._ensure_table()
        except sqlite3.Error as e:
            raise DatabaseConnectionError(f"Failed to connect to database: {e}")

    def _ensure_table(self):
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL
        );
        """
        try:
            cur = self._conn.cursor()
            cur.execute(create_table_sql)
            self._conn.commit()
        except sqlite3.Error as e:
            raise DatabaseConnectionError(f"Failed to ensure tasks table: {e}")

    def _ensure_connection(self):
        if not getattr(self, "_conn", None):
            raise DatabaseConnectionError("Database connection is closed.")

    def add_task(self, task: Task) -> int:
        self._ensure_connection()
        if not isinstance(task, Task):
            raise ValueError("Input must be a Task instance.")
        if not task.title or not task.description:
            raise ValueError("Task title and description cannot be empty.")
        insert_sql = """
        INSERT INTO tasks (title, description, status)
        VALUES (:title, :description, :status);
        """
        try:
            cur = self._conn.cursor()
            cur.execute(insert_sql, {
                "title": task.title,
                "description": task.description,
                "status": task.status
            })
            self._conn.commit()
            task_id = cur.lastrowid
            # Update the Task instance with the generated ID for consistency.
            task.id = task_id
            return task_id
        except sqlite3.Error as e:
            raise DatabaseConnectionError(f"Failed to add task: {e}")

    def get_task(self, task_id: int) -> Task:
        self._ensure_connection()
        if not isinstance(task_id, int) or task_id <= 0:
            raise ValueError("task_id must be a positive integer.")
        select_sql = "SELECT id, title, description, status FROM tasks WHERE id = ?;"
        try:
            cur = self._conn.cursor()
            cur.execute(select_sql, (task_id,))
            row = cur.fetchone()
            if row is None:
                raise TaskNotFoundError(f"Task with ID {task_id} not found.")
            return Task(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                status=row["status"]
            )
        except sqlite3.Error as e:
            raise DatabaseConnectionError(f"Failed to retrieve task: {e}")

    def close(self):
        if getattr(self, "_conn", None):
            try:
                self._conn.close()
            except sqlite3.Error as e:
                raise DatabaseConnectionError(f"Failed to close database connection: {e}")
            finally:
                self._conn = None