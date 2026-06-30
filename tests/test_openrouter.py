import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENROUTER_API_KEY")
print("Key loaded:", api_key[:10] + "..." if api_key else "NOT FOUND")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
)

response = client.chat.completions.create(
    model="openrouter/free",
    messages=[{"role": "user", "content": "Say hello in exactly 5 words."}]
)
print("Response:", response.choices[0].message.content)