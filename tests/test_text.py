import pytest

from app.utils.text import (
    EmptyTextError,
    TextTooLongError,
    chunk_text,
    normalize_text,
    split_sentences,
)


def test_normalize_unicode_whitespace_and_preserve_paragraphs():
    source = "  Cafe\u0301\t	with   space.\r\nwrapped line.\r\n\r\n\nSecond\u00a0paragraph.  "
    assert normalize_text(source) == "Café with space. wrapped line.\n\nSecond paragraph."


def test_normalize_rejects_blank_input():
    with pytest.raises(EmptyTextError, match="Enter some text"):
        normalize_text(" \t\n \r\n")


def test_sentence_split_keeps_abbreviations_initials_and_decimals():
    assert split_sentences("Dr. Ada Lovelace measured 3.14 units. Then U.S. researchers checked it.") == [
        "Dr. Ada Lovelace measured 3.14 units.",
        "Then U.S. researchers checked it.",
    ]


def test_chunks_preserve_sentence_order_and_paragraphs():
    first = "Alpha " + "a" * 260 + "."
    second = "Beta " + "b" * 260 + "."
    third = "Gamma " + "c" * 260 + "."
    chunks = chunk_text(f"{first} {second}\n\n{third}", chunk_size=500)

    assert len(chunks) == 3
    assert chunks[0] == first
    assert chunks[1] == second
    assert chunks[2] == third
    assert chunks[2].startswith("Gamma")


def test_oversized_sentence_wraps_at_word_boundaries():
    sentence = ("speech " * 100).strip() + "."
    chunks = chunk_text(sentence, chunk_size=500)

    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert " ".join(chunks) == sentence


def test_single_word_larger_than_target_stays_intact():
    long_word = "x" * 600
    chunks = chunk_text(f"{long_word} follows.", chunk_size=500)
    assert chunks == [long_word, "follows."]


def test_chunk_text_rejects_empty_and_over_limit_input():
    with pytest.raises(EmptyTextError):
        chunk_text(" \n\t")
    with pytest.raises(TextTooLongError, match="limit of 10 characters"):
        chunk_text("a" * 11, max_chars=10)


def test_chunk_text_uses_environment_settings_when_limits_are_omitted(monkeypatch):
    monkeypatch.setenv("TTS_CHUNK_SIZE", "500")
    monkeypatch.setenv("MAX_TEXT_CHARS", "10")
    with pytest.raises(TextTooLongError):
        chunk_text("a" * 11)


@pytest.mark.parametrize("options", [
    {"chunk_size": 0},
    {"chunk_size": 499},
    {"chunk_size": 1001},
    {"chunk_size": True},
    {"max_chars": 0},
    {"max_chars": 1_000_001},
    {"max_chars": True},
])
def test_chunk_text_validates_runtime_limits(options):
    with pytest.raises(ValueError):
        chunk_text("Hello.", **options)
