import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, KEYS
from agents.security_scanner import run as run_security_scanner

# Deliberately includes a couple of real security smells (hardcoded
# secret, unsafe eval) so we can confirm the scanner actually finds
# something rather than just returning empty findings silently.
FAKE_FIXED_CODE = {
    "config_loader": {
        "language": "python",
        "code": (
            "API_KEY = 'sk-live-abc123hardcodedsecret'\n\n"
            "def load_config(path):\n"
            "    with open(path) as f:\n"
            "        return f.read()\n"
        ),
    },
    "template_renderer": {
        "language": "python",
        "code": (
            "def render(template_str, context):\n"
            "    # deliberately unsafe: evaluates arbitrary user input\n"
            "    return eval(template_str, context)\n"
        ),
    },
    "todo_storage": {
        "language": "python",
        "code": (
            "def add_todo(todos, item):\n"
            "    todos.append(item)\n"
            "    return todos\n"
        ),
    },
}


def main():
    print("Writing fake fixed_code to memory...")
    write(KEYS["fixed_code"], FAKE_FIXED_CODE)

    print("Running Scanner Pool (this calls Cloudflare across up to 5 parallel workers)...")
    # Fake session_id/tier so agent_start/agent_done actually fire.
    results = run_security_scanner(session_id="sess_harness_test", tier=3)

    print("\n--- security_scan_results ---")
    for module, result in results.items():
        print(f"\n{module}:")
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            continue
        findings = result.get("findings", [])
        if not findings:
            print("  no findings")
        for f in findings:
            print(f"  [{f.get('severity', '?')}] {f.get('description', '')}")

    flagged_modules = [m for m, r in results.items() if r.get("findings")]
    if "config_loader" in flagged_modules or "template_renderer" in flagged_modules:
        print("\nOK: scanner flagged at least one of the two deliberately-unsafe modules.")
    else:
        print("\nWARNING: scanner found nothing on code with a hardcoded secret and "
              "an eval() call. Check the prompt or the Cloudflare call.")


if __name__ == "__main__":
    main()