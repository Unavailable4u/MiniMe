"""
tests/integration/test_code_writers_pool.py — mocked rebuild of the old
tests/test_code_writers_pool.py (a throwaway concurrency-timing harness
that seeded real Redis, ran the real pool against real Cerebras keys,
and eyeballed interleaved print() lines in the terminal to "confirm"
parallelism).

Concurrency itself isn't meaningfully testable through a mock (there's no
real network latency to overlap), so this instead covers the shape/logic
contract: one result per module spec, module_specs auto-derived from
task_text when prompt_writer never ran, and code fences stripped from the
model's raw output.
"""
import agents.code_writers as code_writers  # noqa: F401  (ensures mock_llm patches this module)
from memory.bus import write, read, KEYS

MODULE_SPECS = {
    "modules": [
        {"name": "fizzbuzz", "description": "print fizzbuzz 1-30", "language": "python",
         "inputs": "none", "outputs": "printed sequence", "edge_cases": [], "constraints": []},
        {"name": "is_palindrome", "description": "check if a string is a palindrome", "language": "python",
         "inputs": "a string", "outputs": "boolean", "edge_cases": ["empty string"], "constraints": []},
    ]
}


def test_returns_one_result_per_module_spec(mock_llm):
    write(KEYS["module_specs"], MODULE_SPECS)
    mock_llm.set_response("def solve():\n    pass\n")

    results = code_writers.run()

    assert set(results.keys()) == {"fizzbuzz", "is_palindrome"}
    assert read(KEYS["submitted_code"]) == results


def test_strips_markdown_code_fences_from_the_raw_response(mock_llm):
    write(KEYS["module_specs"], MODULE_SPECS)
    mock_llm.set_response("```python\ndef solve():\n    pass\n```")

    results = code_writers.run()

    for code in results.values():
        assert not code.startswith("```")
        assert "def solve" in code


def test_derives_module_specs_from_task_text_when_none_written_yet(mock_llm):
    """Bug-fix regression guard: a hires-driven plan can staff
    "implementer" without "prompt_writer" ahead of it -- module_specs
    must be synthesized from task_text instead of crashing on
    specs["modules"] against None."""
    mock_llm.set_sequence([
        # first call: _derive_specs_from_task_text's own spec-writing call
        '{"modules": [{"name": "greeter", "description": "print hello", '
        '"inputs": "none", "outputs": "printed greeting", "edge_cases": []}]}',
        # second call: the actual code-writing call for the one derived module
        "def greet():\n    print('hello')\n",
    ])

    results = code_writers.run(task_text="write something that prints hello")

    assert set(results.keys()) == {"greeter"}
    assert read(KEYS["module_specs"])["modules"][0]["name"] == "greeter"


def test_derive_specs_falls_back_to_one_module_on_unparseable_json(mock_llm):
    mock_llm.set_response("not json at all")

    results = code_writers.run(task_text="build a thing")

    specs = read(KEYS["module_specs"])
    assert len(specs["modules"]) == 1
    assert specs["modules"][0]["name"] == "main"
    assert set(results.keys()) == {"main"}
