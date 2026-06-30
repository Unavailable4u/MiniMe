# tests/seed_test_data.py
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import write, KEYS

write(KEYS["original_idea"], "a simple todo list app")
write(KEYS["fixed_code"], {
    "auth_login": {"language": "python", "code": "def login(u,p):\n    return u == 'admin'\n"},
    "auth_signup": {"language": "python", "code": "def signup(u,p):\n    return True\n"},
    "todo_crud": {"language": "python", "code": "def add_todo(t):\n    return {'task': t}\n"},
})
print("Seeded original_idea and fixed_code.")