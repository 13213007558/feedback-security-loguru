"""Production hardening for Loguru, built only on capabilities Loguru has.

Loguru 0.7.3 provides NO redaction/masking/scrubbing primitive: there is no
secret-aware formatter option, and ``filter`` / ``patcher`` mutate the record
but cannot intercept the better-exceptions frame-local rendering that happens
later inside the handler. The hardening therefore combines:

1. ``diagnose=False`` (and safe ``catch``/``backtrace`` defaults) on every
   sink, which is the only built-in switch that suppresses frame locals.
2. A custom sink that scrubs the *final formatted output*, which is the only
   place that covers message-carried secrets and (if diagnose is ever turned
   back on) rendered locals, without mutating the live exception objects.
3. A stdlib ``logging`` interceptor that routes through the same sinks.

Patterns are explicit fake/test markers here; replace them with your real
credential patterns before deployment.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Callable, Iterable

from loguru import logger

REDACTION = "[REDACTED]"

# Real deployments should extend this list with actual credential shapes
# (Authorization headers, bearer tokens, password= forms, AWS keys, ...).
DEFAULT_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"FAKE-PW-TEST-MARKER-[A-Za-z0-9-]+"),
    re.compile(r"FAKE-TEMP-TOKEN-TEST-MARKER-[A-Za-z0-9-]+"),
    re.compile(r"FAKE-INNER-SECRET-TEST-MARKER-[A-Za-z0-9-]+"),
    re.compile(r"FAKE-GROUP-SECRET-TEST-MARKER-[A-Za-z0-9-]+"),
)


def make_scrubber(
    patterns: Iterable[re.Pattern[str]] = DEFAULT_SECRET_PATTERNS,
    replacement: str = REDACTION,
) -> Callable[[str], str]:
    patterns = tuple(patterns)

    def scrub(text: str) -> str:
        for pattern in patterns:
            text = pattern.sub(replacement, text)
        return text

    return scrub


class ScrubbingStreamSink:
    """Text sink that redacts the fully formatted message before writing.

    Scrubbing ``str(message)`` (the handler's already-rendered output,
    traceback included) is what catches frame locals when diagnose=True.
    Scrubbing at the record level alone cannot do that.
    """

    def __init__(self, stream: Any, scrub: Callable[[str], str]) -> None:
        self._stream = stream
        self._scrub = scrub

    def write(self, message: Any) -> None:
        self._stream.write(self._scrub(str(message)))

    def stop(self) -> None:
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()


class ScrubbingJsonSink:
    """Structured JSON sink that redacts every field it emits itself.

    It deliberately does not rely on Loguru's ``serialize=True``: with
    serialize, the human-readable traceback (including locals under
    diagnose=True) is embedded verbatim in the ``text`` field, while
    ``record.exception`` only carries type/value/boolean. A custom sink must
    therefore choose its own schema and scrub each field independently.
    """

    def __init__(self, stream: Any, scrub: Callable[[str], str]) -> None:
        self._stream = stream
        self._scrub = scrub

    def write(self, message: Any) -> None:
        record = message.record
        payload: dict[str, Any] = {
            "level": record["level"].name,
            "message": self._scrub(record["message"]),
            "extra": {
                key: self._scrub(value) if isinstance(value, str) else value
                for key, value in record["extra"].items()
            },
            "exception": None,
        }
        if record["exception"] is not None:
            chain: list[dict[str, str]] = []
            current = record["exception"].value
            seen: set[int] = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                chain.append(
                    {
                        "type": type(current).__name__,
                        "value": self._scrub(str(current)),
                    }
                )
                current = current.__cause__ or (
                    None if current.__suppress_context__ else current.__context__
                )
            payload["exception"] = chain
            # Include a redacted rendered traceback for readability. This is
            # formatted by Loguru per this sink's diagnose setting, then
            # scrubbed, so locals cannot pass through even if diagnose=True.
            payload["traceback_text"] = self._scrub(str(message))
        self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def stop(self) -> None:
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()


class InterceptHandler(logging.Handler):
    """Route stdlib logging records (including exc_info) into Loguru.

    Mirrors the recipe in Loguru's README. Because it hands ``record.exc_info``
    to ``logger.opt(exception=...)``, the downstream sink's diagnose setting
    and scrubbing rules apply exactly as for native Loguru calls.
    """

    def emit(self, log_record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(log_record.levelname).name
        except ValueError:
            level = log_record.levelno
        frame, depth = logging.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=log_record.exc_info).log(
            level, log_record.getMessage()
        )


def configure_hardened_logging(
    text_stream: Any = sys.stderr,
    json_stream: Any | None = None,
    *,
    diagnose: bool = False,
    backtrace: bool = True,
    catch: bool = False,
    enqueue: bool = True,
    patterns: Iterable[re.Pattern[str]] = DEFAULT_SECRET_PATTERNS,
) -> dict[str, int]:
    """Attach hardened text (and optionally JSON) sinks to the global logger."""
    scrub = make_scrubber(patterns)
    logger.remove()
    handler_ids: dict[str, int] = {}

    handler_ids["text"] = logger.add(
        ScrubbingStreamSink(text_stream, scrub),
        diagnose=diagnose,
        backtrace=backtrace,
        catch=catch,
        enqueue=enqueue,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {message}\n{exception}",
    )
    if json_stream is not None:
        handler_ids["json"] = logger.add(
            ScrubbingJsonSink(json_stream, scrub),
            diagnose=diagnose,
            backtrace=backtrace,
            catch=catch,
            enqueue=enqueue,
        )
    return handler_ids


def intercept_stdlib_logging() -> InterceptHandler:
    handler = InterceptHandler()
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    return handler
