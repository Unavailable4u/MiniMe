import time
import os
from dotenv import load_dotenv
import psycopg

load_dotenv()
url = os.environ["DATABASE_URL"]
for i in range(10):
    t0 = time.monotonic()
    conn = psycopg.connect(url)
    dt = time.monotonic() - t0
    conn.close()
    print(f"connect #{i}: {dt*1000:.0f}ms")