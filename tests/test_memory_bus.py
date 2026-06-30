import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, read, KEYS

write(KEYS["original_idea"], "A simple todo list app")
result = read(KEYS["original_idea"])
print("Read back:", result)

write(KEYS["current_plan"], {"features": ["add task", "delete task"], "cycle_goal": "build add task"})
plan = read(KEYS["current_plan"])
print("Plan:", plan)