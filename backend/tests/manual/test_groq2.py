"""
tests/manual/test_groq2.py — moved from tests/test_groq2.py (B1 manual-tier
migration). Same as test_groq.py but exercises GROQ_API_KEY_2 (the
secondary/fallback key) specifically, so a bad or expired second key
doesn't go unnoticed just because the primary key still works.
"""
import os

import pytest
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

pytestmark = pytest.mark.manual


@pytest.mark.skipif(not os.getenv("GROQ_API_KEY_2"), reason="GROQ_API_KEY_2 not set")
def test_groq_secondary_key_chat_completion():
    client = Groq(api_key=os.getenv("GROQ_API_KEY_2"))
    # llama-3.3-70b-versatile decommissioned by Groq; migrated to
    # openai/gpt-oss-120b, one of the two models Groq's decommission
    # notice suggested in its place.
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": "say hello"}],
    )
    content = response.choices[0].message.content
    assert content, "expected non-empty response content"
    print("Response:", content)
