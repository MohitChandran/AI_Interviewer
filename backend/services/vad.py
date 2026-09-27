import logging
import time
from collections.abc import Callable

import webrtcvad

logger = logging.getLogger(__name__)


class VoiceActivityDetector:
    """Detects end-of-utterance silence in 16-bit mono PCM using WebRTC VAD."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 30,
        silence_threshold_seconds: float = 1.0,
        vad_mode: int = 3,
    ):
        self.vad = webrtcvad.Vad(vad_mode)
        self.sample_rate = sample_rate
        self.frame_duration_ms = frame_duration_ms
        self.silence_threshold_seconds = silence_threshold_seconds
        self.frame_size = int(sample_rate * frame_duration_ms / 1000) * 2

        self.silence_padding_frames = 2
        self.speech_padding_frames = 2
        self.on_silence_detected: Callable[[], None] | None = None
        self.reset()

    def set_silence_callback(self, callback: Callable[[], None]) -> None:
        self.on_silence_detected = callback

    def reset(self) -> None:
        self.is_speaking = False
        self.silence_start: float | None = None
        self.speech_detected = False
        self.consecutive_silence = 0
        self.consecutive_speech = 0

    def process_frame(self, frame: bytes) -> bool:
        """Feed one frame; returns True if it contains speech."""
        if len(frame) != self.frame_size:
            logger.warning("Incorrect frame size %d, skipping", len(frame))
            return False
        try:
            if self.vad.is_speech(frame, self.sample_rate):
                self._on_speech_frame()
                return True
            self._on_silence_frame()
            return False
        except Exception:
            logger.exception("VAD error")
            return False

    def _on_speech_frame(self) -> None:
        self.consecutive_speech += 1
        self.consecutive_silence = 0
        if self.consecutive_speech >= self.speech_padding_frames:
            self.is_speaking = True
            self.speech_detected = True
            if self.silence_start is not None:
                logger.debug("Speech resumed, silence interrupted")
            self.silence_start = None

    def _on_silence_frame(self) -> None:
        self.consecutive_silence += 1
        self.consecutive_speech = 0
        # Silence only counts once the candidate has actually started speaking.
        if not self.speech_detected:
            return
        if self.consecutive_silence == self.silence_padding_frames and self.silence_start is None:
            self.silence_start = time.time()
            logger.debug("Silence started")
        if self.silence_start is None:
            return

        silence_duration = time.time() - self.silence_start
        if silence_duration >= self.silence_threshold_seconds:
            logger.debug("Silence threshold reached (%.2fs)", silence_duration)
            if self.on_silence_detected:
                self.on_silence_detected()
            self.reset()
