"""
codeagent/adapters/ -- model adapters for the M2C server (patch S05).

One module per provider family, all speaking the provider-neutral interface
defined in codeagent/model_adapter.py:

    openai_compat.py   OpenAI-compatible chat completions with native tool use
                       (Groq, Mistral, Gemini's OpenAI layer, the Hugging Face
                       router, OpenRouter, or any other base URL). First adapter,
                       build plan decision D3. An Anthropic adapter is second and
                       a Gemini-native one third; they are separate patches.
    toolcalls.py       Tolerant tool-call parsing, ported from MiniLocker
                       (decision D12). Pure functions, no network.

Nothing is imported here on purpose. `import codeagent.adapters.toolcalls`
must stay cheap and must not drag in the OpenAI SDK, `utils.llm_client` (and
through it Redis, the database layer and every provider SDK) or any other
heavy module; import the module you need explicitly.

NAMESPACE NOTE. This patch deliberately does not create backend/codeagent/
__init__.py: patch S04 creates it in parallel. Until both are applied
`codeagent` is a plain namespace package, which is enough for
`from codeagent.adapters import toolcalls` and for the tests under
backend/tests/unit/test_codeagent_*.py to import it. Once S04's file exists
it becomes a regular package and nothing here changes.
"""

__all__: list = []
