"""Replayable leak-location probe for Loguru sinks.

All sensitive values are FAKE constants defined in this file. The probe never
reads environment variables, credential files, or any process state; it only
raises and logs locally constructed exceptions.

Run directly:  python3 security_log_leak_audit/leak_probe.py
Under pytest:  pytest security_log_leak_audit/leak_probe.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# Fake sensitive values. The TEST-MARKER tag makes their purpose unmistakable.
# ---------------------------------------------------------------------------
FAKE_PASSWORD = "FAKE-PW-TEST-MARKER-9f3a21"
FAKE_TEMP_TOKEN = "FAKE-TEMP-TOKEN-TEST-MARKER-77bd10"
FAKE_INNER_SECRET = "FAKE-INNER-SECRET-TEST-MARKER-c4e8"
FAKE_GROUP_SECRET = "FAKE-GROUP-SECRET-TEST-MARKER-55aa02"

SECRET_MARKERS: tuple[str, ...] = (
    FAKE_PASSWORD,
    FAKE_TEMP_TOKEN,
    FAKE_INNER_SECRET,
    FAKE_GROUP_SECRET,
)


def leaked_markers(text: str) -> list[str]:
    """Return the fake markers actually observed in *text*."""
    return [marker for marker in SECRET_MARKERS if marker in text]


# ---------------------------------------------------------------------------
# Exception fixtures: nested exception, explicit chain, implicit context,
# ExceptionGroup. Each keeps a fake secret in a frame local; some also embed a
# fake secret in the exception message itself.
# ---------------------------------------------------------------------------
def build_explicit_chain() -> tuple[type[BaseException], BaseException, Any]:
    """Separate the two leak classes on purpose.

    * Inner RuntimeError message is clean; FAKE_TEMP_TOKEN exists only as a
      frame local referenced on the raise line (diagnostic-only leak).
    * Outer ValueError message embeds FAKE_INNER_SECRET (message-carried leak,
      visible even with diagnose=False).
    """
    try:
        try:
            token_local = FAKE_TEMP_TOKEN
            raise RuntimeError("inner clean failure; token present? %s" % bool(token_local))
        except RuntimeError as inner_exc:
            password_local = FAKE_PASSWORD
            raise ValueError(
                "outer message embeds marker " + FAKE_INNER_SECRET
            ) from inner_exc
    except ValueError:
        return sys.exc_info()


def build_implicit_context() -> tuple[type[BaseException], BaseException, Any]:
    try:
        try:
            token_local = FAKE_TEMP_TOKEN
            raise RuntimeError("context clean failure; token present? %s" % bool(token_local))
        except RuntimeError:
            password_local = FAKE_PASSWORD
            raise ValueError(
                "implicit context message embeds marker " + FAKE_INNER_SECRET
            )
    except ValueError:
        return sys.exc_info()


def build_exception_group() -> tuple[type[BaseException], BaseException, Any]:
    try:
        try:
            group_local = FAKE_GROUP_SECRET
            raise ValueError(f"group child failure token={FAKE_GROUP_SECRET}")
        except ValueError as child:
            raise ExceptionGroup("outer group", [child]) from child
    except BaseException:
        return sys.exc_info()


@dataclass
class ProbeResult:
    case_id: str
    description: str
    sink_kind: str
    diagnose: bool
    leaked: list[str] = field(default_factory=list)
    notes: str = ""


class ListSink:
    """Minimal custom sink collecting formatted messages in memory."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, message: Any) -> None:
        self.parts.append(str(message))

    def stop(self) -> None:
        return None


class BrokenSink:
    """Sink that always fails, to exercise the ErrorInterceptor path."""

    def write(self, message: Any) -> None:
        raise RuntimeError(f"sink broke while handling pw={FAKE_PASSWORD}")

    def stop(self) -> None:
        return None


def run_all_cases() -> list[ProbeResult]:
    logger.remove()  # drop the default stderr handler; every case adds its own
    results: list[ProbeResult] = []
    chain = build_explicit_chain()
    context = build_implicit_context()
    group = build_exception_group()

    def record(case_id, description, sink_kind, diagnose, text, notes=""):
        results.append(
            ProbeResult(
                case_id=case_id,
                description=description,
                sink_kind=sink_kind,
                diagnose=diagnose,
                leaked=leaked_markers(text),
                notes=notes,
            )
        )

    def add_text_sink(diagnose, *, backtrace=True, enqueue=False, catch=True):
        buf = io.StringIO()
        handler_id = logger.add(
            buf,
            diagnose=diagnose,
            backtrace=backtrace,
            enqueue=enqueue,
            catch=catch,
            format="{level}|{message}\n{exception}",
        )
        return buf, handler_id

    # -- A. Secret carried by the log message itself (no exception) ----------
    secret_message = f"login rejected pw={FAKE_PASSWORD}"
    for diagnose in (False, True):
        buf, handler_id = add_text_sink(diagnose)
        logger.error(secret_message)
        logger.remove(handler_id)
        record("A1", "secret inside logger message, plain text sink", "text",
               diagnose, buf.getvalue())

        buf = io.StringIO()
        handler_id = logger.add(buf, serialize=True, diagnose=diagnose, backtrace=True)
        logger.bind(token=FAKE_TEMP_TOKEN).error(f"auth event pw={FAKE_PASSWORD}")
        logger.remove(handler_id)
        raw = buf.getvalue()
        obj = json.loads(raw)
        notes = (
            f"record.message leak={bool(leaked_markers(obj['record']['message']))}; "
            f"record.extra leak={bool(leaked_markers(json.dumps(obj['record']['extra'])))}; "
            f"text leak={bool(leaked_markers(obj['text']))}"
        )
        record("A2", "secret in message and bind() extra, serialize sink", "json",
               diagnose, raw, notes)

    # -- B. Exception chain: secret in inner exception message + locals ------
    for diagnose in (False, True):
        buf, handler_id = add_text_sink(diagnose)
        logger.opt(exception=chain).error("login failed")
        logger.remove(handler_id)
        record("B1", "explicit __cause__ chain, plain text sink", "text",
               diagnose, buf.getvalue())

        buf = io.StringIO()
        handler_id = logger.add(buf, serialize=True, diagnose=diagnose, backtrace=True)
        logger.opt(exception=chain).error("login failed")
        logger.remove(handler_id)
        raw = buf.getvalue()
        obj = json.loads(raw)
        notes = (
            f"structured exception={obj['record']['exception']}; "
            f"leak lives in text field={bool(leaked_markers(obj['text']))}"
        )
        record("B2", "explicit __cause__ chain, serialize sink", "json",
               diagnose, raw, notes)

        buf, handler_id = add_text_sink(diagnose, backtrace=False)
        logger.opt(exception=context).error("login failed")
        logger.remove(handler_id)
        record("B3", "implicit __context__ chain, backtrace=False", "text",
               diagnose, buf.getvalue())

        sink = ListSink()
        handler_id = logger.add(sink, diagnose=diagnose, backtrace=True, enqueue=True,
                                format="{level}|{message}\n{exception}")
        logger.opt(exception=chain).error("login failed")
        logger.remove(handler_id)
        record("B4", "explicit chain through enqueue=True custom sink",
               "custom-enqueue", diagnose, "".join(sink.parts))

    # -- C. ExceptionGroup ----------------------------------------------------
    for diagnose in (False, True):
        buf, handler_id = add_text_sink(diagnose)
        logger.opt(exception=group).error("group failure")
        logger.remove(handler_id)
        record("C1", "ExceptionGroup with secret-bearing child", "text",
               diagnose, buf.getvalue())

    # -- D. @logger.catch decorator entry ------------------------------------
    for diagnose in (False, True):
        buf, handler_id = add_text_sink(diagnose)

        @logger.catch
        def work(password_arg: str) -> None:
            raise RuntimeError("decorated failure: " + password_arg)

        work(FAKE_PASSWORD)
        logger.remove(handler_id)
        record("D1", "@logger.catch, secret local referenced on raise line",
               "text", diagnose, buf.getvalue())

    # -- E. Standard logging interception entry ------------------------------
    class InterceptHandler(logging.Handler):
        def emit(self, log_record: logging.LogRecord) -> None:
            try:
                level: str | int = logger.level(log_record.levelname).name
            except ValueError:
                level = log_record.levelno
            logger.opt(exception=log_record.exc_info, depth=2).log(
                level, log_record.getMessage()
            )

    for diagnose in (False, True):
        buf, handler_id = add_text_sink(diagnose)
        std_logger = logging.getLogger("audit.probe.intercept")
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False
        try:
            raise RuntimeError(f"stdlib inner token={FAKE_INNER_SECRET}")
        except RuntimeError:
            stdlib_local = FAKE_TEMP_TOKEN
            std_logger.exception("stdlib login failed pw=%s", FAKE_PASSWORD)
        logger.remove(handler_id)
        record("E1", "stdlib logging.exception() via InterceptHandler", "intercept",
               diagnose, buf.getvalue())

    # -- F. Custom callable sink: raw record vs formatted str ----------------
    raw_seen: dict[str, Any] = {}

    def inspecting_sink(message: Any) -> None:
        log_record = message.record
        raw_seen["message"] = str(log_record["message"])
        raw_seen["chain_values"] = []
        current = log_record["exception"].value if log_record["exception"] else None
        seen_ids: set[int] = set()
        while current is not None and id(current) not in seen_ids:
            seen_ids.add(id(current))
            raw_seen["chain_values"].append(repr(current))
            current = current.__cause__ or (
                None if current.__suppress_context__ else current.__context__
            )
        raw_seen["formatted"] = str(message)

    handler_id = logger.add(inspecting_sink, diagnose=False, backtrace=True,
                            format="{message}\n{exception}")
    logger.opt(exception=chain).error("custom sink inspection")
    logger.remove(handler_id)
    record(
        "F1",
        "custom sink receives raw record (exception chain reachable)",
        "custom",
        False,
        json.dumps(raw_seen, default=str),
        "sink code can walk __cause__/__context__ regardless of diagnose",
    )

    # -- G. Sink failure -> ErrorInterceptor fallback to stderr --------------
    for catch in (True, False):
        err = io.StringIO()
        original_stderr = sys.stderr
        sys.stderr = err
        try:
            handler_id = logger.add(BrokenSink(), diagnose=False, catch=catch)
            with contextlib.suppress(Exception):
                logger.error(f"payload token={FAKE_TEMP_TOKEN}")
            logger.remove(handler_id)
        finally:
            sys.stderr = original_stderr
        record(
            f"G1-catch{catch}",
            f"broken sink, catch={catch} (stderr fallback vs propagation)",
            "error-interceptor",
            False,
            err.getvalue(),
            "catch=True prints 'Record was:' repr plus stdlib traceback to stderr; "
            "catch=False re-raises so the fallback path never runs",
        )

    return results


def print_report(results: list[ProbeResult]) -> None:
    header = f"{'case':12} {'sink':15} {'diagnose':8} leaked markers"
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.case_id:12} {result.sink_kind:15} {str(result.diagnose):8} "
            f"{', '.join(result.leaked) if result.leaked else '-'}"
        )
        if result.notes:
            print(f"             note: {result.notes}")


# ---------------------------------------------------------------------------
# Observed-behavior oracle. These expectations document what Loguru 0.7.3
# actually does (verified against loguru/_handler.py and _better_exceptions.py),
# not what a secure logger ideally would do.
#
# LEAK CLASSIFICATION:
#   message-carried  -> the secret was already in message / exception.args /
#                       extra before Loguru formatted anything; diagnose does
#                       not gate it, so it leaks with diagnose=False too.
#   diagnostic       -> the secret only existed as a frame local and was added
#                       by the better-exceptions variable display; it is fully
#                       gated by diagnose=False.
#
# Each entry maps a case id to the markers expected under diagnose=False and
# the additional markers expected only when diagnose=True.
# ---------------------------------------------------------------------------
EXPECTED_LEAKS: dict[str, dict[str, list[str]]] = {
    # A1: secret interpolated into the message itself -> both diagnose modes.
    "A1": {"message_carried": [FAKE_PASSWORD], "diagnostic_only": []},
    # A2: secret message lands in record.message + text; extra is a separate
    # structured field that json.dumps(default=str) emits verbatim.
    "A2": {
        "message_carried": [FAKE_PASSWORD, FAKE_TEMP_TOKEN],
        "diagnostic_only": [],
    },
    # B1/B2/B4: outer exception message carries INNER_SECRET; TEMP_TOKEN is a
    # frame local on the inner raise line and appears only with diagnose=True.
    "B1": {
        "message_carried": [FAKE_INNER_SECRET],
        "diagnostic_only": [FAKE_TEMP_TOKEN],
    },
    "B2": {
        "message_carried": [FAKE_INNER_SECRET],
        "diagnostic_only": [FAKE_TEMP_TOKEN],
    },
    "B4": {
        "message_carried": [FAKE_INNER_SECRET],
        "diagnostic_only": [FAKE_TEMP_TOKEN],
    },
    # B3: backtrace=False still formats the exception chain messages; the
    # inner frame local is rendered per-frame whenever diagnose=True anyway.
    "B3": {
        "message_carried": [FAKE_INNER_SECRET],
        "diagnostic_only": [FAKE_TEMP_TOKEN],
    },
    # C1: ExceptionGroup child message carries the secret.
    "C1": {"message_carried": [FAKE_GROUP_SECRET], "diagnostic_only": []},
    # D1: @logger.catch path; the exception message itself embeds the secret.
    "D1": {"message_carried": [FAKE_PASSWORD], "diagnostic_only": []},
    # E1: stdlib interception keeps both the stdlib formatted message and the
    # chained exception message; the stdlib local is not rendered by Loguru's
    # better-exceptions because the logging call frame is stdlib code.
    "E1": {
        "message_carried": [FAKE_PASSWORD, FAKE_INNER_SECRET],
        "diagnostic_only": [],
    },
    # F1: the raw record handed to a custom sink exposes the whole exception
    # chain; here only the outer message contains a marker.
    "F1": {"message_carried": [FAKE_INNER_SECRET], "diagnostic_only": []},
    # G1: only catch=True triggers the stderr fallback; catch=False re-raises.
    "G1-catchTrue": {
        "message_carried": [FAKE_PASSWORD, FAKE_TEMP_TOKEN],
        "diagnostic_only": [],
    },
    "G1-catchFalse": {"message_carried": [], "diagnostic_only": []},
}


def _case_map() -> dict[str, list[ProbeResult]]:
    grouped: dict[str, list[ProbeResult]] = {}
    for result in run_all_cases():
        grouped.setdefault(result.case_id, []).append(result)
    return grouped


def test_message_carried_secrets_leak_even_with_diagnose_disabled() -> None:
    """Secrets inside messages/exception args are not protected by diagnose."""
    for case_id, expectations in EXPECTED_LEAKS.items():
        results = _case_map().get(case_id, [])
        disabled = [r for r in results if r.diagnose is False]
        if not disabled:
            continue
        observed = sorted({marker for r in disabled for marker in r.leaked})
        assert observed == sorted(expectations["message_carried"]), case_id


def test_diagnose_true_adds_frame_locals_on_top_of_message_secrets() -> None:
    """With diagnose=True, frame locals referenced on a traceback line appear.

    B1's outer message already leaks INNER_SECRET under diagnose=False; under
    diagnose=True the inner frame-local TEMP_TOKEN is additionally rendered.
    """
    results = {r.diagnose: r for r in _case_map()["B1"]}
    assert FAKE_INNER_SECRET in results[False].leaked
    assert FAKE_TEMP_TOKEN not in results[False].leaked
    assert set(results[True].leaked) == {FAKE_INNER_SECRET, FAKE_TEMP_TOKEN}


def test_diagnostic_locals_are_gated_but_message_secrets_are_not() -> None:
    """Across chain cases, diagnose only changes the local-variable surface."""
    for case_id in ("B1", "B2", "B3", "B4"):
        results = {r.diagnose: r for r in _case_map()[case_id]}
        carried = set(EXPECTED_LEAKS[case_id]["message_carried"])
        diagnostic = set(EXPECTED_LEAKS[case_id]["diagnostic_only"])
        assert carried.issubset(set(results[False].leaked))
        assert diagnostic.isdisjoint(set(results[False].leaked))
        assert diagnostic.issubset(set(results[True].leaked))


def test_catch_false_skips_stderr_fallback() -> None:
    results = {r.case_id: r for r in run_all_cases()}
    assert results["G1-catchFalse"].leaked == []
    assert set(results["G1-catchTrue"].leaked) == {
        FAKE_PASSWORD,
        FAKE_TEMP_TOKEN,
    }


def test_all_fake_markers_are_explicitly_tagged() -> None:
    for marker in SECRET_MARKERS:
        assert "FAKE-" in marker and "TEST-MARKER" in marker


def main() -> int:
    results = run_all_cases()
    print_report(results)
    leaky = [r for r in results if r.leaked]
    print()
    print(f"cases={len(results)} cases_with_observed_leak={len(leaky)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
