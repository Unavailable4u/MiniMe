"""
tests/manual/test_cerebras.py — moved from tests/test_cerebras.py (B1
manual-tier migration). Hits the real Cerebras API with CEREBRAS_API_KEY;
not run in CI.
"""
import os

import pytest
from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras

load_dotenv()

pytestmark = pytest.mark.manual


@pytest.mark.skipif(not os.getenv("CEREBRAS_API_KEY"), reason="CEREBRAS_API_KEY not set")
def test_cerebras_chat_completion():
    api_key = os.getenv("CEREBRAS_API_KEY")
    client = Cerebras(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-oss-120b",
        messages=[{"role": "user", "content": "Say hello in exactly 5 words."}],
    )
    content = response.choices[0].message.content
    assert content, "expected non-empty response content"
    print("Response:", content)
