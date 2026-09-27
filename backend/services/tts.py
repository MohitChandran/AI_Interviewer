import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.elevenlabs.io"
# httpx drops idle connections after 5 s by default, and a candidate's answer is longer than
# that, so every reply would pay a fresh TLS handshake (~400 ms from far regions).
KEEPALIVE_SECONDS = 300
VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}


class VoiceSynthesizer:
    """Text-to-speech via the ElevenLabs REST API, over one reused connection. Produces MP3 bytes."""

    def __init__(self, api_key: str, voice_id: str, model: str):
        self.voice_id = voice_id
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"xi-api-key": api_key},
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(keepalive_expiry=KEEPALIVE_SECONDS),
        )

    async def synthesize(self, text: str) -> bytes | None:
        """Returns MP3 bytes, or None if synthesis failed."""
        try:
            response = await self._client.post(
                f"/v1/text-to-speech/{self.voice_id}",
                params={"output_format": "mp3_44100_128"},
                json={"text": text, "model_id": self.model, "voice_settings": VOICE_SETTINGS},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.error("TTS failed with %s: %s", e.response.status_code, e.response.text[:200])
            return None
        except httpx.HTTPError:
            logger.exception("TTS request failed")
            return None

        if not response.content:
            logger.warning("TTS returned empty audio for: %.50s", text)
            return None
        logger.debug("TTS generated %d bytes for %d chars", len(response.content), len(text))
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()
