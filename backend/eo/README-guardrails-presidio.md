# Output validation & PII redaction — setup

Two additions, wired in over Parts 1–6:

- **Guardrails AI** (`eo/output_guard.py`) — validates LLM-produced structured
  output (`code_writers.py` module maps, `task_runner.py`'s final answer,
  `eo/result_render.py`'s CO2 artifact payloads) before it's trusted downstream.
- **Presidio** (`eo/pii_scrub.py`) — redacts PII from ingested source text
  (`agents/pdf_ingestor.py`, `agents/voice_ingestor.py`) before it reaches
  `source_ingestor.write_ingested_source()`, i.e. before anything is embedded
  into Upstash Vector or written to the knowledge graph.

This file is Part 1 (scaffolding) — install steps only, nothing is wired into
the pipeline yet.

## One-time setup

1. **Guardrails Hub API key** — required even for validators that run
   100% locally; this is a known friction point, not a bug.
   - Go to https://hub.guardrailsai.com/keys, sign up (free), copy the key.
   - Locally: `pip install guardrails-ai` (already added to
     `requirements.txt`), then run `guardrails configure` and paste the key
     when prompted. This is one-time and stores the token in
     `~/.guardrailsai/`, not in `.env`.

2. **spaCy language model for Presidio** — `presidio-analyzer` needs this
   downloaded once; it isn't something `pip install -r requirements.txt`
   pulls in for you.
   ```bash
   python -m spacy download en_core_web_lg
   ```
   (~560MB — do this locally, not in CI/patch automation.)

3. Confirm `pip install -r requirements.txt` succeeds with the new deps
   (`guardrails-ai`, `presidio-analyzer`, `presidio-anonymizer`) before any
   downstream part (2–6) is applied.

## Smoke test

Both `eo/output_guard.py` and `eo/pii_scrub.py` have an `if __name__ ==
"__main__":` block you can run directly once the above is done:

```bash
python -m eo.output_guard
python -m eo.pii_scrub
```
