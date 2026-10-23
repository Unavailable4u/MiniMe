"""
m2c/tests/test_logging_setup.py -- Patch C01: "logging never records message
content".

The rule (build plan §11.4 applied to the client): nothing a user typed, a
model produced, or a tool returned may end up in a log line.  These tests
prove it for every route a value can take into a log record:

  * a field passed to log_event()                    (allowlist)
  * a %-style argument                               (args are redacted)
  * an ``extra=`` attribute                          (non-standard attrs removed)
  * an exception message in a traceback              (type name only)
  * a non-string object passed as the message        (suppressed)
  * a record already formatted by another handler    (exc_text cache)

and finish with an end-to-end canary: the real CLI runs ``doctor`` at DEBUG
level, with a secret in the project path, and the secret must not appear.
"""
from __future__ import annotations

import io
import logging

import pytest

from m2c import cli, doctor
from m2c import logging_setup as ls

SECRET = "SECRET-PROMPT-do-not-log-me-7f3a"


@pytest.fixture
def stream():
    return io.StringIO()


@pytest.fixture
def log(stream):
    logger = ls.configure_logging(logging.DEBUG, stream=stream)
    yield logging.getLogger("m2c.test")
    # leave no handlers behind for other tests
    for h in list(logging.getLogger(ls.LOGGER_NAME).handlers):
        logging.getLogger(ls.LOGGER_NAME).removeHandler(h)
    assert logger is logging.getLogger(ls.LOGGER_NAME)


# --------------------------------------------------------------------------
# Every route a value can take into a record
# --------------------------------------------------------------------------
def test_log_event_drops_non_allowlisted_fields(log, stream):
    ls.log_event(log, logging.INFO, "run.started", content=SECRET, prompt=SECRET, seq=3)
    out = stream.getvalue()
    assert SECRET not in out
    assert "seq=3" in out
    assert "_dropped=2" in out


def test_log_event_redacts_unsafe_string_values_even_for_allowed_keys(log, stream):
    ls.log_event(log, logging.INFO, "run.started", model=SECRET + " with spaces", code="ok_1")
    out = stream.getvalue()
    assert SECRET not in out
    assert "model=<redacted>" in out
    assert "code=ok_1" in out


def test_log_event_redacts_non_scalar_values(log, stream):
    ls.log_event(log, logging.INFO, "run.started", count=[SECRET], status={"k": SECRET})
    out = stream.getvalue()
    assert SECRET not in out
    assert "count=<redacted>" in out and "status=<redacted>" in out


def test_log_event_passes_numbers_and_bools(log, stream):
    ls.log_event(log, logging.INFO, "usage.recorded", input_tokens=12, credits=0.5, check=True)
    out = stream.getvalue()
    assert "input_tokens=12" in out and "credits=0.5" in out and "check=true" in out


def test_log_event_rejects_bad_event_names(log, stream):
    ls.log_event(log, logging.INFO, f"hello {SECRET}")
    out = stream.getvalue()
    assert SECRET not in out
    assert "invalid_event" in out


def test_percent_args_are_never_interpolated(log, stream):
    log.info("user said %s and %d", SECRET, 42)
    out = stream.getvalue()
    assert SECRET not in out
    assert "42" not in out
    assert "user said %s and %d" in out  # the literal template survives


def test_mapping_style_args_are_never_interpolated(log, stream):
    log.info("user said %(text)s", {"text": SECRET})
    assert SECRET not in stream.getvalue()


def test_extra_attributes_are_not_emitted(log, stream):
    log.info("something happened", extra={"content": SECRET, "prompt": SECRET})
    assert SECRET not in stream.getvalue()


def test_exception_message_is_not_logged_only_the_type(log, stream):
    try:
        raise ValueError(f"bad input: {SECRET}")
    except ValueError:
        log.error("failed", exc_info=True)
    out = stream.getvalue()
    assert SECRET not in out
    assert "ValueError" in out
    assert "Traceback" not in out


def test_exception_chain_messages_are_not_logged(log, stream):
    try:
        try:
            raise KeyError(SECRET)
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError:
        log.exception("failed")
    assert SECRET not in stream.getvalue()


def test_non_string_message_object_is_suppressed(log, stream):
    class Msg:
        def __str__(self):
            return SECRET

    log.info(Msg())
    out = stream.getvalue()
    assert SECRET not in out
    assert "suppressed" in out


def test_exc_text_cached_by_another_handler_does_not_leak(stream):
    """logging.Formatter caches the formatted traceback on the record.  If a
    plain handler formats first, our handler must not reuse that text."""
    logger = logging.getLogger("m2c.cachetest")
    logger.setLevel(logging.DEBUG)
    plain_stream = io.StringIO()
    plain = logging.StreamHandler(plain_stream)  # default Formatter: full traceback
    safe = ls.build_handler(stream)
    logger.addHandler(plain)
    logger.addHandler(safe)
    logger.propagate = False
    try:
        try:
            raise ValueError(SECRET)
        except ValueError:
            logger.error("failed", exc_info=True)
    finally:
        logger.removeHandler(plain)
        logger.removeHandler(safe)
    assert SECRET in plain_stream.getvalue()  # the control: the leak is real
    assert SECRET not in stream.getvalue()


# --------------------------------------------------------------------------
# Filter / formatter unit behaviour
# --------------------------------------------------------------------------
def _record(msg="m", args=(), **extra):
    rec = logging.LogRecord("m2c.x", logging.INFO, __file__, 1, msg, args, None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_filter_strips_args_and_extras_but_never_drops_records():
    rec = _record("hello %s", (SECRET,), content=SECRET, tool_output=SECRET)
    assert ls.ContentFreeFilter().filter(rec) is True
    assert rec.args == ()
    assert not hasattr(rec, "content") and not hasattr(rec, "tool_output")
    assert SECRET not in rec.getMessage()


def test_filter_keeps_standard_attributes():
    rec = _record("plain")
    ls.ContentFreeFilter().filter(rec)
    assert rec.levelname == "INFO" and rec.name == "m2c.x"


# --------------------------------------------------------------------------
# configure_logging
# --------------------------------------------------------------------------
def test_configure_is_idempotent_and_does_not_propagate(stream):
    ls.configure_logging(logging.INFO, stream=stream)
    ls.configure_logging(logging.INFO, stream=stream)
    logger = logging.getLogger(ls.LOGGER_NAME)
    try:
        assert len([h for h in logger.handlers if getattr(h, ls._HANDLER_MARK, False)]) == 1
        assert logger.propagate is False
        assert logger.level == logging.INFO
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)


def test_default_stream_is_stderr(capsys):
    ls.configure_logging(logging.WARNING)
    try:
        logging.getLogger("m2c.t").warning("to stderr")
    finally:
        logger = logging.getLogger(ls.LOGGER_NAME)
        for h in list(logger.handlers):
            logger.removeHandler(h)
    captured = capsys.readouterr()
    assert "to stderr" in captured.err
    assert captured.out == ""


def test_level_is_respected(stream):
    ls.configure_logging(logging.ERROR, stream=stream)
    try:
        logging.getLogger("m2c.t").warning("hidden")
        logging.getLogger("m2c.t").error("shown")
    finally:
        logger = logging.getLogger(ls.LOGGER_NAME)
        for h in list(logger.handlers):
            logger.removeHandler(h)
    assert "hidden" not in stream.getvalue()
    assert "shown" in stream.getvalue()


# --------------------------------------------------------------------------
# End-to-end canary: the real CLI path
# --------------------------------------------------------------------------
def test_canary_cli_doctor_at_debug_logs_no_content(tmp_path, monkeypatch, make_env, capsys):
    """Run the real ``main()`` at DEBUG with a secret inside the project path
    and inside an exception raised by a probe.  Neither may reach the log
    stream (stderr)."""
    secret_dir = tmp_path / SECRET
    secret_dir.mkdir()

    def exploding_which(name):
        if name == "bwrap":
            raise OSError(f"cannot look up {SECRET}")
        return f"/usr/bin/{name}"

    env = make_env(
        project_path=secret_dir,
        resolved_project_path=secret_dir,
        which=exploding_which,
    )
    monkeypatch.setattr(doctor.Environment, "detect", classmethod(lambda cls, project=None: env))
    monkeypatch.setenv("M2C_LOG_LEVEL", "DEBUG")
    try:
        code = cli.main(["doctor", "--project", str(secret_dir)])
    finally:
        logger = logging.getLogger(ls.LOGGER_NAME)
        for h in list(logger.handlers):
            logger.removeHandler(h)
    captured = capsys.readouterr()
    assert code == 0  # bwrap crash is a soft check -> warning only
    assert SECRET not in captured.err
    assert "doctor" in captured.err  # DEBUG events did get logged (the canary is live)
