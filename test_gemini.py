import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY_1")
print("Key loaded:", api_key[:10] + "..." if api_key else "NOT FOUND")

client = genai.Client(api_key=api_key)
response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Say hello in exactly 5 words."
)
print("Response:", response.text)