import asyncio

import pytest

from opendss_gateway.scheduler import Draining, Job, QueueFull, QueueTimeout, Scheduler

pytestmark = pytest.mark.anyio


def job(client="a", priority=10, pool="guest", concurrency=1, plan="guest") -> Job:
    return Job(client=client, plan_id=plan, priority=priority, pool=pool,
               concurrency=concurrency)


async def test_free_workers_are_handed_out_immediately():
    s = Scheduler(["w1", "w2"])
    a = await s.acquire(job("a"))
    b = await s.acquire(job("b"))
    assert {a.worker, b.worker} == {"w1", "w2"}
    assert s.inflight == 2 and s.depth == 0
    a.release()
    assert s.inflight == 1


async def test_waiters_are_served_in_priority_order_when_a_worker_frees():
    s = Scheduler(["w1"])
    held = await s.acquire(job("holder"))
    low = asyncio.ensure_future(s.acquire(job("low", priority=10)))
    await asyncio.sleep(0)
    high = asyncio.ensure_future(s.acquire(job("high", priority=1)))
    await asyncio.sleep(0)
    assert s.depth == 2
    held.release()
    winner = await asyncio.wait_for(high, 1)
    assert winner.worker == "w1"
    assert not low.done()
    winner.release()
    (await asyncio.wait_for(low, 1)).release()


async def test_pool_cap_leaves_a_worker_for_other_pools():
    s = Scheduler(["w1", "w2"], pool_caps={"guest": 1})
    g1 = await s.acquire(job("g1"))
    g2 = asyncio.ensure_future(s.acquire(job("g2")))
    await asyncio.sleep(0)
    assert not g2.done(), "second guest must wait although a worker is free"
    member = await asyncio.wait_for(s.acquire(job("m", priority=1, pool="member")), 1)
    assert member.worker != g1.worker
    g1.release()
    (await asyncio.wait_for(g2, 1)).release()
    member.release()


async def test_blocked_waiter_does_not_block_the_line_behind_it():
    """A guest blocked by the pool cap is skipped so a member behind them runs."""
    s = Scheduler(["w1", "w2"], pool_caps={"guest": 1})
    g1 = await s.acquire(job("g1"))
    g2 = asyncio.ensure_future(s.acquire(job("g2", priority=5)))
    await asyncio.sleep(0)
    m = asyncio.ensure_future(s.acquire(job("m", priority=9, pool="member")))
    await asyncio.sleep(0)
    lease = await asyncio.wait_for(m, 1)
    assert not g2.done()
    lease.release(); g1.release()
    (await asyncio.wait_for(g2, 1)).release()


async def test_per_client_concurrency():
    s = Scheduler(["w1", "w2"])
    first = await s.acquire(job("same"))
    second = asyncio.ensure_future(s.acquire(job("same")))
    await asyncio.sleep(0)
    assert not second.done()
    first.release()
    (await asyncio.wait_for(second, 1)).release()


async def test_queue_full_and_timeout():
    s = Scheduler(["w1"], max_queue=1, wait_s=0.05)
    held = await s.acquire(job("h"))
    waiting = asyncio.ensure_future(s.acquire(job("w")))
    await asyncio.sleep(0)
    with pytest.raises(QueueFull):
        await s.acquire(job("overflow"))
    with pytest.raises(QueueTimeout):
        await waiting
    assert s.depth == 0, "a timed-out waiter must not linger in the queue"
    held.release()
    (await s.acquire(job("after"))).release()


async def test_drain_fails_waiters_and_refuses_new_work():
    s = Scheduler(["w1"], wait_s=5)
    held = await s.acquire(job("h"))
    waiting = asyncio.ensure_future(s.acquire(job("w")))
    await asyncio.sleep(0)
    drain = asyncio.ensure_future(s.drain(timeout=1.0))
    with pytest.raises(Draining):
        await waiting
    with pytest.raises(Draining):
        await s.acquire(job("late"))
    held.release()
    assert await drain == 0


async def test_double_release_is_harmless():
    s = Scheduler(["w1"])
    lease = await s.acquire(job("a"))
    lease.release(); lease.release()
    assert s.inflight == 0 and len(s._free) == 1
