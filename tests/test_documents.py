import asyncio
import tempfile

from httpx import ASGITransport, AsyncClient
import pymupdf
import pytest

from app.main import app


def pdf_bytes(text: str | None = None, *, encrypted: bool = False) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    options = {}
    if encrypted:
        options = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "secret"}
    output = document.tobytes(**options)
    document.close()
    return output


async def post_file(filename: str, content: bytes, media_type: str):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/documents", files={"file": (filename, content, media_type)})


@pytest.fixture
def upload_directory(monkeypatch, tmp_path):
    directory = tmp_path / "uploads"
    monkeypatch.setenv("TEMP_DIR", str(directory))
    return directory


def assert_upload_removed(directory):
    assert not directory.exists() or list(directory.iterdir()) == []


def test_txt_upload_returns_normalized_text_and_deletes_file(upload_directory):
    response = asyncio.run(post_file("notes.txt", b"\xef\xbb\xbfHello,   world.\r\n\r\nNext line.", "text/plain"))
    assert response.status_code == 200
    assert response.json() == {
        "text": "Hello, world.\n\nNext line.",
        "character_count": len("Hello, world.\n\nNext line."),
        "file_type": "txt",
    }
    assert response.headers["cache-control"] == "no-store"
    assert_upload_removed(upload_directory)


def test_pdf_upload_extracts_text_and_deletes_original(upload_directory):
    response = asyncio.run(post_file("article.pdf", pdf_bytes("Readable PDF text."), "application/pdf"))
    assert response.status_code == 200
    assert response.json()["text"] == "Readable PDF text."
    assert response.json()["file_type"] == "pdf"
    assert_upload_removed(upload_directory)


def test_pdf_pages_keep_reading_order(upload_directory):
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "First page.")
    document.new_page().insert_text((72, 72), "Second page.")
    content = document.tobytes()
    document.close()

    response = asyncio.run(post_file("pages.pdf", content, "application/pdf"))
    assert response.status_code == 200
    assert response.json()["text"] == "First page.\n\nSecond page."
    assert_upload_removed(upload_directory)


@pytest.mark.parametrize("filename,content,media_type,status", [
    ("../escape.pdf", b"%PDF-1.4", "application/pdf", 400),
    ("other.docx", b"hello", "application/octet-stream", 415),
    ("sample.txt", b"hello", "application/pdf", 415),
    ("bad.txt", b"\xff\xfe", "text/plain", 422),
    ("empty.txt", b"", "text/plain", 422),
    ("binary.txt", b"Hello\x00world", "text/plain", 422),
    ("blank.txt", b"  \n  ", "text/plain", 422),
    ("fake.pdf", b"not a pdf", "application/pdf", 422),
    ("broken.pdf", b"%PDF-broken", "application/pdf", 422),
])
def test_upload_failures_leave_no_original(upload_directory, filename, content, media_type, status):
    response = asyncio.run(post_file(filename, content, media_type))
    assert response.status_code == status
    assert "detail" in response.json()
    assert_upload_removed(upload_directory)


@pytest.mark.parametrize("content,expected_message", [
    (pdf_bytes(), "no readable text"),
    (pdf_bytes("Private content", encrypted=True), "Password-protected"),
])
def test_unreadable_pdfs_are_rejected_and_removed(upload_directory, content, expected_message):
    response = asyncio.run(post_file("unreadable.pdf", content, "application/pdf"))
    assert response.status_code == 422
    assert expected_message in response.json()["detail"]
    assert_upload_removed(upload_directory)


def test_file_and_extracted_text_limits(upload_directory, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    response = asyncio.run(post_file("large.txt", b"a" * 11, "text/plain"))
    assert response.status_code == 413
    assert_upload_removed(upload_directory)

    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10000000")
    monkeypatch.setenv("MAX_TEXT_CHARS", "10")
    response = asyncio.run(post_file("long.pdf", pdf_bytes("More than ten characters"), "application/pdf"))
    assert response.status_code == 413
    assert_upload_removed(upload_directory)


def test_large_multipart_part_never_uses_os_temp(upload_directory, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "unavailable-os-temp"))
    monkeypatch.setenv("MAX_TEXT_CHARS", "1000000")
    response = asyncio.run(post_file("large.txt", b"Hello " + "🎧".encode() * 280_000, "text/plain"))
    assert response.status_code == 200
    assert response.json()["character_count"] == 280_006
    assert_upload_removed(upload_directory)


def test_streamed_body_has_a_bound_without_content_length(upload_directory, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "10")
    boundary = "bounded"
    header = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="large.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
    ).encode()

    async def chunks():
        yield header
        yield b"a" * 70_000
        yield f"\r\n--{boundary}--\r\n".encode()

    async def send():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.post(
                    "/api/documents",
                    content=chunks(),
                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )

    response = asyncio.run(send())
    assert response.status_code == 413
    assert_upload_removed(upload_directory)


def test_malformed_form_is_rejected(upload_directory):
    async def send():
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                return await client.post(
                    "/api/documents",
                    content=b"invalid",
                    headers={"Content-Type": "multipart/form-data; boundary=abc"},
                )

    response = asyncio.run(send())
    assert response.status_code == 400
    assert_upload_removed(upload_directory)
