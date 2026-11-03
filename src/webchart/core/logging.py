"""로깅 설정. 모든 로그 줄에 요청 ID(`rid=`)를 붙인다."""

import logging
import sys
from contextvars import ContextVar

# 요청별 ID. RequestIdMiddleware가 채운다. 요청 밖(백그라운드 작업, 시작 로그)에서는 "-"다.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """현재 request_id를 모든 LogRecord에 넣는다."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(debug: bool = False) -> None:
    """`webchart` 로거에 stdout 핸들러를 단다. 다시 호출하면 기존 핸들러를 교체한다."""
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s | %(message)s"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger("webchart")
    root.setLevel(level)
    # TestClient 등에서 두 번 호출돼도 핸들러가 중복되지 않게 한다.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)

    # 로그가 많은 라이브러리는 WARNING 이상만 남긴다.
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("dask").setLevel(logging.WARNING)
