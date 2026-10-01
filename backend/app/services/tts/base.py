from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class Voice:
    id: str
    display_name: str
    language: str
    engine: str


@dataclass(frozen=True)
class AudioResult:
    samples: NDArray[np.float32]
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.samples) / self.sample_rate


@dataclass(frozen=True)
class EngineHealth:
    ready: bool
    message: str


class TTSError(Exception):
    """A safe, user-facing synthesis failure; never includes document text."""


class TTSUnavailable(TTSError):
    pass


class InvalidTTSInput(TTSError):
    pass


class TTSEngine(ABC):
    @abstractmethod
    def list_voices(self) -> tuple[Voice, ...]:
        raise NotImplementedError

    @abstractmethod
    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> AudioResult:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> EngineHealth:
        raise NotImplementedError
