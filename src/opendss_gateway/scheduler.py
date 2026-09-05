"""One dispatch slot per worker, handed out in priority order.

A worker runs one engine call at a time (the OpenDSS engine is a process-wide
singleton), so the whole scheduling problem is: which waiting request gets the
next free worker. Two policies sit on top of plain priority:

- a *pool cap*: guest traffic as a whole may hold at most N workers, so a
  signed-in caller always finds one once accounts exist;
- a *per-caller concurrency* limit from the plan.

A waiter blocked by either policy is skipped, not queued behind: a guest who
cannot run because guests hold every allowed worker must not stop the
signed-in caller behind them in line.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field


class QueueFull(Exception):
    """Too many requests already waiting; try again shortly."""


class QueueTimeout(Exception):
    """Waited the allowed time and no worker came free."""


class Draining(Exception):
    """The gateway is shutting down and admits nothing new."""


@dataclass(frozen=True)
class Job:
    client: str
    plan_id: str
    priority: int
    pool: str = "guest"
    concurrency: int = 1


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    job: Job = field(compare=False)
    future: asyncio.Future = field(compare=False)
    enqueued: float = field(compare=False, default_factory=time.monotonic)


class Lease:
    """A held worker. Release exactly once; the scheduler's `finally` blocks
    tolerate a double release so error paths can be simple."""

    def __init__(self, scheduler: Scheduler, worker: str, job: Job, waited: float):
        self._scheduler = scheduler
        self.worker = worker
        self.job = job
        self.waited = waited
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._scheduler._release(self.worker, self.job)


class Scheduler:
    def __init__(self, workers: Sequence[str], pool_caps: dict[str, int] | None = None,
                 max_queue: int = 16, wait_s: float = 45.0):
        if not workers:
            raise ValueError("at least one worker is required")
        self.workers = tuple(workers)
        self.pool_caps = dict(pool_caps or {})
        self.max_queue = max_queue
        self.wait_s = wait_s
        self._free: list[str] = list(self.workers)
        self._waiting: list[_Waiter] = []
        self._pool_inuse: Counter[str] = Counter()
        self._client_inuse: Counter[str] = Counter()
        self._seq = itertools.count()
        self.draining = False

    # -- observability ------------------------------------------------------

    @property
    def depth(self) -> int:
        return len(self._waiting)

    @property
    def inflight(self) -> int:
        return len(self.workers) - len(self._free)

    # -- acquire / release --------------------------------------------------

    async def acquire(self, job: Job, wait_s: float | None = None) -> Lease:
        if self.draining:
            raise Draining
        if len(self._waiting) >= self.max_queue:
            raise QueueFull
        loop = asyncio.get_running_loop()
        waiter = _Waiter(job.priority, next(self._seq), job, loop.create_future())
        heapq.heappush(self._waiting, waiter)
        self._dispatch()
        try:
            worker = await asyncio.wait_for(waiter.future, timeout=self.wait_s
                                            if wait_s is None else wait_s)
        except (TimeoutError, asyncio.CancelledError):
            self._forget(waiter)
            if waiter.future.done() and not waiter.future.cancelled():
                # Granted in the same tick we gave up: hand it straight back.
                self._release(waiter.future.result(), job)
            if isinstance(waiter.future.exception() if waiter.future.done()
                          and not waiter.future.cancelled() else None, Draining):
                raise Draining from None
            raise QueueTimeout from None
        return Lease(self, worker, job, time.monotonic() - waiter.enqueued)

    def _eligible(self, job: Job) -> bool:
        cap = self.pool_caps.get(job.pool)
        if cap is not None and self._pool_inuse[job.pool] >= cap:
            return False
        return self._client_inuse[job.client] < job.concurrency

    def _dispatch(self) -> None:
        if not self._free or not self._waiting:
            return
        skipped: list[_Waiter] = []
        while self._waiting and self._free:
            waiter = heapq.heappop(self._waiting)
            if waiter.future.done():
                continue  # abandoned (timed out) while queued
            if not self._eligible(waiter.job):
                skipped.append(waiter)
                continue
            worker = self._free.pop(0)
            self._pool_inuse[waiter.job.pool] += 1
            self._client_inuse[waiter.job.client] += 1
            waiter.future.set_result(worker)
        for waiter in skipped:
            heapq.heappush(self._waiting, waiter)

    def _release(self, worker: str, job: Job) -> None:
        self._free.append(worker)
        self._pool_inuse[job.pool] -= 1
        self._client_inuse[job.client] -= 1
        self._dispatch()

    def _forget(self, waiter: _Waiter) -> None:
        try:
            self._waiting.remove(waiter)
            heapq.heapify(self._waiting)
        except ValueError:
            pass

    # -- shutdown -----------------------------------------------------------

    async def drain(self, timeout: float) -> int:
        """Refuse new work, fail the queue, wait for in-flight calls.

        Returns how many were still running when the wait ran out."""
        self.draining = True
        for waiter in self._waiting:
            if not waiter.future.done():
                waiter.future.set_exception(Draining())
        self._waiting.clear()
        deadline = time.monotonic() + timeout
        while self.inflight and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return self.inflight
