"""Verify the hardened configuration against the same fake-secret fixtures.

Run:  python3 security_log_leak_audit/verify_hardening.py
      pytest security_log_leak_audit/verify_hardening.py -v
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys

from loguru import logger

try:
    from .hardened_logging import (
        configure_hardened_logging,
        intercept_stdlib_logging,
    )
    from .leak_probe import (
        FAKE_GROUP_SECRET,
        FAKE_INNER_SECRET,
        FAKE_PASSWORD,
        FAKE_TEMP_TOKEN,
        SECRET_MARKERS,
        build_exception_group,
        build_explicit_chain,
        build_implicit_context,
        leaked_markers,
    )
except ImportError:  # allow ``python3 security_log_leak_audit/verify_hardening.py``
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from security_log_leak_audit.hardened_logging import (  # type: ignore
        configure_hardened_logging,
        intercept_stdlib_logging,
    )
    from security_log_leak_audit.leak_probe import (  # type: ignore
        FAKE_GROUP_SECRET,
        FAKE_INNER_SECRET,
        FAKE_PASSWORD,
        FAKE_TEMP_TOKEN,
        SECRET_MARKERS,
        build_exception_group,
        build_explicit_chain,
        build_implicit_context,
        leaked_markers,
    )


def _all_outputs() -> tuple[io.StringIO, io.StringIO]:
    text_buf = io.StringIO()
    json_buf = io.StringIO()
    configure_hardened_logging(text_buf, json_buf, enqueue=False)
    return text_buf, json_buf


def test_text_and_json_sinks_redact_message_secrets() -> None:
    text_buf, json_buf = _all_outputs()
    logger.bind(token=FAKE_TEMP_TOKEN).error(f"login pw={FAKE_PASSWORD}")
    logger.complete()
    assert leaked_markers(text_buf.getvalue()) == []
    for line in json_buf.getvalue().splitlines():
        assert leaked_markers(line) == []
        obj = json.loads(line)
        assert obj["extra"]["token"] != FAKE_TEMP_TOKEN


def test_exception_chain_redacted_under_both_diagnose_modes() -> None:
    chain = build_explicit_chain()
    for diagnose in (False, True):
        text_buf, json_buf = _all_outputs_for(diagnose)
        logger.opt(exception=chain).error("login failed")
        logger.complete()
        combined = text_buf.getvalue() + json_buf.getvalue()
        assert leaked_markers(combined) == [], (diagnose, combined)
        for line in json_buf.getvalue().splitlines():
            obj = json.loads(line)
            assert obj["exception"] is not None
            for entry in obj["exception"]:
                assert leaked_markers(entry["value"]) == []


def test_implicit_context_and_exception_group_redacted() -> None:
    text_buf, json_buf = _all_outputs()
    logger.opt(exception=build_implicit_context()).error("context case")
    logger.opt(exception=build_exception_group()).error("group case")
    logger.complete()
    assert leaked_markers(text_buf.getvalue() + json_buf.getvalue()) == []
    assert FAKE_GROUP_SECRET not in text_buf.getvalue()


def test_logger_catch_decorator_path_redacted() -> None:
    text_buf, json_buf = _all_outputs()

    @logger.catch
    def work(password_arg: str) -> None:
        raise RuntimeError("decorated failure: " + password_arg)

    work(FAKE_PASSWORD)
    logger.complete()
    assert leaked_markers(text_buf.getvalue() + json_buf.getvalue()) == []


def test_stdlib_interception_path_redacted() -> None:
    text_buf, json_buf = _all_outputs()
    intercept_stdlib_logging()
    std_logger = logging.getLogger("audit.verify.intercept")
    try:
        raise RuntimeError(f"stdlib inner token={FAKE_INNER_SECRET}")
    except RuntimeError:
        std_local = FAKE_TEMP_TOKEN
        std_logger.exception("stdlib login failed pw=%s", FAKE_PASSWORD)
    logger.complete()
    assert leaked_markers(text_buf.getvalue() + json_buf.getvalue()) == []
    assert std_local == FAKE_TEMP_TOKEN  # fixture sanity, not a log assertion


def test_live_exception_objects_are_not_mutated() -> None:
    chain = build_explicit_chain()
    text_buf, json_buf = _all_outputs()
    logger.opt(exception=chain).error("login failed")
    logger.complete()
    assert FAKE_INNER_SECRET in chain[1].args[0]
    assert chain[1].__cause__ is not None
    # The inner exception message never contained a marker in this fixture,
    # but its args must remain structurally identical after logging.
    assert "clean failure" in chain[1].__cause__.args[0]


def _all_outputs_for(diagnose: bool) -> tuple[io.StringIO, io.StringIO]:
    text_buf = io.StringIO()
    json_buf = io.StringIO()
    configure_hardened_logging(
        text_buf, json_buf, enqueue=False, diagnose=diagnose
    )
    return text_buf, json_buf


def main() -> int:
    failures = 0
    checks = [
        test_text_and_json_sinks_redact_message_secrets,
        test_exception_chain_redacted_under_both_diagnose_modes,
        test_implicit_context_and_exception_group_redacted,
        test_logger_catch_decorator_path_redacted,
        test_stdlib_interception_path_redacted,
        test_live_exception_objects_are_not_mutated,
    ]
    for check in checks:
        try:
            check()
        except AssertionError as exc:  # pragma: no cover - failure reporting
            failures += 1
            print(f"FAIL {check.__name__}: {exc}")
        else:
            print(f"PASS {check.__name__}")
    print()
    print(f"checks={len(checks)} failures={failures}")
    print(f"fake markers exercised: {', '.join(SECRET_MARKERS)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
