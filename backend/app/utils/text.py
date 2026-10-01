"""Normalize plain text and split it into ordered, speech-friendly chunks."""

from __future__ import annotations

import re
import unicodedata

from app.config import Settings


class TextProcessingError(ValueError):
    """Input cannot be processed into speech text."""


class EmptyTextError(TextProcessingError):
    """Input is empty after whitespace normalization."""


class TextTooLongError(TextProcessingError):
    """Input exceeds the caller's configured character limit."""


_SENTENCE_END = re.compile(r"([.!?…]+[\"'”’\)\]]*)(?=\s|$)")
_CLOSING_QUOTES = "\"'”’)]}»"
_ABBREVIATIONS = {
    "mr", "mrs", "ms", "mx", "dr", "prof", "rev", "hon", "sr", "jr",
    "st", "vs", "etc", "e.g", "i.e", "a.m", "p.m", "cf", "approx",
}


def normalize_text(text: str) -> str:
    """Normalize Unicode and whitespace while retaining paragraph boundaries.

    Single line breaks within a paragraph are treated as layout wraps and joined
    with spaces. One or more blank lines separate paragraphs in the result.
    """
    if not isinstance(text, str):
        raise TypeError("Text input must be a string.")

    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    paragraphs: list[str] = []
    lines: list[str] = []

    def finish_paragraph() -> None:
        if lines:
            paragraphs.append(" ".join(lines))
            lines.clear()

    for raw_line in text.split("\n"):
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
        else:
            finish_paragraph()
    finish_paragraph()

    normalized = "\n\n".join(paragraphs)
    if not normalized:
        raise EmptyTextError("Enter some text to process.")
    return normalized


def split_sentences(paragraph: str) -> list[str]:
    """Split one normalized paragraph at likely sentence endings.

    Common titles, initials, decimal numbers, and dotted abbreviations are kept
    with the following words. This is deliberately a small dependency-free
    segmenter; it does not attempt linguistic sentence classification.
    """
    if not isinstance(paragraph, str):
        raise TypeError("Paragraph input must be a string.")
    paragraph = " ".join(paragraph.split())
    if not paragraph:
        return []

    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(paragraph):
        ending = match.group(1)
        if "." in ending and _is_abbreviation(paragraph[start:match.end()]):
            continue
        sentence = paragraph[start:match.end()].strip()
        if sentence:
            sentences.append(sentence)
        start = match.end()

    remainder = paragraph[start:].strip()
    if remainder:
        sentences.append(remainder)
    return sentences


def chunk_text(
    text: str,
    *,
    chunk_size: int | None = None,
    max_chars: int | None = None,
) -> list[str]:
    """Normalize text, then pack sentences into ordered chunks.

    Sentences stay intact when they fit. Oversized sentences wrap at spaces;
    an individual long word stays intact even if it exceeds ``chunk_size``.
    Omitted limits use ``TTS_CHUNK_SIZE`` and ``MAX_TEXT_CHARS`` settings.
    ``max_chars`` applies to the source text before normalization.
    """
    if not isinstance(text, str):
        raise TypeError("Text input must be a string.")
    settings = Settings()
    chunk_size = settings.tts_chunk_size if chunk_size is None else chunk_size
    max_chars = settings.max_text_chars if max_chars is None else max_chars
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 500 <= chunk_size <= 1000:
        raise ValueError("Chunk size must be between 500 and 1000 characters.")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 1_000_000:
        raise ValueError("Maximum text length must be between 1 and 1000000 characters.")
    if len(text) > max_chars:
        raise TextTooLongError(f"Text exceeds the configured limit of {max_chars} characters.")

    normalized = normalize_text(text)
    units: list[tuple[str, bool]] = []
    for paragraph_index, paragraph in enumerate(normalized.split("\n\n")):
        sentences = split_sentences(paragraph)
        for sentence_index, sentence in enumerate(sentences):
            paragraph_break = paragraph_index > 0 and sentence_index == 0
            if len(sentence) > chunk_size:
                fragments = _wrap_at_words(sentence, chunk_size)
                units.extend((fragment, paragraph_break if index == 0 else False)
                             for index, fragment in enumerate(fragments))
            else:
                units.append((sentence, paragraph_break))

    chunks: list[str] = []
    current = ""
    for unit, paragraph_break in units:
        separator = "\n\n" if paragraph_break else " "
        candidate = f"{current}{separator}{unit}" if current else unit
        if current and len(candidate) > chunk_size:
            chunks.append(current)
            current = unit
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _is_abbreviation(sentence_tail: str) -> bool:
    token = sentence_tail.rsplit(None, 1)[-1].lstrip("([{“‘")
    token = token.rstrip(_CLOSING_QUOTES)
    if token.count(".") == 0:
        return False
    letters = token.rstrip(".")
    if letters.lower() in _ABBREVIATIONS:
        return True
    return bool(re.fullmatch(r"(?:[A-Za-z]\.){1,4}", token))


def _wrap_at_words(sentence: str, chunk_size: int) -> list[str]:
    fragments: list[str] = []
    current = ""
    for word in sentence.split():
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > chunk_size:
            fragments.append(current)
            current = word
        else:
            current = candidate
    if current:
        fragments.append(current)
    return fragments
