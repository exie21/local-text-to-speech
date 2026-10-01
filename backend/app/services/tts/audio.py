"""Serialize engine-neutral audio results as browser-playable PCM WAV."""

from io import BytesIO
import wave

import numpy as np

from app.services.tts.base import AudioResult


def encode_wav(audio: AudioResult) -> bytes:
    pcm = (np.clip(audio.samples, -1.0, 1.0) * 32767).astype("<i2")
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(audio.sample_rate)
        wav.writeframes(pcm.tobytes())
    return output.getvalue()
