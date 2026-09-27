import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable

from deepgram import Deepgram

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 5


class SpeechRecognizer:
    """Live speech-to-text over a Deepgram streaming connection."""

    def __init__(
        self,
        api_key: str,
        sample_rate: int = 16000,
        model: str = "nova-2",
        language: str = "en-US",
    ):
        self.deepgram = Deepgram(api_key)
        self.sample_rate = sample_rate
        self.model = model
        self.language = language
        self.connection = None
        self.transcript_callback: Callable[[str, bool], None] | None = None
        self.last_audio_time: float | None = None
        self.connection_health_task: asyncio.Task | None = None

    async def start_streaming(self, on_transcript: Callable[[str, bool], None]) -> None:
        self.transcript_callback = on_transcript

        for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
            try:
                self.connection = await self.deepgram.transcription.live(
                    {
                        "punctuate": True,
                        "interim_results": True,
                        "language": self.language,
                        "model": self.model,
                        "smart_format": True,
                        "encoding": "linear16",
                        "sample_rate": self.sample_rate,
                        "channels": 1,
                        "vad_events": True,
                    }
                )
                self.connection.registerHandler(
                    self.connection.event.CLOSE,
                    lambda _: logger.info("Deepgram connection closed"),
                )
                self.connection.registerHandler(
                    self.connection.event.TRANSCRIPT_RECEIVED,
                    self._on_message,
                )
                logger.info("Deepgram connection established")
                self.connection_health_task = asyncio.create_task(self._monitor_connection_health())
                return
            except Exception as e:
                logger.warning("Error starting Deepgram (attempt %d): %s", attempt, e)
                if attempt < MAX_CONNECT_ATTEMPTS:
                    await asyncio.sleep(2**attempt)

        raise RuntimeError("Max reconnect attempts reached for Deepgram")

    async def _monitor_connection_health(self) -> None:
        try:
            while self.connection is not None:
                await asyncio.sleep(5)
                if self.last_audio_time is not None:
                    idle = time.time() - self.last_audio_time
                    if idle > 30:
                        logger.warning("No audio for %.0fs, connection may be stale", idle)
                if self.connection and not hasattr(self.connection, "_socket"):
                    logger.warning("Deepgram socket lost")
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Connection health monitor error")

    def _on_message(self, message) -> None:
        try:
            self.last_audio_time = time.time()

            data = message
            if isinstance(message, (str, bytes)):
                try:
                    data = json.loads(message)
                except ValueError:
                    return
            if not isinstance(data, dict):
                return

            channel = data.get("channel")
            if not isinstance(channel, dict):
                return
            alternatives = channel.get("alternatives")
            if not isinstance(alternatives, list) or not alternatives:
                return
            first_alt = alternatives[0]
            if not isinstance(first_alt, dict):
                return

            transcript = first_alt.get("transcript", "")
            # Interim results are Deepgram's evolving guesses at the words so far ("I built",
            # "I built a chat"); a final result replaces them. Both are forwarded: interims are
            # good for noticing that someone started talking, only finals belong in the transcript.
            is_final = bool(data.get("is_final"))
            if transcript and self.transcript_callback:
                logger.debug("Transcript (%s): %s", "final" if is_final else "interim", transcript)
                self.transcript_callback(transcript, is_final)
        except Exception:
            logger.exception("Error processing transcript")

    async def send_audio(self, audio_data: bytes) -> None:
        if not self.connection:
            return
        try:
            self.connection.send(audio_data)
            self.last_audio_time = time.time()
        except Exception as e:
            logger.error("Error sending audio to Deepgram: %s", e)

    async def close(self) -> None:
        if self.connection_health_task:
            self.connection_health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.connection_health_task
            self.connection_health_task = None

        if self.connection:
            try:
                await self.connection.finish()
                logger.info("Deepgram connection closed gracefully")
            except Exception as e:
                logger.error("Error closing Deepgram: %s", e)
        self.connection = None
