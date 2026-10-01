"""LocalReader backend."""

import os

# Must precede any ONNX Runtime import: API opt-out alone happens too late.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
