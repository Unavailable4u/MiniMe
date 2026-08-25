"""
tests/unit/test_agent_source_manager_exec.py — Patch 7f-4a-2.

Covers the "execution pipeline" half of agents/source_manager.py (lines
350-702): _run_sequential_pass, _run_chunk_worker, _run_parallel_passes,
_run_mode_a_topic_extraction, process_upload.

Unlike 7f-4a-1's prep pipeline (pure, no I/O), every function here
either makes an LLM call, spins up threads, or writes through to
another module (Secondary Data, notify, Backlink Detector, worker
selection). So instead of re-verifying parsing/id-resolution logic
already locked down in 7f-4a-1, these tests isolate each function's
OWN wiring: what it calls, with what arguments, how it degrades on
failure, and how it assembles its return value from its collaborators'
(mocked) outputs. _parse_mode_a_topics / _topics_to_ops are monkeypatched
out wherever a test isn't specifically about them, so a regression in
that pure-parsing logic can't masquerade as an execution-pipeline
failure here (and vice versa).
"""
import threading

import pytest

import eo.notify as notify_module
from agents import generic_worker, overlapping_checker, source_manager

# ---------------------------------------------------------------------------
# 1. _run_sequential_pass
# ---------------------------------------------------------------------------

class TestRunSequentialPass:
    def test_ensures_role_registered_before_call(self, monkeypatch):
        called = {"count": 0}
        monkeypatch.setattr(source_manager, "_ensure_role_registered",
                             lambda: called.__setitem__("count", called["count"] + 1))
        monkeypatch.setattr(generic_worker, "run", lambda **kw: {"text": ""})
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", lambda raw, ids: [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], []))

        source_manager._run_sequential_pass("some context", {"n1": {}})
        assert called["count"] == 1

    def test_builds_task_text_with_context_appended(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_ensure_role_registered", lambda: None)
        captured = {}

        def fake_run(**kw):
            captured.update(kw)
            return {"text": ""}

        monkeypatch.setattr(generic_worker, "run", fake_run)
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", lambda raw, ids: [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], []))

        source_manager._run_sequential_pass("--- [n1] Intro ---\nhello", {"n1": {}})
        assert captured["task_text"].endswith("--- [n1] Intro ---\nhello")
        assert "Extract this newly-ingested source's topic tree" in captured["task_text"]

    def test_forwards_role_and_call_shape_to_run_role(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_ensure_role_registered", lambda: None)
        captured = {}

        def fake_run(**kw):
            captured.update(kw)
            return {"text": ""}

        monkeypatch.setattr(generic_worker, "run", fake_run)
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", lambda raw, ids: [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], []))

        source_manager._run_sequential_pass("ctx", {"n1": {}}, session_id="sess-1")
        assert captured["role"] == "source_manager"
        assert captured["input_keys"] == []
        assert captured["session_id"] == "sess-1"
        assert captured["include_conversation_context"] is False
        assert captured["domain"] == "notes"

    def test_parses_result_text_and_forwards_to_topics_to_ops(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_ensure_role_registered", lambda: None)
        monkeypatch.setattr(generic_worker, "run", lambda **kw: {"text": "raw model output"})

        parse_calls = []

        def fake_parse(raw, valid_ids):
            parse_calls.append((raw, valid_ids))
            return ["fake topic"]

        topics_calls = []

        def fake_topics_to_ops(topics):
            topics_calls.append(topics)
            return (["op1"], ["id1"])

        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", fake_parse)
        monkeypatch.setattr(source_manager, "_topics_to_ops", fake_topics_to_ops)

        id_map = {"n1": {}, "n2": {}}
        result = source_manager._run_sequential_pass("ctx", id_map)

        assert parse_calls == [("raw model output", {"n1", "n2"})]
        assert topics_calls == [["fake topic"]]
        assert result == (["op1"], ["id1"])

    def test_missing_text_key_passes_empty_string_to_parser(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_ensure_role_registered", lambda: None)
        monkeypatch.setattr(generic_worker, "run", lambda **kw: {})  # no "text" key at all
        parse_calls = []
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics",
                             lambda raw, ids: parse_calls.append(raw) or [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], []))

        source_manager._run_sequential_pass("ctx", {})
        assert parse_calls == [""]


# ---------------------------------------------------------------------------
# 2. _run_chunk_worker
# ---------------------------------------------------------------------------

class TestRunChunkWorker:
    def test_emits_agent_start_with_worker_labeled_event(self, monkeypatch, mock_llm):
        mock_llm.set_response("raw")
        events = []
        monkeypatch.setattr(source_manager, "emit_event",
                             lambda event_type, **kw: events.append((event_type, kw)))
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", lambda raw, ids: [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], []))

        source_manager._run_chunk_worker("ctx", {"n1": {}}, "OPENROUTER_API_KEY_1", 3,
                                          session_id="sess-1")
        start_events = [e for e in events if e[0] == "agent_start"]
        assert len(start_events) == 1
        assert start_events[0][1]["agent"] == "source_manager_chunk_3"
        assert "Chunk 3" in start_events[0][1]["payload"]["label"]

    def test_calls_generate_text_with_chain_step_for_key_env(self, monkeypatch, mock_llm):
        mock_llm.set_response("raw output")
        monkeypatch.setattr(source_manager, "emit_event", lambda *a, **k: None)
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", lambda raw, ids: [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], []))

        fake_step = {"provider": "openrouter", "model": "m", "key_env": "OPENROUTER_API_KEY_2"}
        monkeypatch.setattr(generic_worker, "_chain_step_for", lambda key: fake_step)

        source_manager._run_chunk_worker("the context", {"n1": {}}, "OPENROUTER_API_KEY_2", 1,
                                          session_id="sess-9", domain="notes")

        args, kwargs = mock_llm.mock.call_args
        assert args[0] == source_manager.SOURCE_MANAGER_TOPIC_BRIEF
        assert args[1] == "the context"
        assert args[2] == [fake_step]
        assert kwargs["agent_name"] == "source_manager_chunk_1"
        assert kwargs["session_id"] == "sess-9"
        assert kwargs["domain"] == "notes"

    def test_parses_response_and_returns_ops_and_topic_ids(self, monkeypatch, mock_llm):
        mock_llm.set_response("model raw text")
        monkeypatch.setattr(source_manager, "emit_event", lambda *a, **k: None)

        parse_calls = []

        def fake_parse(raw, valid_ids):
            parse_calls.append((raw, valid_ids))
            return ["t"]

        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", fake_parse)
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: (["op"], ["id1"]))
        monkeypatch.setattr(generic_worker, "_chain_step_for",
                             lambda key: {"provider": "x", "model": "m", "key_env": key})

        ops, topic_ids = source_manager._run_chunk_worker(
            "ctx", {"n1": {}, "n2": {}}, "KEY_1", 1,
        )
        assert parse_calls == [("model raw text", {"n1", "n2"})]
        assert ops == ["op"]
        assert topic_ids == ["id1"]

    def test_emits_agent_done_with_topic_count_and_duration(self, monkeypatch, mock_llm):
        mock_llm.set_response("raw")
        events = []
        monkeypatch.setattr(source_manager, "emit_event",
                             lambda event_type, **kw: events.append((event_type, kw)))
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics", lambda raw, ids: [])
        monkeypatch.setattr(source_manager, "_topics_to_ops", lambda topics: ([], ["a", "b"]))
        monkeypatch.setattr(generic_worker, "_chain_step_for",
                             lambda key: {"provider": "x", "model": "m", "key_env": key})

        source_manager._run_chunk_worker("ctx", {}, "KEY_1", 2)
        done_events = [e for e in events if e[0] == "agent_done"]
        assert len(done_events) == 1
        assert done_events[0][1]["agent"] == "source_manager_chunk_2"
        assert done_events[0][1]["payload"]["summary"] == "2 topic(s)"
        assert isinstance(done_events[0][1]["payload"]["duration_ms"], int)

    def test_runtime_error_from_generate_text_returns_empty_and_still_emits_done(
        self, monkeypatch, mock_llm,
    ):
        mock_llm.raise_on_call(RuntimeError("account exhausted"))
        events = []
        monkeypatch.setattr(source_manager, "emit_event",
                             lambda event_type, **kw: events.append((event_type, kw)))
        parse_called = {"count": 0}
        monkeypatch.setattr(source_manager, "_parse_mode_a_topics",
                             lambda raw, ids: parse_called.__setitem__("count", 1) or [])
        monkeypatch.setattr(generic_worker, "_chain_step_for",
                             lambda key: {"provider": "x", "model": "m", "key_env": key})

        ops, topic_ids = source_manager._run_chunk_worker("ctx", {"n1": {}}, "KEY_1", 1)

        assert ops == []
        assert topic_ids == []
        # generate_text's own failure short-circuits before parsing is ever reached
        assert parse_called["count"] == 0
        assert any(e[0] == "agent_done" for e in events)
        done_payload = [e for e in events if e[0] == "agent_done"][0][1]["payload"]
        assert done_payload["summary"] == "0 topic(s)"


# ---------------------------------------------------------------------------
# 3. _run_parallel_passes
# ---------------------------------------------------------------------------

class TestRunParallelPasses:
    def test_worker_count_capped_at_max_parallel_workers(self, monkeypatch):
        select_calls = []

        def fake_select(role_tag, worker_count, key_override, **kw):
            select_calls.append((role_tag, worker_count, key_override))
            return ["KEY_1"]

        monkeypatch.setattr(source_manager, "_select_workers", fake_select)
        monkeypatch.setattr(source_manager, "_run_chunk_worker",
                             lambda *a, **k: ([], []))

        chunks = [[("n1", {"content": "c", "heading": "h"})] for _ in range(20)]
        source_manager._run_parallel_passes(chunks, "Title")

        assert select_calls[0][1] == source_manager.MODE_A_MAX_PARALLEL_WORKERS

    def test_calls_select_workers_with_source_manager_tag_and_overrides(self, monkeypatch):
        select_calls = []

        def fake_select(role_tag, worker_count, key_override, **kw):
            select_calls.append((role_tag, worker_count, key_override, kw))
            return ["KEY_1"]

        monkeypatch.setattr(source_manager, "_select_workers", fake_select)
        monkeypatch.setattr(source_manager, "_run_chunk_worker",
                             lambda *a, **k: ([], []))

        chunks = [[("n1", {"content": "c", "heading": "h"})]]
        source_manager._run_parallel_passes(chunks, "Title", session_id="sess-5",
                                             key_override="EXPLICIT_KEY")

        role_tag, worker_count, key_override, kw = select_calls[0]
        assert role_tag == "source_manager"
        assert worker_count == 1
        assert key_override == "EXPLICIT_KEY"
        assert kw["session_id"] == "sess-5"
        assert kw["agent_name"] == "source_manager"

    def test_round_robins_chunks_across_fewer_workers_via_modulo(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_select_workers",
                             lambda *a, **k: ["KEY_A", "KEY_B"])

        seen = []
        lock = threading.Lock()

        def fake_chunk_worker(context, id_map, key_env, worker_id, **kw):
            with lock:
                seen.append((key_env, worker_id))
            return ([], [])

        monkeypatch.setattr(source_manager, "_run_chunk_worker", fake_chunk_worker)

        chunks = [
            [("n1", {"content": "c1", "heading": "h1"})],
            [("n2", {"content": "c2", "heading": "h2"})],
            [("n3", {"content": "c3", "heading": "h3"})],
        ]
        source_manager._run_parallel_passes(chunks, "Title")

        seen.sort(key=lambda pair: pair[1])
        assert seen == [("KEY_A", 1), ("KEY_A", 1), ("KEY_B", 2)]

    def test_skips_chunks_with_empty_context(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_select_workers",
                             lambda *a, **k: ["KEY_A"])
        calls = []
        monkeypatch.setattr(source_manager, "_run_chunk_worker",
                             lambda *a, **k: calls.append(a) or ([], []))

        chunks = [[], [("n1", {"content": "real content", "heading": "h"})]]
        source_manager._run_parallel_passes(chunks, "Title")

        # only the second (non-empty) chunk actually reaches _run_chunk_worker
        assert len(calls) == 1

    def test_aggregates_ops_and_topic_ids_from_all_chunks(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_select_workers",
                             lambda *a, **k: ["KEY_A", "KEY_B"])

        results = {
            "n1": ([{"op": "add", "path": "/topics/a"}], ["a"]),
            "n2": ([{"op": "add", "path": "/topics/b"}], ["b"]),
        }

        def fake_chunk_worker(context, id_map, key_env, worker_id, **kw):
            node_id = next(iter(id_map))
            return results[node_id]

        monkeypatch.setattr(source_manager, "_run_chunk_worker", fake_chunk_worker)

        chunks = [
            [("n1", {"content": "c1", "heading": "h1"})],
            [("n2", {"content": "c2", "heading": "h2"})],
        ]
        ops, topic_ids = source_manager._run_parallel_passes(chunks, "Title")

        assert sorted(topic_ids) == ["a", "b"]
        assert len(ops) == 2

    def test_propagates_runtime_error_from_select_workers(self, monkeypatch):
        def fake_select(*a, **k):
            raise RuntimeError("worker_pool: no accounts tagged 'source_manager'")

        monkeypatch.setattr(source_manager, "_select_workers", fake_select)
        chunks = [[("n1", {"content": "c", "heading": "h"})]]
        with pytest.raises(RuntimeError):
            source_manager._run_parallel_passes(chunks, "Title")


# ---------------------------------------------------------------------------
# 4. _run_mode_a_topic_extraction
# ---------------------------------------------------------------------------

def _artifact_with_sections(n, title="Doc"):
    return {
        "title": title,
        "sections": [{"heading": f"H{i}", "content": f"content {i}"} for i in range(n)],
    }


class TestRunModeATopicExtraction:
    def test_no_usable_pairs_returns_empty_without_calling_anything(self, monkeypatch):
        called = {"count": 0}
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: called.__setitem__("count", 1))
        artifact = {"sections": [{"heading": "H", "content": "   "}]}  # blank content -> filtered
        result = source_manager._run_mode_a_topic_extraction(artifact, ["n1"], "ws-1")
        assert result == ([], {})
        assert called["count"] == 0

    def test_uses_sequential_pass_at_or_under_chunk_size(self, monkeypatch):
        seq_calls = []
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: seq_calls.append(a) or ([], []))
        par_calls = []
        monkeypatch.setattr(source_manager, "_run_parallel_passes",
                             lambda *a, **k: par_calls.append(a) or ([], []))

        n = source_manager.MODE_A_CHUNK_SIZE  # exactly at the boundary
        artifact = _artifact_with_sections(n)
        node_ids = [f"node-{i}" for i in range(n)]
        source_manager._run_mode_a_topic_extraction(artifact, node_ids, "ws-1")

        assert len(seq_calls) == 1
        assert len(par_calls) == 0

    def test_uses_parallel_passes_above_chunk_size(self, monkeypatch):
        seq_calls = []
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: seq_calls.append(a) or ([], []))
        par_calls = []
        monkeypatch.setattr(source_manager, "_run_parallel_passes",
                             lambda *a, **k: par_calls.append(a) or ([], []))

        n = source_manager.MODE_A_CHUNK_SIZE + 1
        artifact = _artifact_with_sections(n)
        node_ids = [f"node-{i}" for i in range(n)]
        source_manager._run_mode_a_topic_extraction(artifact, node_ids, "ws-1")

        assert len(seq_calls) == 0
        assert len(par_calls) == 1
        # split into two contiguous chunks (8 + 1) per _chunk_pairs
        chunks_arg = par_calls[0][0]
        assert len(chunks_arg) == 2
        assert len(chunks_arg[0]) == source_manager.MODE_A_CHUNK_SIZE
        assert len(chunks_arg[1]) == 1

    def test_forwards_key_override_only_on_parallel_path(self, monkeypatch):
        par_calls = []
        monkeypatch.setattr(source_manager, "_run_parallel_passes",
                             lambda *a, **k: par_calls.append(k) or ([], []))
        n = source_manager.MODE_A_CHUNK_SIZE + 1
        artifact = _artifact_with_sections(n)
        node_ids = [f"node-{i}" for i in range(n)]
        source_manager._run_mode_a_topic_extraction(
            artifact, node_ids, "ws-1", session_id="sess-1", key_override="OVERRIDE_KEY",
        )
        assert par_calls[0]["key_override"] == "OVERRIDE_KEY"
        assert par_calls[0]["session_id"] == "sess-1"

    def test_exception_during_pass_is_caught_and_degrades_to_empty(self, monkeypatch):
        def raise_exc(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(source_manager, "_run_sequential_pass", raise_exc)
        artifact = _artifact_with_sections(2)
        result = source_manager._run_mode_a_topic_extraction(artifact, ["n1", "n2"], "ws-1")
        assert result == ([], {})

    def test_empty_ops_short_circuits_before_overlap_check(self, monkeypatch):
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: ([], []))
        check_called = {"count": 0}
        monkeypatch.setattr(overlapping_checker, "check_batch",
                             lambda *a, **k: check_called.__setitem__("count", 1) or {})

        artifact = _artifact_with_sections(2)
        result = source_manager._run_mode_a_topic_extraction(artifact, ["n1", "n2"], "ws-1")
        assert result == ([], {})
        assert check_called["count"] == 0

    def test_apply_patch_failure_is_caught_and_degrades_to_empty(self, monkeypatch):
        ops = [{"op": "add", "path": "/topics/t1",
                "value": {"name": "T", "summary": "s", "source_section_ids": []}}]
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: (ops, ["t1"]))
        monkeypatch.setattr(overlapping_checker, "check_batch",
                             lambda *a, **k: {"t1": {"tag": "new"}})

        def fake_apply_patch(workspace_id, ops):
            raise ValueError("bad op")

        monkeypatch.setattr(source_manager, "apply_patch", fake_apply_patch)

        artifact = _artifact_with_sections(2)
        result = source_manager._run_mode_a_topic_extraction(artifact, ["n1", "n2"], "ws-1")
        assert result == ([], {})

    def test_duplicate_tag_folds_into_instances_append_op(self, monkeypatch):
        ops = [{"op": "add", "path": "/topics/t1",
                "value": {"name": "T", "summary": "a summary", "source_section_ids": ["n1"],
                          "content_hint": "conceptual", "parent": None}}]
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: (ops, ["t1"]))
        monkeypatch.setattr(overlapping_checker, "check_batch",
                             lambda *a, **k: {"t1": {"tag": "duplicate", "target_topic_id": "existing-42"}})

        applied = {}
        monkeypatch.setattr(source_manager, "apply_patch",
                             lambda ws_id, patch_ops: applied.setdefault("ops", patch_ops))

        artifact = _artifact_with_sections(2)
        source_manager._run_mode_a_topic_extraction(artifact, ["n1", "n2"], "ws-1")

        assert len(applied["ops"]) == 1
        folded = applied["ops"][0]
        assert folded["path"] == "/topics/existing-42/instances/-"
        assert folded["value"]["verbatim"] == "a summary"
        assert folded["value"]["source_section_ids"] == ["n1"]
        assert folded["value"]["confidence"] == 1.0

    def test_new_and_merge_tags_keep_their_own_add_op(self, monkeypatch):
        ops = [
            {"op": "add", "path": "/topics/t1",
             "value": {"name": "New", "summary": "s1", "source_section_ids": [],
                       "content_hint": "conceptual", "parent": None}},
            {"op": "add", "path": "/topics/t2",
             "value": {"name": "Merge", "summary": "s2", "source_section_ids": [],
                       "content_hint": "conceptual", "parent": None}},
        ]
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: (ops, ["t1", "t2"]))
        monkeypatch.setattr(overlapping_checker, "check_batch",
                             lambda *a, **k: {"t2": {"tag": "merge", "target_topic_id": "other-9"}})

        applied = {}
        monkeypatch.setattr(source_manager, "apply_patch",
                             lambda ws_id, patch_ops: applied.setdefault("ops", patch_ops))

        artifact = _artifact_with_sections(2)
        source_manager._run_mode_a_topic_extraction(artifact, ["n1", "n2"], "ws-1")

        paths = {op["path"] for op in applied["ops"]}
        assert paths == {"/topics/t1", "/topics/t2"}

    def test_returns_original_topic_ids_even_after_duplicate_folding(self, monkeypatch):
        # the function's return value is the topic_ids from the pass itself,
        # not re-derived from the post-filter ops -- a "duplicate"-tagged id
        # is still included even though it was folded away and never wrote
        # its own /topics/<id> node.
        ops = [{"op": "add", "path": "/topics/t1",
                "value": {"name": "T", "summary": "s", "source_section_ids": [],
                          "content_hint": "conceptual", "parent": None}}]
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: (ops, ["t1"]))
        monkeypatch.setattr(overlapping_checker, "check_batch",
                             lambda *a, **k: {"t1": {"tag": "duplicate", "target_topic_id": "existing-42"}})
        monkeypatch.setattr(source_manager, "apply_patch", lambda ws_id, patch_ops: None)

        artifact = _artifact_with_sections(2)
        topic_ids, overlap_tags = source_manager._run_mode_a_topic_extraction(
            artifact, ["n1", "n2"], "ws-1",
        )
        assert topic_ids == ["t1"]
        assert overlap_tags == {"t1": {"tag": "duplicate", "target_topic_id": "existing-42"}}

    def test_notify_fires_once_per_pending_event_and_swallows_errors(self, monkeypatch):
        ops = [{"op": "add", "path": "/topics/t1",
                "value": {"name": "T", "summary": "s", "source_section_ids": [],
                          "content_hint": "conceptual", "parent": None}}]
        monkeypatch.setattr(source_manager, "_run_sequential_pass",
                             lambda *a, **k: (ops, ["t1"]))
        monkeypatch.setattr(overlapping_checker, "check_batch", lambda *a, **k: {})
        monkeypatch.setattr(source_manager, "apply_patch", lambda ws_id, patch_ops: None)

        def raising_notify(session_id, kind, payload=None):
            raise Exception("relay down")

        monkeypatch.setattr(notify_module, "notify", raising_notify)

        artifact = _artifact_with_sections(2)
        # must not raise even though notify() blows up internally
        result = source_manager._run_mode_a_topic_extraction(
            artifact, ["n1", "n2"], "ws-1", session_id="sess-1",
        )
        assert result == (["t1"], {})


# ---------------------------------------------------------------------------
# 5. process_upload
# ---------------------------------------------------------------------------

class TestProcessUpload:
    def _stub_pipeline(self, monkeypatch, topic_ids=None, overlap_tags=None,
                        artifact=None, node_ids=None):
        artifact = artifact if artifact is not None else {"title": "My Doc", "sections": []}
        node_ids = node_ids if node_ids is not None else ["node-1"]
        topic_ids = topic_ids if topic_ids is not None else []
        overlap_tags = overlap_tags if overlap_tags is not None else {}

        dispatch_calls = []
        monkeypatch.setattr(source_manager, "_INGEST_DISPATCH", {
            "pdf": lambda payload, **kw: dispatch_calls.append((payload, kw)) or artifact,
        })
        write_calls = []
        monkeypatch.setattr(source_manager, "write_ingested_source",
                             lambda *a, **k: write_calls.append((a, k)) or node_ids)
        mode_a_calls = []
        monkeypatch.setattr(source_manager, "_run_mode_a_topic_extraction",
                             lambda *a, **k: mode_a_calls.append((a, k)) or (topic_ids, overlap_tags))
        backlink_calls = []
        monkeypatch.setattr(source_manager, "run_after_source_manager",
                             lambda *a, **k: backlink_calls.append((a, k)))
        promote_calls = []
        monkeypatch.setattr(source_manager, "auto_partial_promote",
                             lambda *a, **k: promote_calls.append((a, k)))
        notify_calls = []
        monkeypatch.setattr(notify_module, "notify",
                             lambda *a, **k: notify_calls.append((a, k)))
        return {
            "dispatch_calls": dispatch_calls, "write_calls": write_calls,
            "mode_a_calls": mode_a_calls, "backlink_calls": backlink_calls,
            "promote_calls": promote_calls, "notify_calls": notify_calls,
        }

    def test_unknown_kind_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown upload kind"):
            source_manager.process_upload("not_a_real_kind", "payload", "ws-1")

    def test_dispatches_to_matching_ingestor_with_kwargs(self, monkeypatch):
        spies = self._stub_pipeline(monkeypatch)
        source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1", fmt="docx")
        assert spies["dispatch_calls"] == [("/tmp/file.pdf", {"fmt": "docx"})]

    def test_writes_ingested_source_with_expected_args(self, monkeypatch):
        spies = self._stub_pipeline(monkeypatch)
        source_manager.process_upload(
            "pdf", "/tmp/file.pdf", "ws-1", session_id="sess-1",
            created_by="alice", section="research",
        )
        args, kwargs = spies["write_calls"][0]
        # workspace_id is forwarded positionally (write_ingested_source(artifact,
        # workspace_id, created_by=..., section=..., session_id=...)), so check
        # it on `args`, not `kwargs`.
        assert args[1] == "ws-1"
        assert kwargs["created_by"] == "alice"
        assert kwargs["section"] == "research"
        assert kwargs["session_id"] == "sess-1"

    def test_forwards_mode_a_key_override_as_key_override(self, monkeypatch):
        spies = self._stub_pipeline(monkeypatch)
        source_manager.process_upload(
            "pdf", "/tmp/file.pdf", "ws-1", mode_a_key_override="PINNED_KEY",
        )
        _, kwargs = spies["mode_a_calls"][0]
        assert kwargs["key_override"] == "PINNED_KEY"

    def test_calls_run_after_source_manager_with_topic_ids_and_overlap_tags(self, monkeypatch):
        spies = self._stub_pipeline(monkeypatch, topic_ids=["t1", "t2"],
                                     overlap_tags={"t1": {"tag": "new"}})
        source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1", session_id="sess-1")
        args, kwargs = spies["backlink_calls"][0]
        assert args[0] == "ws-1"
        assert args[1] == ["t1", "t2"]
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["overlap_tags"] == {"t1": {"tag": "new"}}

    def test_auto_partial_promote_called_when_topics_found(self, monkeypatch):
        spies = self._stub_pipeline(monkeypatch, topic_ids=["t1"])
        source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1")
        assert len(spies["promote_calls"]) == 1
        args, _ = spies["promote_calls"][0]
        assert args == ("ws-1", source_manager.AUTO_PROMOTE_TARGET_STAGE)

    def test_auto_partial_promote_skipped_when_no_topics(self, monkeypatch):
        spies = self._stub_pipeline(monkeypatch, topic_ids=[])
        source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1")
        assert len(spies["promote_calls"]) == 0

    def test_auto_partial_promote_exception_is_swallowed(self, monkeypatch):
        self._stub_pipeline(monkeypatch, topic_ids=["t1"])

        def raising_promote(*a, **k):
            raise Exception("promote failed")

        monkeypatch.setattr(source_manager, "auto_partial_promote", raising_promote)
        # must not raise
        result = source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1")
        assert result["topic_ids"] == ["t1"]

    def test_notify_called_with_upload_processed_payload(self, monkeypatch):
        artifact = {"title": "Cool Doc", "sections": []}
        spies = self._stub_pipeline(monkeypatch, topic_ids=["t1"], artifact=artifact,
                                     node_ids=["node-a", "node-b"])
        source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1", session_id="sess-1")
        args, kwargs = spies["notify_calls"][0]
        assert args[0] == "sess-1"
        assert args[1] == "upload_processed"
        assert args[2] == {
            "workspace_id": "ws-1", "node_ids": ["node-a", "node-b"],
            "title": "Cool Doc", "topic_ids": ["t1"],
        }

    def test_returns_expected_shape_with_defaulted_title(self, monkeypatch):
        artifact = {"sections": []}  # no "title" key at all
        self._stub_pipeline(monkeypatch, topic_ids=["t1"], artifact=artifact,
                             node_ids=["node-a"])
        result = source_manager.process_upload("pdf", "/tmp/file.pdf", "ws-1")
        assert result == {
            "node_ids": ["node-a"], "title": "Untitled",
            "kind": "pdf", "topic_ids": ["t1"],
        }
