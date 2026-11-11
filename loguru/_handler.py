import functools
import json
import multiprocessing
import os
import threading
from contextlib import contextmanager
from threading import Thread

from ._bounded_queue import BoundedQueue, LoguruQueueFull, UnboundedQueue
from ._colorizer import Colorizer
from ._locks_machinery import create_handler_lock
from . import _defaults


def prepare_colored_format(format_, ansi_level):
    colored = Colorizer.prepare_format(format_)
    return colored, colored.colorize(ansi_level)


def prepare_stripped_format(format_):
    colored = Colorizer.prepare_format(format_)
    return colored.strip()


def memoize(function):
    return functools.lru_cache(maxsize=64)(function)


class Message(str):
    __slots__ = ("record",)


class _UncatchableQueueFull(LoguruQueueFull):
    """A queue-full rejection that must propagate even when ``catch=True``.

    It is only used internally for the ``onqueue_full="raise"`` policy so that
    the rejection signal is never silently converted into a stderr diagnostic.
    """


class Handler:
    def __init__(
        self,
        *,
        sink,
        name,
        levelno,
        formatter,
        is_formatter_dynamic,
        filter_,
        colorize,
        serialize,
        enqueue,
        queue_maxsize,
        onqueue_full,
        multiprocessing_context,
        error_interceptor,
        exception_formatter,
        id_,
        levels_ansi_codes
    ):
        self._name = name
        self._sink = sink
        self._levelno = levelno
        self._formatter = formatter
        self._is_formatter_dynamic = is_formatter_dynamic
        self._filter = filter_
        self._colorize = colorize
        self._serialize = serialize
        self._enqueue = enqueue
        self._queue_maxsize = queue_maxsize
        self._onqueue_full = onqueue_full
        self._multiprocessing_context = multiprocessing_context
        self._error_interceptor = error_interceptor
        self._exception_formatter = exception_formatter
        self._id = id_
        self._levels_ansi_codes = levels_ansi_codes  # Warning, reference shared among handlers

        self._decolorized_format = None
        self._precolorized_formats = {}
        self._memoize_dynamic_format = None

        self._stopped = False
        self._lock = create_handler_lock()
        self._lock_acquired = threading.local()
        self._queue = None
        self._queue_lock = None
        self._confirmation_event = None
        self._confirmation_lock = None
        self._owner_process_pid = None
        self._thread = None

        if self._is_formatter_dynamic:
            if self._colorize:
                self._memoize_dynamic_format = memoize(prepare_colored_format)
            else:
                self._memoize_dynamic_format = memoize(prepare_stripped_format)
        else:
            if self._colorize:
                for level_name in self._levels_ansi_codes:
                    self.update_format(level_name)
            else:
                self._decolorized_format = self._formatter.strip()

        if self._enqueue:
            if self._multiprocessing_context is None:
                if self._queue_maxsize is None:
                    self._queue = UnboundedQueue(multiprocessing_context=None)
                else:
                    self._queue = BoundedQueue(
                        self._queue_maxsize, multiprocessing_context=None
                    )
                self._confirmation_event = multiprocessing.Event()
                self._confirmation_lock = multiprocessing.Lock()
            else:
                if self._queue_maxsize is None:
                    self._queue = UnboundedQueue(
                        multiprocessing_context=self._multiprocessing_context
                    )
                else:
                    self._queue = BoundedQueue(
                        self._queue_maxsize,
                        multiprocessing_context=self._multiprocessing_context,
                    )
                self._confirmation_event = self._multiprocessing_context.Event()
                self._confirmation_lock = self._multiprocessing_context.Lock()
            self._queue_lock = create_handler_lock()
            self._owner_process_pid = os.getpid()
            self._thread = Thread(
                target=self._queued_writer, daemon=True, name="loguru-writer-%d" % self._id
            )
            self._thread.start()

    def __repr__(self):
        return "(id=%d, level=%d, sink=%s)" % (self._id, self._levelno, self._name)

    @contextmanager
    def _protected_lock(self):
        """Acquire the lock, but fail fast if its already acquired by the current thread."""
        if getattr(self._lock_acquired, "acquired", False):
            raise RuntimeError(
                "Could not acquire internal lock because it was already in use (deadlock avoided). "
                "This likely happened because the logger was re-used inside a sink, a signal "
                "handler or a '__del__' method. This is not permitted because the logger and its "
                "handlers are not re-entrant."
            )
        try:
            self._lock_acquired.acquired = True
            with self._lock:
                yield
        finally:
            self._lock_acquired.acquired = False

    def emit(self, record, level_id, from_decorator, is_raw, colored_message):
        try:
            if self._levelno > record["level"].no:
                return

            if self._filter is not None:
                if not self._filter(record):
                    return

            if self._is_formatter_dynamic:
                dynamic_format = self._formatter(record)

            formatter_record = record.copy()

            if not record["exception"]:
                formatter_record["exception"] = ""
            else:
                type_, value, tb = record["exception"]
                formatter = self._exception_formatter
                lines = formatter.format_exception(type_, value, tb, from_decorator=from_decorator)
                formatter_record["exception"] = "".join(lines)

            if colored_message is not None and colored_message.stripped != record["message"]:
                colored_message = None

            if is_raw:
                if colored_message is None or not self._colorize:
                    formatted = record["message"]
                else:
                    ansi_level = self._levels_ansi_codes[level_id]
                    formatted = colored_message.colorize(ansi_level)
            elif self._is_formatter_dynamic:
                if not self._colorize:
                    precomputed_format = self._memoize_dynamic_format(dynamic_format)
                    formatted = self._format_record(precomputed_format, formatter_record)
                elif colored_message is None:
                    ansi_level = self._levels_ansi_codes[level_id]
                    _, precomputed_format = self._memoize_dynamic_format(dynamic_format, ansi_level)
                    formatted = self._format_record(precomputed_format, formatter_record)
                else:
                    ansi_level = self._levels_ansi_codes[level_id]
                    formatter, precomputed_format = self._memoize_dynamic_format(
                        dynamic_format, ansi_level
                    )
                    coloring_message = formatter.make_coloring_message(
                        record["message"], ansi_level=ansi_level, colored_message=colored_message
                    )
                    formatter_record["message"] = coloring_message
                    formatted = self._format_record(precomputed_format, formatter_record)

            else:
                if not self._colorize:
                    precomputed_format = self._decolorized_format
                    formatted = self._format_record(precomputed_format, formatter_record)
                elif colored_message is None:
                    ansi_level = self._levels_ansi_codes[level_id]
                    precomputed_format = self._precolorized_formats[level_id]
                    formatted = self._format_record(precomputed_format, formatter_record)
                else:
                    ansi_level = self._levels_ansi_codes[level_id]
                    precomputed_format = self._precolorized_formats[level_id]
                    coloring_message = self._formatter.make_coloring_message(
                        record["message"], ansi_level=ansi_level, colored_message=colored_message
                    )
                    formatter_record["message"] = coloring_message
                    formatted = self._format_record(precomputed_format, formatter_record)

            if self._serialize:
                formatted = self._serialize_record(formatted, record)

            str_record = Message(formatted)
            str_record.record = record

            with self._protected_lock():
                if self._stopped:
                    if self._enqueue and self._queue_maxsize is not None:
                        self._reject_stopped_record(str_record)
                    return
                if self._enqueue:
                    self._enqueue_record(str_record)
                else:
                    self._sink.write(str_record)
        except _UncatchableQueueFull:
            raise
        except Exception:
            if not self._error_interceptor.should_catch():
                raise
            self._error_interceptor.print(record)

    def _enqueue_record(self, str_record):
        if self._onqueue_full == "block":
            # Backpressure: the logging call waits until a slot is available.
            self._queue.put(str_record, block=True)
            return

        try:
            self._queue.put(str_record, block=False)
        except LoguruQueueFull as exception:
            if self._onqueue_full == "raise":
                # An explicit hard signal: it always reaches the caller so that
                # rejection can be handled programmatically, even with catch=True.
                raise _UncatchableQueueFull(str(exception)) from None
            if self._onqueue_full == "drop":
                # Soft signal: it goes through the usual "catch" handling, so by
                # default the rejection is reported on sys.stderr.
                raise
            # A user-provided callback acts as a dead-letter sink. If it returns
            # normally, the record is considered handled and no error is raised.
            self._onqueue_full(str_record)

    def _reject_stopped_record(self, str_record):
        message = "Log record has been rejected because the handler has been stopped"
        if self._onqueue_full == "raise":
            raise _UncatchableQueueFull(message)
        if callable(self._onqueue_full):
            self._onqueue_full(str_record)
            return
        raise LoguruQueueFull(message)

    def stop(self):
        with self._protected_lock():
            self._stopped = True
            if self._enqueue:
                if self._owner_process_pid != os.getpid():
                    return
                self._queue.put(None)
                self._thread.join(timeout=_defaults.LOGURU_STOP_TIMEOUT)
                if self._thread.is_alive():
                    # The writer thread could not finish in time, typically because another
                    # thread is stuck on a "block" policy while the queue is full. We cannot
                    # join it without hanging the process, so report the situation explicitly
                    # instead of pretending the handler shut down cleanly.
                    raise RuntimeError(
                        "The enqueue writer thread did not stop within %d seconds. This can "
                        "happen if a logging call is blocked on a full queue while the handler "
                        "is being removed." % _defaults.LOGURU_STOP_TIMEOUT
                    )
                if hasattr(self._queue, "close"):
                    self._queue.close()

            try:
                self._sink.stop()
            except Exception:
                if not self._error_interceptor.should_catch():
                    raise
                self._error_interceptor.print(None)

    def complete_queue(self):
        if not self._enqueue:
            return

        with self._confirmation_lock:
            self._queue.put(True)
            self._confirmation_event.wait()
            self._confirmation_event.clear()

    def tasks_to_complete(self):
        if self._enqueue and self._owner_process_pid != os.getpid():
            return []
        lock = self._queue_lock if self._enqueue else self._protected_lock()
        with lock:
            return self._sink.tasks_to_complete()

    def update_format(self, level_id):
        if not self._colorize or self._is_formatter_dynamic:
            return
        ansi_code = self._levels_ansi_codes[level_id]
        self._precolorized_formats[level_id] = self._formatter.colorize(ansi_code)

    @property
    def levelno(self):
        return self._levelno

    @staticmethod
    def _format_record(log_format, record):
        try:
            return log_format.format_map(record)
        except KeyError as e:
            available = ", ".join(map(repr, record.keys()))
            raise ValueError(
                "Failed to format log record: key %s not found.\n"
                "Verify that the format string %r only references valid record keys "
                "and that all required extra keys are present.\n"
                "Available records key are: %s.\n"
                "While using a dynamic formatter as a function, note that it must return "
                "the string to be formatted, not an already formatted message.\n"
                "To include custom data, use 'logger.bind(key=value)' and reference it "
                "as '{extra[key]}' in the format string." % (e, log_format, available)
            ) from e

    @staticmethod
    def _serialize_record(text, record):
        exception = record["exception"]

        if exception is not None:
            exception = {
                "type": None if exception.type is None else exception.type.__name__,
                "value": exception.value,
                "traceback": bool(exception.traceback),
            }

        serializable = {
            "text": text,
            "record": {
                "elapsed": {
                    "repr": record["elapsed"],
                    "seconds": record["elapsed"].total_seconds(),
                },
                "exception": exception,
                "extra": record["extra"],
                "file": {"name": record["file"].name, "path": record["file"].path},
                "function": record["function"],
                "level": {
                    "icon": record["level"].icon,
                    "name": record["level"].name,
                    "no": record["level"].no,
                },
                "line": record["line"],
                "message": record["message"],
                "module": record["module"],
                "name": record["name"],
                "process": {"id": record["process"].id, "name": record["process"].name},
                "template": record["template"],
                "thread": {"id": record["thread"].id, "name": record["thread"].name},
                "time": {"repr": record["time"], "timestamp": record["time"].timestamp()},
            },
        }

        return json.dumps(serializable, default=str, ensure_ascii=False) + "\n"

    def _queued_writer(self):
        message = None
        queue = self._queue

        # We need to use a lock to protect sink during fork.
        # Particularly, writing to stderr may lead to deadlock in child process.
        lock = self._queue_lock

        while True:
            try:
                message = queue.get()
            except Exception:
                with lock:
                    self._error_interceptor.print(None)
                continue

            if message is None:
                break

            if message is True:
                self._confirmation_event.set()
                continue

            with lock:
                try:
                    self._sink.write(message)
                except Exception:
                    self._error_interceptor.print(message.record)

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_lock"] = None
        state["_lock_acquired"] = None
        state["_memoize_dynamic_format"] = None
        if self._enqueue:
            state["_sink"] = None
            state["_thread"] = None
            state["_queue_lock"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = create_handler_lock()
        self._lock_acquired = threading.local()
        if self._enqueue:
            self._queue_lock = create_handler_lock()
        if self._is_formatter_dynamic:
            if self._colorize:
                self._memoize_dynamic_format = memoize(prepare_colored_format)
            else:
                self._memoize_dynamic_format = memoize(prepare_stripped_format)
