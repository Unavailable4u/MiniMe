import os
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
print("Key loaded:", api_key[:10] + "..." if api_key else "NOT FOUND")

client = Groq(api_key=api_key)
response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[{"role": "user", "content": "Say hello in exactly 5 words."}]
)
print("Response:", response.choices[0].message.content)