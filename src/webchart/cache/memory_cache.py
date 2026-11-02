"""프로세스 내 L1 캐시: 바이트 상한 LRU (계획 문서 2.8, 4.1).

Redis(L2)보다 앞에 둔다. 같은 워커가 방금 그린 타일을 다시 줄 때 네트워크 왕복과
직렬화를 건너뛴다. 큰 RAM을 가진 서버에서 상한을 넉넉히 잡을수록 적중률이 오른다.
"""

import threading
from collections import OrderedDict


class ByteLRU:
    """값 바이트 수의 합이 capacity_bytes를 넘지 않게 오래된 것부터 버리는 캐시."""

    def __init__(self, capacity_bytes: int) -> None:
        self.capacity_bytes = capacity_bytes
        self._entries: OrderedDict[str, bytes] = OrderedDict()
        self._size_bytes = 0
        self._lock = threading.Lock()

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        if len(value) > self.capacity_bytes:
            return  # 한 항목이 전체 상한보다 크면 넣지 않는다
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._size_bytes -= len(previous)
            self._entries[key] = value
            self._size_bytes += len(value)
            while self._size_bytes > self.capacity_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._size_bytes -= len(evicted)

    def discard_prefix(self, prefix: str) -> int:
        """키가 prefix로 시작하는 항목을 지운다 (데이터셋 삭제 등). 지운 개수를 반환."""
        with self._lock:
            doomed = [key for key in self._entries if key.startswith(prefix)]
            for key in doomed:
                self._size_bytes -= len(self._entries.pop(key))
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._size_bytes = 0
