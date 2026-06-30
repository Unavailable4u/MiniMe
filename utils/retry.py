import time


def call_with_retry(fn, *, agent_name="Agent", max_retries=4):
    """
    Calls fn() with exponential backoff retry (1s, 2s, 4s, 8s...).
    fn should be a zero-argument callable that makes the actual API call
    and returns the response. Re-raises the last exception if all
    retries are exhausted.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  [{agent_name}] API error ({exc.__class__.__name__}), "
                  f"retrying in {wait}s... (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
    raise last_exc