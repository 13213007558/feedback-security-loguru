import multiprocessing


class LoguruQueueFull(Exception):
    """Raised when a bounded enqueue handler rejects a log record.

    It is raised by handlers configured with ``onqueue_full="raise"`` (or by a
    custom rejection callback) once the shared queue is full, or when an
    attempt to enqueue a record is made after the handler has been stopped.

    Inherits from :class:`Exception` rather than :class:`queue.Full` so that
    the rejection signal remains available on every Python version supported
    by Loguru and does not depend on the underlying queue implementation.
    """


class UnboundedQueue:
    """A thin wrapper around :class:`multiprocessing.SimpleQueue`.

    It mirrors the interface of :class:`BoundedQueue` (``put(item, block, timeout)``
    and ``get()``) so that the handler can use either one transparently. The
    ``block`` and ``timeout`` arguments are accepted but have no effect: the
    underlying queue never fills up, preserving the historical enqueue behavior.
    """

    def __init__(self, multiprocessing_context=None):
        if multiprocessing_context is None:
            self._queue = multiprocessing.SimpleQueue()
        else:
            self._queue = multiprocessing_context.SimpleQueue()

    def put(self, item, block=True, timeout=None):
        self._queue.put(item)

    def get(self):
        return self._queue.get()

    def close(self):
        if hasattr(self._queue, "close"):
            self._queue.close()


class BoundedQueue:
    """A multiprocessing-safe queue with a fixed number of in-flight slots.

    This wrapper combines a :class:`multiprocessing.SimpleQueue` (which has no
    built-in capacity limit) with a shared :class:`multiprocessing.BoundedSemaphore`.
    The semaphore is the single source of truth for the available capacity, so
    the limit is enforced across every process and thread sharing the handler.

    A slot is reserved *before* the record is serialized and pushed, and it is
    released by the writer thread as soon as the record has been taken out of
    the queue, before the sink is called. A record that fails to be serialized
    therefore never holds a slot, and a record whose sink raises releases its
    slot before the error is reported, so a single bad record can neither fill
    the queue nor block a completion barrier.

    Internal control markers (the stop sentinel ``None`` and the completion
    barrier ``True``) bypass the semaphore and do not consume capacity.
    """

    def __init__(self, maxsize, multiprocessing_context=None):
        if multiprocessing_context is None:
            self._queue = multiprocessing.SimpleQueue()
            self._semaphore = multiprocessing.BoundedSemaphore(maxsize)
        else:
            self._queue = multiprocessing_context.SimpleQueue()
            self._semaphore = multiprocessing_context.BoundedSemaphore(maxsize)

    def put(self, item, block=True, timeout=None):
        """Put a record into the queue, reserving one capacity slot.

        Control markers (``None`` and ``True``) are forwarded directly without
        touching the semaphore. For any other item, a slot must be acquired
        first. If it cannot be acquired (the queue is full) a
        :class:`LoguruQueueFull` exception is raised and no slot is consumed.

        If the subsequent queue insertion fails, in particular because the
        record could not be pickled, the reserved slot is released before the
        exception propagates, so a rejected record never leaks capacity.
        """
        if item is None or item is True:
            self._queue.put(item)
            return

        acquired = self._semaphore.acquire(block, timeout)
        if not acquired:
            raise LoguruQueueFull(
                "Log record has been rejected because the enqueue queue is full"
            )

        try:
            self._queue.put(item)
        except Exception:
            self._semaphore.release()
            raise

    def get(self):
        """Remove and return the next item, releasing the slot of a record.

        Markers carry no slot, so only actual records release the semaphore.
        Releasing the slot at dequeue time (rather than after the sink write)
        guarantees that a failing or slow sink cannot permanently hold a slot.

        The slot is released even if unpickling the payload raises: by then the
        item has already been consumed from the underlying pipe, so a corrupted
        record must not leak capacity either.
        """
        try:
            item = self._queue.get()
        except Exception:
            # The item was removed from the pipe but could not be reconstructed.
            # We cannot tell it apart from a marker at this point, so release a
            # slot defensively; a BoundedSemaphore only raises on over-release,
            # which cannot happen for a genuinely consumed record.
            self._semaphore.release()
            raise

        if item is not None and item is not True:
            self._semaphore.release()
        return item

    def close(self):
        if hasattr(self._queue, "close"):
            self._queue.close()
