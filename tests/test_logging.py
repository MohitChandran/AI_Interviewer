import asyncio
import io
import json
import logging

from backend.logging_config import InterviewContextFilter, JsonFormatter, interview_context


def capture_logger(formatter: logging.Formatter):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(InterviewContextFilter())
    handler.setFormatter(formatter)
    logger = logging.getLogger("test.context")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


async def test_interview_id_follows_tasks_and_threads():
    logger, stream = capture_logger(logging.Formatter("%(interview_id)s %(message)s"))

    async def log_in_task():
        logger.info("in task")

    with interview_context("abc-123"):
        await asyncio.create_task(log_in_task())
        await asyncio.to_thread(logger.info, "in thread")
    logger.info("outside")

    assert stream.getvalue().splitlines() == ["abc-123 in task", "abc-123 in thread", "- outside"]


def test_json_formatter_emits_one_object_per_line():
    logger, stream = capture_logger(JsonFormatter())

    with interview_context("abc-123"):
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("failed")

    entry = json.loads(stream.getvalue())
    assert entry["interview_id"] == "abc-123"
    assert entry["level"] == "ERROR"
    assert entry["message"] == "failed"
    assert "ValueError: boom" in entry["exception"]
