"""데이터 변경 알림: 실시간 추가가 끝나면 그 데이터셋을 보는 WS 세션에 알린다 (계획 문서 4.3).

구조
- DatasetEvents: 프로세스 안의 구독자 목록. 추가는 렌더 스레드에서 일어나므로 전달은
  스레드 안전하게 각 구독자의 이벤트 루프로 넘긴다.
- RedisEventRelay: 워커가 여러 개일 때 워커끼리 알림을 나눈다. 발행은 Redis 채널로만
  보내고, 모든 워커(발행한 워커 포함)가 채널에서 받아 자기 구독자에게 전달한다.
  그래서 한 워커에서 일어난 추가를 다른 워커에 붙은 WS 세션도 받는다.

릴레이가 없거나 Redis로 보내지 못하면 프로세스 안에서 바로 전달한다 (워커 하나면 충분하다).
"""

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("webchart.application.live")


@dataclass(frozen=True, slots=True)
class DataAppended:
    dataset_id: str
    generation: int
    # x 컬럼 → 그림이 바뀐 닫힌 구간. 없는 컬럼은 전체가 바뀐 것으로 본다
    ranges: dict[str, tuple[float, float]]

    def to_json(self) -> str:
        return json.dumps(
            {"dataset_id": self.dataset_id, "generation": self.generation, "ranges": self.ranges}
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "DataAppended":
        data = json.loads(raw)
        ranges = {name: (float(low), float(high)) for name, (low, high) in data["ranges"].items()}
        return cls(str(data["dataset_id"]), int(data["generation"]), ranges)


class EventRelay(Protocol):
    def send(self, event: DataAppended) -> None: ...


class DatasetEvents:
    def __init__(self) -> None:
        self._subscribers: dict[
            str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[DataAppended]]]
        ] = {}
        self._lock = threading.Lock()
        self._relay: EventRelay | None = None

    def subscribe(self, dataset_id: str) -> "asyncio.Queue[DataAppended]":
        queue: asyncio.Queue[DataAppended] = asyncio.Queue(maxsize=64)
        with self._lock:
            self._subscribers.setdefault(dataset_id, set()).add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, dataset_id: str, queue: "asyncio.Queue[DataAppended]") -> None:
        with self._lock:
            subscribers = self._subscribers.get(dataset_id, set())
            for entry in [entry for entry in subscribers if entry[1] is queue]:
                subscribers.discard(entry)
            if not subscribers:
                self._subscribers.pop(dataset_id, None)

    def attach_relay(self, relay: EventRelay | None) -> None:
        """발행을 relay로 보낸다 (None이면 프로세스 안에서만 전달)."""
        self._relay = relay

    def publish(self, event: DataAppended) -> None:
        """어느 스레드에서든 부를 수 있다. 릴레이가 있으면 모든 워커로, 없으면 이 프로세스로."""
        relay = self._relay
        if relay is not None:
            relay.send(event)
        else:
            self.deliver(event)

    def deliver(self, event: DataAppended) -> None:
        """이 프로세스의 구독자에게 전달한다. 큐가 가득 찬 구독자는 이번 알림을 건너뛴다."""
        with self._lock:
            subscribers = list(self._subscribers.get(event.dataset_id, ()))
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(_offer, queue, event)


def _offer(queue: "asyncio.Queue[DataAppended]", event: DataAppended) -> None:
    if not queue.full():
        queue.put_nowait(event)


class RedisEventRelay:
    """Redis pub/sub으로 워커끼리 알림을 나눈다.

    send()는 렌더 스레드에서도 불리므로 발행을 릴레이의 이벤트 루프로 넘긴다. 발행에
    실패하면 적어도 이 워커의 구독자에게는 전달한다. 구독 연결이 끊기면 잠시 뒤 다시
    구독한다 (끊긴 동안의 알림은 잃는다. 뷰어는 다음 viewport 요청에서 새 데이터를 받는다).
    """

    CHANNEL = "webchart:events:data-appended"
    RETRY_DELAY_S = 1.0

    def __init__(self, redis: Any, events: DatasetEvents) -> None:
        self._redis = redis
        self._events = events
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener: asyncio.Task[None] | None = None
        self._subscribed = asyncio.Event()

    async def start(self) -> None:
        """채널 구독을 시작한다. 첫 구독이 끝난 뒤에 돌아온다."""
        self._loop = asyncio.get_running_loop()
        self._listener = asyncio.create_task(self._listen())
        await self._subscribed.wait()

    async def stop(self) -> None:
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
            self._listener = None

    def send(self, event: DataAppended) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            self._events.deliver(event)
            return
        asyncio.run_coroutine_threadsafe(self._publish(event), loop)

    async def _publish(self, event: DataAppended) -> None:
        try:
            await self._redis.publish(self.CHANNEL, event.to_json())
        except Exception as error:
            logger.warning(f"추가 알림을 Redis로 보내지 못해 이 워커에만 전달한다: {error}")
            self._events.deliver(event)

    async def _listen(self) -> None:
        while True:
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(self.CHANNEL)
                self._subscribed.set()
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        self._events.deliver(DataAppended.from_json(message["data"]))
                    except (KeyError, TypeError, ValueError) as error:
                        logger.warning(f"알 수 없는 추가 알림을 버린다: {error}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"추가 알림 구독이 끊겼다. 다시 구독한다: {error}")
                self._subscribed.set()  # start()가 Redis 장애로 멈추지 않게 한다
            finally:
                await _close_pubsub(pubsub)
            await asyncio.sleep(self.RETRY_DELAY_S)


async def _close_pubsub(pubsub: Any) -> None:
    closer = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:
        pass


dataset_events = DatasetEvents()
