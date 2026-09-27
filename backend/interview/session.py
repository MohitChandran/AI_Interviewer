import asyncio
import base64
import enum
import logging
import re
import time

from backend.config import Settings
from backend.db.models import EndReason, LLMPurpose, Speaker
from backend.interview.recorder import InterviewRecorder
from backend.interview.timer import InterviewTimer
from backend.services.llm import InterviewerLLM
from backend.services.stt import SpeechRecognizer
from backend.services.tts import VoiceSynthesizer
from backend.services.vad import VoiceActivityDetector

logger = logging.getLogger(__name__)

# After the candidate goes quiet, how long to wait for Deepgram to finalize their last words.
FINAL_TRANSCRIPT_WAIT_SECONDS = 1.0


class TurnState(str, enum.Enum):
    LISTENING = "listening"  # candidate's turn: the VAD watches for the end of their speech
    THINKING = "thinking"  # generating a reply: new speech is kept for the candidate's next turn
    SPEAKING = "speaking"  # reply audio is playing: enough speech counts as an interruption


def _b64(audio: bytes | None) -> str:
    return base64.b64encode(audio).decode("utf-8") if audio else ""


def split_sentences(text: str) -> list[str]:
    """Splits a reply into sentences so the first can be voiced while the rest are synthesized."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip())]
    return [s for s in sentences if s] or [text.strip()]


class InterviewSession:
    """Drives one live interview: STT -> silence detection -> LLM -> TTS -> client."""

    def __init__(
        self,
        candidate_name: str,
        role: str,
        resume_data: dict,
        settings: Settings,
        recorder: InterviewRecorder,
    ):
        self.recorder = recorder
        self.candidate_name = candidate_name
        self.role = role
        self.resume_data = resume_data

        self.llm = InterviewerLLM(
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            history_messages=settings.llm_history_messages,
            reasoning_effort=settings.llm_reasoning_effort,
        )
        self.tts = VoiceSynthesizer(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_voice_id,
            model=settings.elevenlabs_model,
        )
        self.stt = SpeechRecognizer(
            api_key=settings.deepgram_api_key,
            sample_rate=settings.sample_rate,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
        )
        self.vad = VoiceActivityDetector(
            sample_rate=settings.sample_rate,
            frame_duration_ms=settings.frame_duration_ms,
            silence_threshold_seconds=settings.silence_threshold_seconds,
            vad_mode=settings.vad_mode,
        )
        self.vad.set_silence_callback(self._on_silence_detected)
        self.timer = InterviewTimer(settings.interview_duration_minutes)
        self.barge_in_min_words = settings.barge_in_min_words

        self.conversation_history: list[dict] = []
        self.state = TurnState.LISTENING
        self.interview_active = False
        self.audio_buffer = b""

        # The candidate's current turn: finalized transcript segments, plus Deepgram's latest
        # guess at the words after them. Interims are replaced, never appended.
        self._final_parts: list[str] = []
        self._interim = ""
        self._final_arrived = asyncio.Event()

        # Each reply sent to the client gets an ID, so an "audio finished" message about an
        # older reply can't end the current one.
        self._response_id = 0
        self.websocket = None
        self.end_reason: EndReason | None = None

    async def start(self, websocket) -> None:
        """Connects STT and speaks the greeting."""
        self.websocket = websocket
        self.interview_active = True
        self.timer.start()

        try:
            await self.stt.start_streaming(self._on_transcript_received)
        except Exception as e:
            logger.error("Deepgram connection failed: %s", e)
            await self.recorder.event("stt_failed", error=repr(e))
            await self._send({"type": "error", "message": f"Audio input connection failed: {e}"})
            raise
        await self.recorder.event("stt_connected")

        greeting = await asyncio.to_thread(self.llm.start_interview, self.candidate_name, self.role, self.resume_data)
        await self.recorder.llm_call(LLMPurpose.GREETING, greeting)
        if await self._speak(greeting.text) is None:
            logger.error("Greeting audio synthesis failed, trying fallback")
            await self.recorder.event("tts_failed", stage="greeting")
            fallback = f"Hi {self.candidate_name}, let's begin the interview. Are you ready?"
            if await self._speak(fallback) is None:
                raise RuntimeError("TTS service not responding")

    async def process_audio_chunk(self, audio_data: bytes) -> None:
        if not self.interview_active:
            return
        if self.timer.is_expired():
            await self._end_interview()
            return

        # Always stream to STT so the candidate can interrupt the AI.
        await self.stt.send_audio(audio_data)

        # Only look for the end of the candidate's turn while it is their turn.
        if self.state != TurnState.LISTENING:
            return

        self.audio_buffer += audio_data
        frame_size = self.vad.frame_size
        while len(self.audio_buffer) >= frame_size:
            frame, self.audio_buffer = self.audio_buffer[:frame_size], self.audio_buffer[frame_size:]
            self.vad.process_frame(frame)

    def on_ai_audio_completed(self, response_id: int | None) -> None:
        if self.state == TurnState.SPEAKING and response_id == self._response_id:
            self._listen()
            logger.info("Client finished playing reply %d, listening", response_id)
        else:
            logger.info("Ignoring audio-finished for reply %s (state=%s)", response_id, self.state.value)

    async def stop(self) -> None:
        self.interview_active = False
        await self.stt.close()
        await self.tts.aclose()

    async def _send(self, payload: dict) -> bool:
        if not self.websocket:
            return False
        try:
            await self.websocket.send_json(payload)
            return True
        except Exception as e:
            logger.warning("Failed to send payload to client: %s", e)
            return False

    def _listen(self) -> None:
        self.state = TurnState.LISTENING
        self.vad.reset()
        self.audio_buffer = b""

    def _pending_speech(self) -> str:
        parts = self._final_parts + ([self._interim] if self._interim else [])
        return " ".join(parts).strip()

    def _on_transcript_received(self, transcript: str, is_final: bool) -> None:
        if self.state == TurnState.SPEAKING:
            if len(transcript.split()) < self.barge_in_min_words:
                return
            self._interrupt(transcript)

        if is_final:
            self._final_parts.append(transcript.strip())
            self._interim = ""
            self._final_arrived.set()
        else:
            self._interim = transcript.strip()

    def _interrupt(self, transcript: str) -> None:
        logger.info("Candidate interrupted reply %d: %s", self._response_id, transcript)
        self._listen()
        asyncio.create_task(self._record_interruption(transcript))

    async def _record_interruption(self, transcript: str) -> None:
        await self._send({"type": "stop_ai_audio", "response_id": self._response_id})
        await self.recorder.mark_last_interviewer_turn_interrupted()
        await self.recorder.event("candidate_interrupted", transcript=transcript)

    def _on_silence_detected(self) -> None:
        # Changing state here, before the task starts, is what stops a second silence
        # from starting a second reply.
        if self.state == TurnState.LISTENING and self._pending_speech():
            self.state = TurnState.THINKING
            asyncio.create_task(self._respond())

    async def _take_candidate_turn(self) -> str:
        """Collects the candidate's turn, giving Deepgram a moment to finalize the last words."""
        if self._interim:
            self._final_arrived.clear()
            try:
                await asyncio.wait_for(self._final_arrived.wait(), FINAL_TRANSCRIPT_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.info("No final transcript for %r, using the interim text", self._interim)
        text = self._pending_speech()
        self._final_parts, self._interim = [], ""
        return text

    async def _respond(self) -> None:
        started = time.perf_counter()
        try:
            user_text = await self._take_candidate_turn()
            if not user_text:
                self._listen()
                return
            logger.info("Candidate said: %s", user_text)
            await self._send({"type": "candidate_transcript", "text": user_text})
            self.conversation_history.append({"role": "candidate", "text": user_text})
            await self.recorder.add_turn(Speaker.CANDIDATE, user_text)

            result = await asyncio.to_thread(
                self.llm.generate_response, user_text, self.conversation_history, self.resume_data
            )
            await self.recorder.llm_call(LLMPurpose.RESPONSE, result)

            first_audio_at = await self._speak(result.text)
            if first_audio_at is None:
                await self.recorder.event("tts_failed", stage="response")
                first_audio_at = await self._speak(
                    "I apologize, there was an issue generating audio. Could you please repeat your answer?"
                )
                if first_audio_at is None:
                    logger.error("Fallback audio synthesis also failed")
                    self._listen()
                    return

            # Latency breakdown, measured from the moment the candidate's turn ended.
            first_audio_ms = int((first_audio_at - started) * 1000)
            logger.info("First audio after %d ms (LLM %d ms)", first_audio_ms, result.latency_ms)
            await self.recorder.event(
                "response_sent",
                llm_ms=result.latency_ms,
                first_audio_ms=first_audio_ms,
                total_ms=_elapsed_ms(started),
            )
        except Exception:
            logger.exception("Error during AI response generation")
            self._listen()

    async def _speak(self, text: str) -> float | None:
        """Voices `text` as one reply, sentence by sentence.

        The first sentence is synthesized and sent on its own, so the candidate hears it
        without waiting for the whole reply; later sentences are synthesized while earlier
        ones play. Returns when the first audio was sent (perf_counter), or None if the
        first sentence couldn't be synthesized.
        """
        sentences = split_sentences(text)
        first_audio = await self.tts.synthesize(sentences[0])
        if first_audio is None:
            return None

        self._response_id += 1
        response_id = self._response_id
        self.conversation_history.append({"role": "interviewer", "text": text})
        await self.recorder.add_turn(Speaker.INTERVIEWER, text)

        # The text goes out first so it can be shown; "chunks" tells the client how many
        # audio parts to expect before this reply counts as finished.
        header = {"type": "ai_response", "response_id": response_id, "text": text, "chunks": len(sentences)}
        if not await self._send(header):
            self._listen()
            return None
        self.state = TurnState.SPEAKING
        await self._send_audio_chunk(response_id, 0, first_audio)
        first_audio_at = time.perf_counter()

        for index, sentence in enumerate(sentences[1:], start=1):
            audio = await self.tts.synthesize(sentence)
            if self.state != TurnState.SPEAKING or self._response_id != response_id:
                break  # the candidate interrupted; don't send the rest
            if audio is None:
                await self.recorder.event("tts_failed", stage="sentence", index=index)
            # An empty chunk still counts, so the client's chunk total stays correct.
            await self._send_audio_chunk(response_id, index, audio)
        return first_audio_at

    async def _send_audio_chunk(self, response_id: int, index: int, audio: bytes | None) -> None:
        await self._send({"type": "ai_audio", "response_id": response_id, "index": index, "audio": _b64(audio)})

    async def _end_interview(self) -> None:
        if not self.interview_active:
            return
        self.interview_active = False
        self.end_reason = EndReason.TIME_LIMIT

        closing = await asyncio.to_thread(self.llm.generate_closing, self.candidate_name)
        await self.recorder.llm_call(LLMPurpose.CLOSING, closing)
        closing_text = closing.text
        closing_audio = await self.tts.synthesize(closing_text)
        if closing_audio is None:
            await self.recorder.event("tts_failed", stage="closing")
            closing_text = f"Thank you {self.candidate_name}, the interview is complete. Goodbye!"
            closing_audio = await self.tts.synthesize(closing_text)

        await self.recorder.add_turn(Speaker.INTERVIEWER, closing_text)
        await self._send({"type": "interview_end", "text": closing_text, "audio": _b64(closing_audio)})
        await self.stt.close()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
