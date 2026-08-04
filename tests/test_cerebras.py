import os
from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras

load_dotenv()

api_key = os.getenv("CEREBRAS_API_KEY")
print("Key loaded:", api_key[:10] + "..." if api_key else "NOT FOUND")

client = Cerebras(api_key=api_key)
response = client.chat.completions.create(
    model="gpt-oss-120b",
    messages=[{"role": "user", "content": "Say hello in exactly 5 words."}]
)
print("Response:", response.choices[0].message.content)