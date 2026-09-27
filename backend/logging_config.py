import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

# Each asyncio task gets its own copy of the context, and tasks/threads started from
# inside it inherit the value, so setting this once per interview tags all its logs.
interview_id_var: ContextVar[str] = ContextVar("interview_id", default="-")

TEXT_FORMAT = "%(asctime)s %(levelname)-7s [%(interview_id)s] %(name)s: %(message)s"
NOISY_LOGGERS = ("httpx", "httpcore", "aiosqlite", "websockets", "alembic.runtime.migration")


@contextmanager
def interview_context(interview_id: str) -> Iterator[None]:
    token = interview_id_var.set(interview_id)
    try:
        yield
    finally:
        interview_id_var.reset(token)


class InterviewContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.interview_id = interview_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, the format log platforms (Loki, Datadog, CloudWatch) ingest."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "interview_id": getattr(record, "interview_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler()
    # Filters on a handler see records from every logger; filters on a logger would not
    # see records propagated up from child loggers.
    handler.addFilter(InterviewContextFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
