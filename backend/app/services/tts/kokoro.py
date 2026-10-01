"""Keep Kokoro and ONNX-specific details behind the shared engine interface."""

import logging
import math
from threading import Lock

import numpy as np

from app.config import Settings
from app.services.tts.base import (
    AudioResult,
    EngineHealth,
    InvalidTTSInput,
    TTSEngine,
    TTSError,
    TTSUnavailable,
    Voice,
)

# Public identifiers are independent of the vendor's voice-bank names.
VOICE_CATALOG = (
    (Voice("heart", "Heart", "en-US", "kokoro"), "af_heart"),
    (Voice("bella", "Bella", "en-US", "kokoro"), "af_bella"),
    (Voice("nicole", "Nicole", "en-US", "kokoro"), "af_nicole"),
    (Voice("sarah", "Sarah", "en-US", "kokoro"), "af_sarah"),
    (Voice("sky", "Sky", "en-US", "kokoro"), "af_sky"),
    (Voice("adam", "Adam", "en-US", "kokoro"), "am_adam"),
    (Voice("michael", "Michael", "en-US", "kokoro"), "am_michael"),
    (Voice("emma", "Emma", "en-GB", "kokoro"), "bf_emma"),
    (Voice("george", "George", "en-GB", "kokoro"), "bm_george"),
    (Voice("fable", "Fable", "en-GB", "kokoro"), "bm_fable"),
)


class KokoroEngine(TTSEngine):
    def __init__(self, settings: Settings):
        self._model_path = settings.kokoro_model_path or settings.model_dir / "kokoro-v1.0.onnx"
        self._voices_path = settings.kokoro_voices_path or settings.model_dir / "voices-v1.0.bin"
        self._threads = settings.tts_threads
        self._lock = Lock()
        self._engine = None
        self._voices: tuple[Voice, ...] = ()

    def _load(self) -> None:
        """Caller holds the lock; publish an engine only after successful setup."""
        if self._engine is not None:
            return
        if not self._model_path.is_file() or not self._voices_path.is_file():
            raise TTSUnavailable("Kokoro model or voices are missing. Run the model setup command first.")

        try:
            import onnxruntime as ort
            from kokoro_onnx import Kokoro

            ort.disable_telemetry_events()
            # Vendor debug messages may include input phonemes. Keep them private.
            logging.getLogger("kokoro_onnx").disabled = True
            logging.getLogger("phonemizer").disabled = True
            options = ort.SessionOptions()
            options.intra_op_num_threads = self._threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(
                str(self._model_path), sess_options=options, providers=["CPUExecutionProvider"]
            )
            engine = Kokoro.from_session(session, str(self._voices_path))
            bank = engine.voices
            try:
                available = set(engine.get_voices())
                voices = tuple(voice for voice, vendor_id in VOICE_CATALOG if vendor_id in available)
                if not voices:
                    raise ValueError("No supported English voices")
                # np.load returns a lazy archive: cache selected voice arrays once
                # rather than decompressing a voice again on every synthesis call.
                engine.voices = {
                    vendor_id: bank[vendor_id]
                    for voice, vendor_id in VOICE_CATALOG
                    if voice in voices
                }
            finally:
                bank.close()
            self._voices = voices
            self._engine = engine
        except Exception:
            raise TTSUnavailable("Kokoro could not load. Verify the model files and installed dependencies.") from None

    def list_voices(self) -> tuple[Voice, ...]:
        # Once loaded, metadata is immutable and can be read without waiting
        # for a long synthesize call holding the engine lock. This keeps new
        # job submissions responsive while another job is generating audio.
        if self._engine is None:
            with self._lock:
                self._load()
        return self._voices

    def health(self) -> EngineHealth:
        try:
            self.list_voices()
        except TTSUnavailable as error:
            return EngineHealth(ready=False, message=str(error))
        return EngineHealth(ready=True, message="Kokoro is ready.")

    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> AudioResult:
        if not isinstance(text, str) or not text.strip():
            raise InvalidTTSInput("Enter some text to synthesize.")
        match = next(((item, vendor_id) for item, vendor_id in VOICE_CATALOG if item.id == voice), None)
        if match is None:
            raise InvalidTTSInput("Choose an available voice.")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
            raise InvalidTTSInput("Synthesis speed must be between 0.5 and 2.0.")

        selected, vendor_id = match
        with self._lock:
            self._load()
            if selected not in self._voices:
                raise InvalidTTSInput("That voice is not available in the installed voice bank.")
            try:
                samples, sample_rate = self._engine.create(
                    text.strip(), voice=vendor_id, speed=float(speed), lang=selected.language.lower()
                )
                samples = np.asarray(samples, dtype=np.float32)
                if samples.ndim != 1 or not len(samples) or not np.isfinite(samples).all() or sample_rate <= 0:
                    raise ValueError("Invalid synthesis output")
                return AudioResult(samples=samples, sample_rate=int(sample_rate))
            except Exception:
                raise TTSError("Speech generation failed. Try a short passage of readable text.") from None
