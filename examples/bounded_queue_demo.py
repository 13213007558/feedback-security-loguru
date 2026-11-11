"""Small, self-contained demo of a bounded enqueue queue.

Run with:  python examples/bounded_queue_demo.py

It uses a tiny capacity, a deliberately slow sink and a few short-lived child
processes, then prints how many records were admitted (and later completed),
rejected via the dead-letter callback, and how ``logger.complete()`` behaves.

The capacity counter is a multiprocessing ``Value`` so the parent can aggregate
the counts produced by forked children.
"""

import multiprocessing
import time

from loguru import logger

CAPACITY = 4
WORKERS = 3
PER_WORKER = 10
SINK_DELAY = 0.02


def main():
    logger.remove()

    context = multiprocessing.get_context("fork")
    completed = context.Value("i", 0)
    rejected = context.Value("i", 0)
    barrier = context.Barrier(WORKERS + 1)

    def slow_sink(message):
        time.sleep(SINK_DELAY)
        with completed.get_lock():
            completed.value += 1

    def dead_letter(message):
        with rejected.get_lock():
            rejected.value += 1

    handler_id = logger.add(
        slow_sink,
        context=context,
        enqueue=True,
        queue_maxsize=CAPACITY,
        onqueue_full=dead_letter,
        format="{message}",
    )

    def worker():
        barrier.wait()
        for _ in range(PER_WORKER):
            logger.info("record")

    processes = [context.Process(target=worker) for _ in range(WORKERS)]
    for process in processes:
        process.start()

    barrier.wait()

    for process in processes:
        process.join()
        assert process.exitcode == 0

    # Every record admitted before this point has reached the sink afterwards.
    logger.complete()
    admitted_when_done = completed.value
    logger.remove(handler_id)

    total = WORKERS * PER_WORKER
    print("capacity            :", CAPACITY)
    print("total attempts      :", total)
    print("admitted + completed:", admitted_when_done)
    print("rejected            :", rejected.value)
    print("accounted for       :", admitted_when_done + rejected.value)
    assert admitted_when_done + rejected.value == total
    assert rejected.value > 0, "A slow sink and small queue should cause rejections"


if __name__ == "__main__":
    main()
