"""데이터 변경 알림: 실시간 추가가 끝나면 그 데이터셋을 보는 WS 세션에 알린다 (계획 문서 4.3).

프로세스 안의 발행·구독이다. 추가는 렌더 스레드에서 일어나므로 발행은 스레드 안전하게
각 구독자의 이벤트 루프로 넘긴다. 워커를 여러 개 띄우면 워커끼리는 알림이 가지 않는다
(그때는 Redis pub/sub으로 바꾼다).
"""

import asyncio
import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DataAppended:
    dataset_id: str
    generation: int
    # x 컬럼 → 그림이 바뀐 닫힌 구간. 없는 컬럼은 전체가 바뀐 것으로 본다
    ranges: dict[str, tuple[float, float]]


class DatasetEvents:
    def __init__(self) -> None:
        self._subscribers: dict[
            str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[DataAppended]]]
        ] = {}
        self._lock = threading.Lock()

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

    def publish(self, event: DataAppended) -> None:
        """어느 스레드에서든 부를 수 있다. 큐가 가득 찬 구독자는 이번 알림을 건너뛴다."""
        with self._lock:
            subscribers = list(self._subscribers.get(event.dataset_id, ()))
        for loop, queue in subscribers:
            loop.call_soon_threadsafe(_offer, queue, event)


def _offer(queue: "asyncio.Queue[DataAppended]", event: DataAppended) -> None:
    if not queue.full():
        queue.put_nowait(event)


dataset_events = DatasetEvents()
