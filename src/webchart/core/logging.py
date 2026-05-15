import logging
import sys
from contextvars import ContextVar

# Per-request id, populated by RequestIdMiddleware. Empty string when outside
# a request (e.g. background tasks, startup logging).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Inject the current request_id into every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s | %(message)s"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger("webchart")
    root.setLevel(level)
    # Avoid duplicate handlers when setup is called twice (e.g. by TestClient)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("dask").setLevel(logging.WARNING)
