import sys
import os
import json
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS
from agents.test_writer import run

FAKE_SUBMITTED_CODE = {
    "math_utils": {
        "language": "python",
        "code": (
            "def add(a, b):\n"
            "    return a + b\n\n"
            "def is_even(n):\n"
            "    return n % 2 == 0\n"
        ),
    },
}


def main():
    print("Writing fake submitted_code to memory...")
    write(KEYS["submitted_code"], FAKE_SUBMITTED_CODE)

    print("Running Test Writer...")
    test_code = run()

    print("\n--- test_code ---")
    print(json.dumps(test_code, indent=2))

    if "math_utils" in test_code and "add" in test_code["math_utils"]:
        print("\nOK: generated tests reference the add() function.")
    else:
        print("\nWARNING: generated tests don't look like they reference math_utils — check output above.")


if __name__ == "__main__":
    main()
