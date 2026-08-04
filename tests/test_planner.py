import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS

write(KEYS["original_idea"], "A simple to-do list web app where users can add, complete, and delete tasks")
print("Idea written to memory.")