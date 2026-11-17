"""실시간 추가 알림 테스트: Redis 릴레이로 워커끼리 알림이 오가는지 확인한다.

워커 둘을 DatasetEvents 둘과 릴레이 둘로 흉내 낸다. 두 릴레이는 같은 가짜 Redis 서버를 쓴다.
"""

import asyncio

import fakeredis
import fakeredis.aioredis

from webchart.application.live import DataAppended, DatasetEvents, RedisEventRelay

EVENT = DataAppended("ds", 3, {"x": (10.0, 20.5)})


def test_event_round_trips_through_json():
    assert DataAppended.from_json(EVENT.to_json()) == EVENT


async def _worker(server):
    events = DatasetEvents()
    relay = RedisEventRelay(fakeredis.aioredis.FakeRedis(server=server), events)
    await relay.start()
    events.attach_relay(relay)
    return events, relay


async def test_append_on_one_worker_reaches_subscribers_on_every_worker():
    server = fakeredis.FakeServer()
    (events_a, relay_a), (events_b, relay_b) = await _worker(server), await _worker(server)
    queue_a = events_a.subscribe("ds")
    queue_b = events_b.subscribe("ds")
    other = events_b.subscribe("other-ds")
    try:
        events_a.publish(EVENT)  # 워커 A에서 추가가 일어났다
        assert await asyncio.wait_for(queue_b.get(), 2) == EVENT
        assert await asyncio.wait_for(queue_a.get(), 2) == EVENT  # 자기 구독자도 Redis로 받는다
        await asyncio.sleep(0.05)
        assert other.empty() and queue_a.empty()  # 다른 데이터셋에는 가지 않고, 중복도 없다
    finally:
        await relay_a.stop()
        await relay_b.stop()


async def test_publish_from_render_thread_is_delivered():
    """추가는 렌더 스레드에서 끝나므로 다른 스레드에서 발행해도 전달돼야 한다."""
    server = fakeredis.FakeServer()
    events, relay = await _worker(server)
    queue = events.subscribe("ds")
    try:
        await asyncio.to_thread(events.publish, EVENT)
        assert await asyncio.wait_for(queue.get(), 2) == EVENT
    finally:
        await relay.stop()


async def test_falls_back_to_local_delivery_when_redis_publish_fails():
    server = fakeredis.FakeServer()
    events, relay = await _worker(server)
    queue = events.subscribe("ds")

    async def broken_publish(channel, message):
        raise ConnectionError("redis down")

    relay._redis.publish = broken_publish
    try:
        events.publish(EVENT)
        assert await asyncio.wait_for(queue.get(), 2) == EVENT
    finally:
        await relay.stop()


async def test_without_relay_delivers_in_process():
    events = DatasetEvents()
    queue = events.subscribe("ds")
    events.publish(EVENT)
    assert await asyncio.wait_for(queue.get(), 1) == EVENT
