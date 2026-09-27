from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # API credentials
    groq_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""

    # LLM (Groq)
    groq_model: str = "openai/gpt-oss-120b"
    # Reasoning models spend max_tokens on hidden reasoning; "low" keeps replies from truncating.
    # Set to an empty string for non-reasoning models.
    llm_reasoning_effort: str = "low"
    llm_max_tokens: int = 150
    llm_temperature: float = 0.7
    llm_history_messages: int = 12

    # Post-interview evaluation. No one is waiting on a live call, so it can reason more.
    evaluation_model: str = "openai/gpt-oss-120b"
    evaluation_reasoning_effort: str = "medium"
    evaluation_max_tokens: int = 8000

    # TTS (ElevenLabs)
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    elevenlabs_model: str = "eleven_flash_v2_5"

    # STT (Deepgram)
    deepgram_model: str = "nova-2"
    deepgram_language: str = "en-US"

    # Interview
    interview_duration_minutes: int = 10
    # How long the candidate must be quiet before their turn ends. People pause mid-sentence
    # for 0.5-1s, so shorter values make the interviewer cut in.
    silence_threshold_seconds: float = 1.2
    # Words the candidate must say while the AI is talking to count as an interruption;
    # filters out coughs, "mm-hm" and speaker echo.
    barge_in_min_words: int = 2

    # Audio / VAD
    sample_rate: int = 16000
    frame_duration_ms: int = 30
    vad_mode: int = 3

    # Uploads
    max_resume_mb: int = 5

    # Interviews not started within this window are marked expired.
    interview_link_ttl_minutes: int = 30

    # Database
    database_url: str = f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'interview_bot.db'}"
    database_echo: bool = False

    # Logging
    log_level: str = "INFO"
    log_format: str = "text"  # "text" for humans, "json" for log aggregators

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    upload_dir: Path = PROJECT_ROOT / "uploads"
    frontend_dir: Path = PROJECT_ROOT / "frontend"


@lru_cache
def get_settings() -> Settings:
    return Settings()
