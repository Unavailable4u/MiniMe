# quick_groq2_test.py
import os
from dotenv import load_dotenv
from groq import Groq
load_dotenv()
client = Groq(api_key=os.getenv("GROQ_API_KEY_2"))
r = client.chat.completions.create(model="llama-3.3-70b-versatile",
    messages=[{"role":"user","content":"say hello"}])
print(r.choices[0].message.content)