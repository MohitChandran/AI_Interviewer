import struct
import time

from backend.services.vad import VoiceActivityDetector


def make_vad(threshold=0.05):
    vad = VoiceActivityDetector(sample_rate=16000, frame_duration_ms=30, silence_threshold_seconds=threshold)
    return vad


def silent_frame(vad):
    return b"\x00" * vad.frame_size


def fake_is_speech(vad, pattern):
    """Replace webrtcvad's decision with a scripted sequence of booleans."""
    results = iter(pattern)
    vad.vad = type("FakeVad", (), {"is_speech": lambda self, frame, rate: next(results)})()


def test_frame_size_is_16bit_samples():
    vad = make_vad()
    assert vad.frame_size == 16000 * 30 // 1000 * 2


def test_wrong_frame_size_is_ignored():
    vad = make_vad()
    assert vad.process_frame(b"\x00" * 10) is False


def test_silence_before_any_speech_never_triggers():
    vad = make_vad(threshold=0.0)
    calls = []
    vad.set_silence_callback(lambda: calls.append(1))
    for _ in range(20):
        vad.process_frame(silent_frame(vad))
    assert calls == []


def test_silence_after_speech_triggers_callback_once_and_resets():
    vad = make_vad(threshold=0.02)
    calls = []
    vad.set_silence_callback(lambda: calls.append(1))
    fake_is_speech(vad, [True, True] + [False] * 50)

    vad.process_frame(silent_frame(vad))
    vad.process_frame(silent_frame(vad))
    assert vad.speech_detected

    for _ in range(50):
        vad.process_frame(silent_frame(vad))
        time.sleep(0.001)

    assert calls == [1]
    assert not vad.speech_detected


def test_real_vad_treats_zeros_as_silence():
    vad = make_vad()
    frame = struct.pack(f"<{vad.frame_size // 2}h", *([0] * (vad.frame_size // 2)))
    assert vad.process_frame(frame) is False
