"""Generate a short local WAV without depending on the HTTP API or job system."""

import argparse
from pathlib import Path
import sys
from uuid import uuid4
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import numpy as np

from app.config import Settings
from app.services.tts.base import TTSError
from app.services.tts.registry import get_tts_engine


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", help="Short text to speak locally")
    parser.add_argument("--voice", default="heart", help="Public voice ID (default: heart)")
    parser.add_argument("--speed", type=float, default=1.0, help="Synthesis speed, 0.5-2.0")
    parser.add_argument("--output", default=None, help="WAV filename inside TEMP_DIR; never overwritten")
    args = parser.parse_args()
    filename = args.output or f"speech-{uuid4().hex}.wav"
    if Path(filename).name != filename or not filename.lower().endswith(".wav"):
        parser.error("--output must be a .wav filename without a directory")

    output = Settings().temp_dir / filename
    if output.exists():
        parser.error("Output already exists; choose another filename")
    audio = get_tts_engine().synthesize(args.text, args.voice, args.speed)
    output.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio.samples, -1.0, 1.0) * 32767).astype("<i2")
    with output.open("xb") as destination:
        with wave.open(destination, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(audio.sample_rate)
            wav.writeframes(pcm.tobytes())
    print(f"Created {output.name}: {audio.duration:.2f}s, {audio.sample_rate} Hz, mono PCM WAV")
    print("Stored in TEMP_DIR. This developer command does not yet provide automatic expiration.")


if __name__ == "__main__":
    try:
        main()
    except (TTSError, OSError) as error:
        print(f"Synthesis failed: {error}", file=sys.stderr)
        sys.exit(1)
