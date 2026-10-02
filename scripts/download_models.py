"""Explicitly fetch the versioned Kokoro assets; never runs during app startup."""

import hashlib
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import Settings

RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
ASSETS = {
    "kokoro-v1.0.onnx": (325_532_387, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    "voices-v1.0.bin": (28_214_398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}


def verify(path: Path, size: int, expected_hash: str | None) -> str:
    if path.stat().st_size != size:
        raise ValueError(f"{path.name} has an unexpected size; move it aside before downloading again")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError(f"{path.name} failed checksum verification; move it aside before downloading again")
    return digest


def main() -> None:
    directory = Settings().model_dir
    directory.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in ASSETS.items():
        destination = directory / name
        if destination.exists():
            print(f"Already present: {name} sha256={verify(destination, size, digest)}", flush=True)
            continue

        partial = destination.with_suffix(destination.suffix + ".part")
        # Exclusive creation also prevents two setup commands sharing a partial file.
        with partial.open("xb") as output:
            try:
                print(f"Downloading {name} ({size / 1_000_000:.1f} MB)…", flush=True)
                request = Request(f"{RELEASE_URL}/{name}", headers={"User-Agent": "EdSpeech-setup"})
                with urlopen(request, timeout=30) as response:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                output.flush()
                actual_hash = verify(partial, size, digest)
                # Publish atomically without overwriting a concurrently created file.
                os.link(partial, destination)
                print(f"Ready: {name} sha256={actual_hash}", flush=True)
            finally:
                partial.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        print(f"Model setup failed: {error}", file=sys.stderr)
        sys.exit(1)
