import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from backend.config import Settings, get_settings
from backend.db import repository
from backend.db.engine import SessionMaker, get_sessionmaker
from backend.db.models import EndReason
from backend.interview.evaluation import begin_evaluation
from backend.interview.recorder import InterviewRecorder
from backend.interview.session import InterviewSession
from backend.logging_config import interview_context

logger = logging.getLogger(__name__)
router = APIRouter()

RECEIVE_TIMEOUT_SECONDS = 60.0
MAX_QUEUED_CHUNKS = 100


class AudioProcessor:
    """Processes incoming audio off the receive loop so the socket never blocks.

    The bounded queue means a stalled consumer drops audio instead of growing memory forever.
    """

    def __init__(self, session: InterviewSession):
        self.session = session
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=MAX_QUEUED_CHUNKS)
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def add_chunk(self, chunk: bytes) -> None:
        try:
            self.queue.put_nowait(chunk)
        except asyncio.QueueFull:
            logger.warning("Audio queue full, dropping frame")

    async def _run(self) -> None:
        while self.session.interview_active:
            chunk = await self.queue.get()
            try:
                await self.session.process_audio_chunk(chunk)
            except Exception:
                logger.exception("Error processing audio chunk")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


async def _safe_send_json(websocket: WebSocket, data: dict) -> None:
    if websocket.application_state == WebSocketState.CONNECTED:
        with contextlib.suppress(Exception):
            await websocket.send_json(data)


@router.websocket("/ws/interviews/{interview_id}")
async def interview_websocket(
    websocket: WebSocket,
    interview_id: str,
    sessionmaker: SessionMaker = Depends(get_sessionmaker),
):
    await websocket.accept()
    with interview_context(interview_id):
        await _run_interview(websocket, interview_id, sessionmaker, get_settings())


async def _claim_interview(sessionmaker: SessionMaker, interview_id: str, settings: Settings):
    """Returns (interview, None) if this connection now owns the interview, else (None, error)."""
    async with sessionmaker() as db:
        interview = await repository.get_interview(db, interview_id)
        if interview is None:
            return None, "Interview not found"
        if repository.is_link_expired(interview, settings.interview_link_ttl_minutes):
            await repository.mark_expired(db, interview_id)
            return None, "This interview link has expired"
        claimed = await repository.start_interview(
            db,
            interview_id,
            llm_model=settings.groq_model,
            tts_voice=settings.elevenlabs_voice_id,
            stt_model=settings.deepgram_model,
        )
        if not claimed:
            return None, f"This interview can't be started (status: {interview.status.value})"
        return interview, None


async def _run_interview(
    websocket: WebSocket, interview_id: str, sessionmaker: SessionMaker, settings: Settings
) -> None:
    interview, error = await _claim_interview(sessionmaker, interview_id, settings)
    if error:
        logger.info("Rejected connection: %s", error)
        await _safe_send_json(websocket, {"type": "error", "message": error})
        await websocket.close()
        return

    logger.info("Interview started")
    recorder = InterviewRecorder(interview_id, sessionmaker)
    session: InterviewSession | None = None
    audio_processor: AudioProcessor | None = None
    end_reason = EndReason.ERROR
    try:
        session = InterviewSession(
            candidate_name=interview.candidate_name,
            role=interview.role,
            resume_data=interview.parsed_resume,
            settings=settings,
            recorder=recorder,
        )
        await session.start(websocket)

        audio_processor = AudioProcessor(session)
        audio_processor.start()

        loop_reason = await _receive_loop(websocket, session, audio_processor)
        # A time-limit ending is decided inside the session; it wins over how the socket closed.
        end_reason = session.end_reason or loop_reason
    except Exception as e:
        logger.exception("Interview failed")
        await recorder.event("error", error=repr(e))
        await _safe_send_json(websocket, {"type": "error", "message": f"Server error: {e}"})
    finally:
        if audio_processor:
            await audio_processor.stop()
        if session:
            await session.stop()
        await recorder.finish(end_reason)
        try:
            await begin_evaluation(interview_id, sessionmaker, settings)
        except Exception:
            logger.exception("Could not start the evaluation")
        with contextlib.suppress(Exception):
            await websocket.close()


async def _receive_loop(websocket: WebSocket, session: InterviewSession, audio_processor: AudioProcessor) -> EndReason:
    while session.interview_active:
        try:
            data = await asyncio.wait_for(websocket.receive(), timeout=RECEIVE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.info("Timed out waiting for client data")
            return EndReason.DISCONNECTED
        except (WebSocketDisconnect, RuntimeError) as e:
            logger.info("WebSocket closed: %r", e)
            return EndReason.DISCONNECTED

        if data.get("type") == "websocket.disconnect":
            logger.info("Client disconnected")
            return EndReason.DISCONNECTED

        if data.get("bytes") is not None:
            audio_processor.add_chunk(data["bytes"])
        elif data.get("text") is not None:
            message = json.loads(data["text"])
            if message.get("type") == "stop":
                return EndReason.CANDIDATE_ENDED
            if message.get("type") == "ai_audio_completed":
                session.on_ai_audio_completed(message.get("response_id"))
    return EndReason.DISCONNECTED
