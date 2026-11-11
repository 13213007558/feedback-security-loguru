import datetime
import multiprocessing
import os
import pickle
import threading
import time

import pytest

from loguru import LoguruQueueFull, logger
from loguru._bounded_queue import BoundedQueue, UnboundedQueue

from .conftest import default_threading_excepthook


@pytest.fixture
def fork_context():
    return multiprocessing.get_context("fork")


class NotPicklable:
    def __getstate__(self):
        raise pickle.PicklingError("You shall not serialize me!")


class NotUnpicklable:
    def __getstate__(self):
        return "..."

    def __setstate__(self, state):
        raise pickle.UnpicklingError("You shall not de-serialize me!")


class SlowSink:
    def __init__(self, delay, written):
        self._delay = delay
        self._written = written

    def write(self, message):
        time.sleep(self._delay)
        self._written.append(message)


def worker_log(logger_, count, barrier=None):
    if barrier is not None:
        barrier.wait()
    for _ in range(count):
        logger_.info("child")


def test_unbounded_queue_keeps_default_behavior():
    handler = logger.add(lambda _: None, enqueue=True, catch=False)
    assert isinstance(logger._core.handlers[handler]._queue, UnboundedQueue)
    logger.remove()


def test_bounded_queue_used_when_maxsize_set():
    handler = logger.add(lambda _: None, enqueue=True, queue_maxsize=4, catch=False)
    assert isinstance(logger._core.handlers[handler]._queue, BoundedQueue)
    logger.remove()


@pytest.mark.parametrize("maxsize", [-1, -10])
def test_invalid_negative_maxsize(maxsize):
    with pytest.raises(ValueError, match="positive integer"):
        logger.add(lambda _: None, enqueue=True, queue_maxsize=maxsize)


@pytest.mark.parametrize("maxsize", [1.5, "3", True])
def test_invalid_maxsize_type(maxsize):
    with pytest.raises(TypeError, match="Invalid queue_maxsize"):
        logger.add(lambda _: None, enqueue=True, queue_maxsize=maxsize)


def test_invalid_onqueue_full_value():
    with pytest.raises(TypeError, match="Invalid onqueue_full"):
        logger.add(
            lambda _: None, enqueue=True, queue_maxsize=1, onqueue_full="nope"
        )


def test_maxsize_ignored_without_enqueue():
    handler_id = logger.add(lambda _: None, queue_maxsize=2)
    handler = logger._core.handlers[handler_id]
    assert handler._queue_maxsize is None
    assert not handler._enqueue
    logger.remove()


def test_onqueue_full_ignored_without_enqueue():
    handler_id = logger.add(lambda _: None, onqueue_full="raise")
    handler = logger._core.handlers[handler_id]
    assert handler._onqueue_full == "drop"
    logger.remove()


def test_onqueue_full_requires_maxsize():
    with pytest.raises(ValueError, match="requires 'queue_maxsize'"):
        logger.add(lambda _: None, enqueue=True, onqueue_full="raise")


def test_env_defaults_do_not_break_non_queued_handler(monkeypatch):
    # Even if the environment would configure bounded-queue defaults, a handler
    # added with enqueue=False must ignore them rather than fail validation.
    monkeypatch.setattr("loguru._logger._defaults.LOGURU_ONQUEUE_FULL", "block")

    handler_id = logger.add(lambda _: None, queue_maxsize=7, onqueue_full="block")
    handler = logger._core.handlers[handler_id]
    assert handler._queue_maxsize is None
    assert not handler._enqueue
    logger.remove()

    # The same options are honored for an explicitly enqueued handler.
    bounded_id = logger.add(
        lambda _: None, enqueue=True, queue_maxsize=7, onqueue_full="block"
    )
    bounded = logger._core.handlers[bounded_id]
    assert bounded._queue_maxsize == 7
    assert bounded._onqueue_full == "block"
    logger.remove()


def test_drop_policy_reports_rejection_on_stderr(capsys):
    written = []
    logger.add(
        SlowSink(0.2, written),
        enqueue=True,
        queue_maxsize=2,
        onqueue_full="drop",
        catch=True,
        format="{message}",
    )

    total = 6
    for i in range(total):
        logger.info("msg {}", i)

    logger.remove()

    out, err = capsys.readouterr()
    assert out == ""
    # With catch=True the drop policy does not raise, it reports on stderr.
    rejected = err.count("queue is full")
    assert rejected > 0
    assert len(written) + rejected == total


def test_drop_policy_without_catch_raises():
    written = []
    logger.add(
        SlowSink(0.2, written),
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="drop",
        catch=False,
        format="{message}",
    )

    with pytest.raises(LoguruQueueFull, match="queue is full"):
        for _ in range(5):
            logger.info("msg")

    logger.remove()


def test_raise_policy_always_propagates_even_with_catch():
    written = []
    logger.add(
        SlowSink(0.2, written),
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="raise",
        catch=True,
        format="{message}",
    )

    raised = 0
    for _ in range(5):
        try:
            logger.info("msg")
        except LoguruQueueFull:
            raised += 1

    logger.remove()
    assert raised > 0
    # The hard signal must never be swallowed into a stderr diagnostic.
    assert len(written) + raised == 5


def test_callback_policy_receives_rejected_message():
    dead_letter = []
    written = []
    logger.add(
        SlowSink(0.2, written),
        enqueue=True,
        queue_maxsize=1,
        onqueue_full=dead_letter.append,
        catch=False,
        format="{message}",
    )

    for i in range(5):
        logger.info("msg {}", i)

    logger.remove()

    assert len(written) + len(dead_letter) == 5
    assert all(message.endswith("\n") for message in dead_letter)
    assert dead_letter, "At least one record should have been rejected"


def test_block_policy_applies_backpressure():
    written = []
    logger.add(
        SlowSink(0.05, written),
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="block",
        catch=False,
        format="{message}",
    )

    for i in range(8):
        logger.info("msg {}", i)

    logger.remove()
    # With backpressure every record is eventually admitted and written.
    assert len(written) == 8


def test_stop_reports_instead_of_hanging_when_thread_survives(monkeypatch):
    import loguru._defaults as defaults

    handler_id = logger.add(
        lambda _: None,
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="block",
        catch=False,
    )
    handler = logger._core.handlers[handler_id]
    real_join = handler._thread.join
    real_is_alive = handler._thread.is_alive

    # Simulate a writer thread that never finishes within the stop timeout.
    monkeypatch.setattr(defaults, "LOGURU_STOP_TIMEOUT", 0)
    monkeypatch.setattr(handler._thread, "join", lambda timeout=None: None)
    monkeypatch.setattr(handler._thread, "is_alive", lambda: True)

    with pytest.raises(RuntimeError, match="did not stop within 0 seconds"):
        handler.stop()

    # The stop sentinel was already queued. Restore the real primitives and let
    # the genuine writer thread drain it and terminate, then drop the handler
    # without invoking "stop()" a second time.
    monkeypatch.setattr(handler._thread, "join", real_join)
    monkeypatch.setattr(handler._thread, "is_alive", real_is_alive)
    handler._thread.join(5)
    assert not handler._thread.is_alive()
    handler._queue.close()
    logger._core.handlers.pop(handler_id)


def test_admitted_rejected_and_completed_counts():
    written = []
    rejected = []
    total = 20
    capacity = 3

    logger.add(
        SlowSink(0.02, written),
        enqueue=True,
        queue_maxsize=capacity,
        onqueue_full=rejected.append,
        catch=False,
        format="{message}",
    )

    admitted = 0
    for i in range(total):
        # A callback policy never raises: rejected records are handed to the
        # dead-letter callback instead, so every logging call returns normally.
        logger.info("msg {}", i)
        admitted += 1

    logger.complete()
    completed_after_barrier = len(written)
    logger.remove()

    assert len(written) + len(rejected) == total
    assert admitted == total
    # complete() guarantees every admitted (queued) record reached the sink.
    assert completed_after_barrier == len(written)
    assert len(rejected) > 0


def test_oversized_record_does_not_hold_a_slot():
    written = []
    big = "x" * (5 * 1024 * 1024)

    logger.add(
        lambda message: written.append(message),
        enqueue=True,
        queue_maxsize=2,
        onqueue_full="raise",
        catch=False,
        format="{message}",
    )

    logger.info("small before")
    logger.info("big {}", big)
    logger.info("small after")
    logger.remove()

    assert len(written) == 3
    assert len(written[1]) > 5 * 1024 * 1024


def test_serialization_failure_releases_slot(capsys):
    written = []
    handler_id = logger.add(
        SlowSink(0.05, written),
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="raise",
        catch=True,
        format="{message}",
    )

    logger.bind(broken=NotPicklable()).info("unpicklable")

    # The failed record must not hold the single slot: after the writer drains
    # each message, a new one can always be admitted.
    for i in range(4):
        deadline = time.time() + 5
        while True:
            try:
                logger.info("ok {}", i)
                break
            except LoguruQueueFull:
                if time.time() > deadline:
                    raise
                time.sleep(0.02)

    handler = logger._core.handlers[handler_id]
    logger.complete()

    # Every slot has been returned: acquiring the full capacity must succeed,
    # and one more acquisition must fail, proving no slot leaked.
    semaphore = handler._queue._semaphore
    assert semaphore.acquire(timeout=2) is True
    assert semaphore.acquire(timeout=0.1) is False
    semaphore.release()

    logger.remove()

    out, err = capsys.readouterr()
    assert len(written) == 4
    assert "PicklingError" in err


def test_unpickling_failure_releases_slot(capsys):
    written = []
    handler_id = logger.add(
        SlowSink(0.02, written),
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="raise",
        catch=True,
        format="{message}",
    )

    with default_threading_excepthook():
        logger.bind(broken=NotUnpicklable()).info("corrupt on arrival")
        for i in range(3):
            deadline = time.time() + 5
            while True:
                try:
                    logger.info("ok {}", i)
                    break
                except LoguruQueueFull:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.02)
        logger.complete()

    handler = logger._core.handlers[handler_id]
    semaphore = handler._queue._semaphore
    assert semaphore.acquire(timeout=2) is True
    assert semaphore.acquire(timeout=0.1) is False
    semaphore.release()

    logger.remove()

    out, err = capsys.readouterr()
    assert len(written) == 3
    assert "UnpicklingError" in err


def test_sink_write_failure_releases_slot(capsys):
    def flaky_sink(message):
        if message.record["extra"].get("fail"):
            raise RuntimeError("sink boom")
        written.append(message)

    written = []
    handler_id = logger.add(
        flaky_sink,
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="raise",
        catch=True,
        format="{message}",
    )

    with default_threading_excepthook():
        logger.bind(fail=True).info("bad one")
        for i in range(4):
            deadline = time.time() + 5
            while True:
                try:
                    logger.info("good {}", i)
                    break
                except LoguruQueueFull:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.02)
        logger.complete()

    handler = logger._core.handlers[handler_id]
    semaphore = handler._queue._semaphore
    assert semaphore.acquire(timeout=2) is True
    assert semaphore.acquire(timeout=0.1) is False
    semaphore.release()

    logger.remove()

    out, err = capsys.readouterr()
    assert len(written) == 4
    assert "sink boom" in err


def _make_record():
    return {
        "elapsed": datetime.timedelta(0),
        "exception": None,
        "extra": {},
        "file": type("F", (), {"name": "f.py", "path": "/f.py"})(),
        "function": "fn",
        "level": type("L", (), {"icon": "i", "name": "INFO", "no": 20})(),
        "line": 1,
        "message": "too late",
        "module": "m",
        "name": "n",
        "process": type("P", (), {"id": 1, "name": "p"})(),
        "template": "too late",
        "thread": type("T", (), {"id": 1, "name": "t"})(),
        "time": datetime.datetime.now(),
    }


def test_rejection_after_handler_stopped_raise_policy():
    bounded_id = logger.add(
        lambda _: None,
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="raise",
        catch=False,
        format="{message}",
    )
    handler = logger._core.handlers[bounded_id]
    logger.remove(bounded_id)

    # The handler is stopped but still reachable (e.g. a message in flight, or a
    # reference held elsewhere): it must reject explicitly instead of vanishing.
    with pytest.raises(LoguruQueueFull, match="handler has been stopped"):
        handler.emit(_make_record(), "INFO", False, False, None)


def test_rejection_after_handler_stopped_callback_policy():
    dead_letter = []
    bounded_id = logger.add(
        lambda _: None,
        enqueue=True,
        queue_maxsize=1,
        onqueue_full=dead_letter.append,
        catch=False,
        format="{message}",
    )
    handler = logger._core.handlers[bounded_id]
    logger.remove(bounded_id)

    handler.emit(_make_record(), "INFO", False, False, None)
    assert len(dead_letter) == 1
    assert "too late" in dead_letter[0]


def test_block_policy_does_not_deadlock_on_stopped_handler():
    bounded_id = logger.add(
        lambda _: None,
        enqueue=True,
        queue_maxsize=1,
        onqueue_full="block",
        catch=False,
        format="{message}",
    )
    handler = logger._core.handlers[bounded_id]
    logger.remove(bounded_id)

    # A blocking policy must not wait forever once the handler is stopped.
    with pytest.raises(LoguruQueueFull, match="handler has been stopped"):
        handler.emit(_make_record(), "INFO", False, False, None)


def test_completion_barrier_does_not_hang_after_sink_errors():
    sink_errors = threading.Event()

    def failing_sink(message):
        sink_errors.set()
        raise RuntimeError("always failing")

    # No default stderr handler is active (the logger is reset by a fixture), so
    # the writer thread can report errors without re-entering its own lock.
    logger.add(
        failing_sink,
        enqueue=True,
        queue_maxsize=2,
        onqueue_full="raise",
        catch=True,
        format="{message}",
    )

    finished = threading.Event()
    rejected = []

    def run():
        for _ in range(5):
            try:
                logger.info("msg")
            except LoguruQueueFull as exception:
                rejected.append(exception)
        # The barrier must still be reached and answered even though every sink
        # write raised: bad records release their slot at dequeue time.
        logger.complete()
        finished.set()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)

    assert finished.is_set()
    assert sink_errors.is_set()
    assert rejected, "The small capacity should have caused at least one rejection"
    logger.remove()


@pytest.mark.skipif(os.name == "nt", reason="Windows does not support forking")
def test_capacity_shared_across_processes(fork_context):
    written = fork_context.Value("i", 0)
    rejected = fork_context.Value("i", 0)
    barrier = fork_context.Barrier(3)
    capacity = 2
    per_worker = 5

    def sink(message):
        time.sleep(0.05)
        with written.get_lock():
            written.value += 1

    def reject(message):
        with rejected.get_lock():
            rejected.value += 1

    logger.add(
        sink,
        context=fork_context,
        enqueue=True,
        queue_maxsize=capacity,
        onqueue_full=reject,
        catch=False,
        format="{message}",
    )

    processes = [
        fork_context.Process(target=worker_log, args=(logger, per_worker, barrier))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    barrier.wait()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0

    logger.remove()

    total = per_worker * 2
    assert written.value + rejected.value == total
    assert rejected.value > 0
