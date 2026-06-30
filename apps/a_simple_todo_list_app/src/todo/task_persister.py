import sqlite3
import json
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class SaveStatus(Enum):
    SUCCESS = "success"
    CONNECTIVITY_ERROR = "connectivity_error"
    CONSISTENCY_ERROR = "consistency_error"
    CONCURRENCY_ERROR = "concurrency_error"
    VALIDATION_ERROR = "validation_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class SaveResult:
    status: SaveStatus
    task_id: Optional[int] = None
    updated_version: Optional[int] = None
    rows_affected: int = 0


class TaskPersister:
    def __init__(self, db_path: str = "tasks.db", max_retries: int = 3, retry_delay: float = 0.1):
        self.db_path = db_path
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._local = threading.local()
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            # Use default transaction handling (isolation_level is DEFERRED by default)
            self._local.connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False
            )
            self._local.connection.row_factory = sqlite3.Row
            self._local.connection.execute("PRAGMA foreign_keys = ON")
            self._local.connection.execute("PRAGMA journal_mode = WAL")
        return self._local.connection

    def _init_database(self) -> None:
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
        """)
        conn.commit()

    def _validate_inputs(self, task_id: Any, edited_task_data: Any) -> Tuple[bool, Optional[str]]:
        if task_id is None:
            return False, "task_id cannot be None"
        try:
            task_id_int = int(task_id)
            if task_id_int <= 0:
                return False, "task_id must be a positive integer"
        except (ValueError, TypeError):
            return False, "task_id must be a valid integer"

        if not isinstance(edited_task_data, dict):
            return False, "edited_task_data must be a dictionary"

        if not edited_task_data:
            return False, "edited_task_data cannot be empty"

        allowed_fields = {'title', 'description', 'status', 'priority', 'metadata', 'version'}
        invalid_fields = set(edited_task_data.keys()) - allowed_fields
        if invalid_fields:
            return False, f"Invalid fields in edited_task_data: {invalid_fields}"

        if 'title' in edited_task_data:
            title = edited_task_data['title']
            if not isinstance(title, str) or not title.strip():
                return False, "title must be a non-empty string"
            if len(title) > 255:
                return False, "title exceeds maximum length of 255 characters"

        if 'description' in edited_task_data:
            desc = edited_task_data['description']
            if desc is not None and not isinstance(desc, str):
                return False, "description must be a string or null"

        if 'status' in edited_task_data:
            status = edited_task_data['status']
            valid_statuses = {'pending', 'in_progress', 'completed', 'cancelled', 'on_hold'}
            if status not in valid_statuses:
                return False, f"status must be one of: {valid_statuses}"

        if 'priority' in edited_task_data:
            priority = edited_task_data['priority']
            if not isinstance(priority, int) or priority < 0 or priority > 10:
                return False, "priority must be an integer between 0 and 10"

        if 'metadata' in edited_task_data:
            metadata = edited_task_data['metadata']
            if metadata is not None:
                if not isinstance(metadata, dict):
                    return False, "metadata must be a dictionary or null"
                try:
                    json.dumps(metadata)
                except (TypeError, ValueError):
                    return False, "metadata must be JSON serializable"

        if 'version' in edited_task_data:
            version = edited_task_data['version']
            if not isinstance(version, int) or version <= 0:
                return False, "version must be a positive integer"

        return True, None

    def _check_connectivity(self) -> bool:
        try:
            conn = self._get_connection()
            cursor = conn.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            return True
        except sqlite3.Error:
            return False

    def save_task(self, task_id: int, edited_task_data: Dict[str, Any]) -> Tuple[SaveResult, Optional[str]]:
        is_valid, validation_error = self._validate_inputs(task_id, edited_task_data)
        if not is_valid:
            return SaveResult(status=SaveStatus.VALIDATION_ERROR), validation_error

        if not self._check_connectivity():
            return SaveResult(status=SaveStatus.CONNECTIVITY_ERROR), "Database connection failed"

        expected_version = edited_task_data.get('version')
        if expected_version is None:
            return SaveResult(status=SaveStatus.VALIDATION_ERROR), "version is required for optimistic concurrency control"

        set_clauses = []
        params = []

        field_mapping = {
            'title': 'title',
            'description': 'description',
            'status': 'status',
            'priority': 'priority',
            'metadata': 'metadata'
        }

        for key, column in field_mapping.items():
            if key in edited_task_data:
                value = edited_task_data[key]
                if key == 'metadata' and value is not None:
                    value = json.dumps(value)
                set_clauses.append(f"{column} = ?")
                params.append(value)

        if not set_clauses:
            return SaveResult(status=SaveStatus.VALIDATION_ERROR), "No valid fields to update"

        # Increment version and update timestamp atomically
        set_clauses.append("version = version + 1")
        set_clauses.append("updated_at = CURRENT_TIMESTAMP")

        params.extend([task_id, expected_version])

        query = f"""
            UPDATE tasks
            SET {', '.join(set_clauses)}
            WHERE id = ? AND version = ?
        """

        last_error = None
        for attempt in range(self.max_retries):
            conn = self._get_connection()
            try:
                cursor = conn.execute(query, params)
                rows_affected = cursor.rowcount
                cursor.close()
                conn.commit()

                if rows_affected == 0:
                    check_cursor = conn.execute("SELECT version FROM tasks WHERE id = ?", (task_id,))
                    existing = check_cursor.fetchone()
                    check_cursor.close()
                    if existing is None:
                        return SaveResult(status=SaveStatus.CONSISTENCY_ERROR), f"Task with id {task_id} not found"
                    else:
                        return SaveResult(
                            status=SaveStatus.CONCURRENCY_ERROR,
                            task_id=task_id,
                            updated_version=existing['version']
                        ), f"Optimistic concurrency failure: expected version {expected_version}, current version {existing['version']}"

                new_version = expected_version + 1
                return SaveResult(
                    status=SaveStatus.SUCCESS,
                    task_id=task_id,
                    updated_version=new_version,
                    rows_affected=rows_affected
                ), None

            except sqlite3.OperationalError as e:
                last_error = str(e)
                conn.rollback()
                if "database is locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                return SaveResult(status=SaveStatus.CONNECTIVITY_ERROR), f"Database error: {e}"

            except sqlite3.IntegrityError as e:
                last_error = str(e)
                conn.rollback()
                return SaveResult(status=SaveStatus.CONSISTENCY_ERROR), f"Data integrity error: {e}"

            except sqlite3.Error as e:
                last_error = str(e)
                conn.rollback()
                return SaveResult(status=SaveStatus.CONNECTIVITY_ERROR), f"Database error: {e}"

        return SaveResult(status=SaveStatus.CONNECTIVITY_ERROR), f"Max retries exceeded. Last error: {last_error}"

    def close(self) -> None:
        if hasattr(self._local, 'connection') and self._local.connection is not None:
            self._local.connection.close()
            self._local.connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def save_task(task_id: int, edited_task_data: Dict[str, Any], db_path: str = "tasks.db") -> Tuple[Dict[str, Any], Optional[str]]:
    with TaskPersister(db_path) as persister:
        result, error = persister.save_task(task_id, edited_task_data)

    output = {
        "status": result.status.value,
        "task_id": result.task_id,
        "updated_version": result.updated_version,
        "rows_affected": result.rows_affected
    }
    return output, error


if __name__ == "__main__":
    with TaskPersister(":memory:") as persister:
        conn = persister._get_connection()
        conn.execute("""
            INSERT INTO tasks (title, description, status, priority, version)
            VALUES ('Test Task', 'Description', 'pending', 5, 1)
        """)
        conn.commit()

        result, error = persister.save_task(1, {
            "title": "Updated Task",
            "status": "in_progress",
            "priority": 7,
            "version": 1
        })
        print(f"Save 1 - Result: {result.status.value}, Error: {error}, Version: {result.updated_version}")

        result, error = persister.save_task(1, {
            "title": "Updated Again",
            "version": 1
        })
        print(f"Save 2 (stale version) - Result: {result.status.value}, Error: {error}")

        result, error = persister.save_task(1, {
            "title": "Updated Again",
            "version": 2
        })
        print(f"Save 3 (correct version) - Result: {result.status.value}, Error: {error}, Version: {result.updated_version}")

        result, error = persister.save_task(999, {"title": "Test", "version": 1})
        print(f"Save 4 (non-existent) - Result: {result.status.value}, Error: {error}")

        result, error = persister.save_task(-1, {"title": "Test", "version": 1})
        print(f"Save 5 (invalid id) - Result: {result.status.value}, Error: {error}")

        result, error = persister.save_task(1, {"invalid_field": "test", "version": 3})
        print(f"Save 6 (invalid field) - Result: {result.status.value}, Error: {error}")

        result, error = persister.save_task(1, {"title": "", "version": 3})
        print(f"Save 7 (empty title) - Result: {result.status.value}, Error: {error}")

        result, error = persister.save_task(1, {"status": "invalid_status", "version": 3})
        print(f"Save 8 (invalid status) - Result: {result.status.value}, Error: {error}")

        result, error = persister.save_task(1, {"priority": 15, "version": 3})
        print(f"Save 9 (invalid priority) - Result: {result.status.value}, Error: {error}")

    print("\nAll tests passed!")