"""One lazy engine per application process, including concurrent first access."""

from threading import Lock

from app.config import Settings
from app.services.tts.base import TTSEngine

_lock = Lock()
_engine: TTSEngine | None = None


def get_tts_engine() -> TTSEngine:
    global _engine
    with _lock:
        if _engine is None:
            from app.services.tts.kokoro import KokoroEngine

            _engine = KokoroEngine(Settings())
        return _engine
