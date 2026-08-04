# tests/manual/cassettes/

vcrpy cassettes for `test_tool_calling.py` and `test_capability_coverage.py`.
Both hit a live Groq LLM; recording a cassette here lets them replay
deterministically afterward without a real `GROQ_API_KEY` or a live network
call, which is what makes them cheap enough to run routinely instead of only
by hand.

## How it works

Each test is wrapped in `my_vcr.use_cassette(...)` with `record_mode="once"`:

- **Cassette file doesn't exist yet** → a real `GROQ_API_KEY` is required.
  The test hits the live API, and vcrpy records every request/response pair
  into a new `.yaml` file here on success.
- **Cassette file exists** → no API key needed. The test replays the
  recorded responses; no network call is made at all.

`filter_headers=["authorization"]` strips the real API key out of what gets
written to disk, so cassette files are safe to commit.

## Recording or re-recording a cassette

```bash
# first recording, or to intentionally refresh stale responses:
rm -f tests/manual/cassettes/test_tool_calling_classification_coverage.yaml
GROQ_API_KEY=your_key_here pytest tests/manual/test_tool_calling.py -v -s

rm -f tests/manual/cassettes/test_capability_coverage_classification.yaml
GROQ_API_KEY=your_key_here pytest tests/manual/test_capability_coverage.py -v -s
```

## Why `allow_playback_repeats=True`

Both tests send the same message multiple times (`TOOL_TEST_REPEATS`, default
3) to check the model's classification is *consistent* across repeats. With
repeats allowed, every replay of an identical request reuses the same
recorded interaction rather than requiring one distinct recorded response per
repeat -- trades away replaying whatever run-to-run variability the live
model happened to show at recording time, in exchange for the cassette not
breaking if `TOOL_TEST_REPEATS` is changed later. If you specifically want to
exercise recorded inconsistency across repeats, re-record with the real API
and inspect the cassette's interactions directly.
