import os
from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox

load_dotenv()

api_key = os.getenv("E2B_API_KEY")
print("Key loaded:", api_key[:10] + "..." if api_key else "NOT FOUND")
os.environ["E2B_API_KEY"] = api_key

with Sandbox.create() as sbx:
    execution = sbx.run_code("print('hello from sandbox')")
    print("Output:", execution.logs.stdout)