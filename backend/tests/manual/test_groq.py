"""
tests/manual/test_groq.py — moved from tests/test_groq.py (B1 manual-tier
migration). Hits the real Groq API with GROQ_API_KEY; not run in CI (see
pytest.ini's `manual` marker and this package's conftest.py for why
fake_bus is disabled here).
"""
import os

import pytest
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

pytestmark = pytest.mark.manual


@pytest.mark.skipif(not os.getenv("GROQ_API_KEY"), reason="GROQ_API_KEY not set")
def test_groq_chat_completion():
    api_key = os.getenv("GROQ_API_KEY")
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": "Say hello in exactly 5 words."}],
    )
    content = response.choices[0].message.content
    assert content, "expected non-empty response content"
    print("Response:", content)
