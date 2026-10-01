"""One-time document uploads that return text and retain no original file."""

import os
from pathlib import Path
import tempfile
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.utils.text import EmptyTextError, TextTooLongError, normalize_text

router = APIRouter(prefix="/api/documents", tags=["documents"])
_FORM_OVERHEAD_BYTES = 64 * 1024
_COPY_BLOCK_BYTES = 64 * 1024


class DocumentResponse(BaseModel):
    text: str
    character_count: int
    file_type: Literal["txt", "pdf"]


class UploadTooLarge(Exception):
    pass


class InvalidDocument(Exception):
    pass


def _file_type(upload: UploadFile) -> Literal["txt", "pdf"]:
    name = upload.filename or ""
    if not name or "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="Choose a file with a simple .txt or .pdf name.")

    suffix = Path(name).suffix.lower()
    if suffix not in {".txt", ".pdf"}:
        raise HTTPException(status_code=415, detail="Only .txt and .pdf files are supported.")
    file_type: Literal["txt", "pdf"] = "txt" if suffix == ".txt" else "pdf"

    media_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    allowed = {"application/octet-stream", ""}
    allowed.add("text/plain" if file_type == "txt" else "application/pdf")
    if media_type not in allowed:
        raise HTTPException(status_code=415, detail="The file type does not match its extension.")
    return file_type


async def _bounded_body(request: Request, limit: int):
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise UploadTooLarge
        yield chunk


def _save_to_temp(upload: UploadFile, directory: Path, suffix: str, limit: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=directory)
    path = Path(filename)
    try:
        total = 0
        with os.fdopen(fd, "wb") as destination:
            while block := upload.file.read(_COPY_BLOCK_BYTES):
                total += len(block)
                if total > limit:
                    raise UploadTooLarge
                destination.write(block)
        if total == 0:
            raise InvalidDocument("The uploaded file is empty.")
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _extract_text(path: Path, file_type: Literal["txt", "pdf"], max_chars: int) -> str:
    if file_type == "txt":
        try:
            raw = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InvalidDocument("TXT files must use UTF-8 encoding.") from None
        if "\x00" in raw:
            raise InvalidDocument("The TXT file contains binary data.")
    else:
        import pymupdf

        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise InvalidDocument("The file is not a valid PDF.")
        try:
            with pymupdf.open(path) as document:
                if document.needs_pass:
                    raise InvalidDocument("Password-protected PDFs are not supported.")
                pages: list[str] = []
                length = 0
                for page in document:
                    content = page.get_text("text", sort=True)
                    length += len(content) + (2 if pages else 0)
                    if length > max_chars:
                        raise TextTooLongError(
                            f"Extracted text exceeds the configured limit of {max_chars} characters."
                        )
                    pages.append(content)
                raw = "\n\n".join(pages)
        except InvalidDocument:
            raise
        except TextTooLongError:
            raise
        except (pymupdf.FileDataError, RuntimeError, ValueError):
            raise InvalidDocument("The PDF could not be read. Check that it is not damaged.") from None

    if len(raw) > max_chars:
        raise TextTooLongError(f"Extracted text exceeds the configured limit of {max_chars} characters.")
    try:
        return normalize_text(raw)
    except EmptyTextError:
        if file_type == "pdf":
            raise InvalidDocument("The PDF has no readable text. Scanned pages need OCR.") from None
        raise InvalidDocument("The TXT file contains no readable text.") from None


@router.post(
    "",
    response_model=DocumentResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def upload_document(request: Request, response: Response) -> DocumentResponse:
    """Read one bounded multipart file, extract text, then delete its temp copy."""
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail="Send one multipart file field named file.")

    settings = request.app.state.settings
    body_limit = settings.max_upload_bytes + _FORM_OVERHEAD_BYTES
    length_header = request.headers.get("content-length")
    if length_header:
        try:
            declared_length = int(length_header)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from None
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.")
        if declared_length > body_limit:
            raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit.")

    # This parser instance never rolls the multipart file into the OS temp dir.
    # The body limit bounds memory before the file is copied into TEMP_DIR.
    parser = MultiPartParser(
        headers=request.headers,
        stream=_bounded_body(request, body_limit),
        max_files=1,
        max_fields=0,
    )
    parser.spool_max_size = 0
    try:
        form = await parser.parse()
    except UploadTooLarge:
        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit.") from None
    except MultiPartException:
        raise HTTPException(status_code=400, detail="Invalid upload form. Send one file field named file.") from None

    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="Send one file field named file.")
        file_type = _file_type(upload)
        if upload.size is not None and upload.size > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit.")

        path: Path | None = None
        try:
            path = await run_in_threadpool(
                _save_to_temp, upload, settings.temp_dir, f".{file_type}", settings.max_upload_bytes
            )
            text = await run_in_threadpool(_extract_text, path, file_type, settings.max_text_chars)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            return DocumentResponse(text=text, character_count=len(text), file_type=file_type)
        except UploadTooLarge:
            raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit.") from None
        except TextTooLongError as error:
            raise HTTPException(status_code=413, detail=str(error)) from None
        except InvalidDocument as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
    finally:
        await form.close()
