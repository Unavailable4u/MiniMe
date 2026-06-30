import os
from groq import Groq
from google import genai
from google.genai import errors as genai_errors

_groq_client = None
_gemini_primary = None
_gemini_backup = None


def _get_groq():
    global _groq_client
    if _groq_client is None:
        key = os.getenv("GROQ_API_KEY")
        if key:
            _groq_client = Groq(api_key=key)
    return _groq_client


def _get_gemini(key_env_var):
    key = os.getenv(key_env_var)
    if not key:
        return None
    return genai.Client(api_key=key)


def _get_gemini_clients():
    global _gemini_primary, _gemini_backup
    if _gemini_primary is None:
        _gemini_primary = _get_gemini("GEMINI_API_KEY_1")
    if _gemini_backup is None:
        _gemini_backup = _get_gemini("GEMINI_API_KEY_2")
    return _gemini_primary, _gemini_backup


def _call_gemini(system_prompt, user_content, agent_name):
    primary, backup = _get_gemini_clients()
    contents = f"{system_prompt}\n\n{user_content}"

    def try_client(client, label):
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
        ).text.strip()

    if primary is not None:
        try:
            return try_client(primary, "Account 1")
        except genai_errors.ClientError as exc:
            if "RESOURCE_EXHAUSTED" not in str(exc) and "429" not in str(exc):
                raise
            print(f"  [{agent_name}] Gemini Account 1 exhausted, trying Account 2...")

    if backup is not None:
        return try_client(backup, "Account 2")

    raise RuntimeError(f"[{agent_name}] No working Gemini key available for fallback.")


def generate_text(system_prompt, user_content, agent_name="Agent",
                   groq_model="llama-3.3-70b-versatile"):
    """
    Primary: Groq (high daily request limit).
    Fallback: Gemini Account 1, then Account 2, only if Groq fails.
    Returns plain response text (already stripped).
    """
    groq_client = _get_groq()

    if groq_client is not None:
        try:
            response = groq_client.chat.completions.create(
                model=groq_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            print(f"  [{agent_name}] Groq failed ({exc.__class__.__name__}), "
                  f"falling back to Gemini...")

    return _call_gemini(system_prompt, user_content, agent_name)