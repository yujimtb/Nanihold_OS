"""Attachment validation, storage and text extraction."""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
from pathlib import Path

from fastapi import UploadFile
from pypdf import PdfReader

from vsm.ids import generate_uuid
from vsm.web.models import Attachment

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
MAX_TOTAL_BYTES = 50 * 1024 * 1024
TEXT_SUFFIXES = {".txt", ".md", ".json", ".csv"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_SUFFIXES = TEXT_SUFFIXES | IMAGE_SUFFIXES | {".pdf"}


async def save_attachments(
    uploads: list[UploadFile],
    destination: Path,
) -> list[Attachment]:
    if len(uploads) > MAX_FILES:
        raise ValueError(f"添付ファイルは最大{MAX_FILES}件です")

    destination.mkdir(parents=True, exist_ok=True)
    attachments: list[Attachment] = []
    total = 0
    for upload in uploads:
        name = Path(upload.filename or "attachment").name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise ValueError(f"未対応のファイル形式です: {name}")
        content = await upload.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"1ファイルは最大20 MBです: {name}")
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("添付ファイルの合計は最大50 MBです")

        attachment_id = generate_uuid()
        path = destination / f"{attachment_id}{suffix}"
        path.write_bytes(content)
        media_type = upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        extracted_text, model_content = _extract_content(name, suffix, media_type, content)
        attachments.append(
            Attachment(
                attachment_id=attachment_id,
                name=name,
                media_type=media_type,
                size=len(content),
                path=path,
                extracted_text=extracted_text,
                model_content=model_content,
            )
        )
    return attachments


def _extract_content(
    name: str,
    suffix: str,
    media_type: str,
    content: bytes,
) -> tuple[str, dict | None]:
    if suffix in {".txt", ".md"}:
        return content.decode("utf-8"), None
    if suffix == ".json":
        parsed = json.loads(content.decode("utf-8"))
        return json.dumps(parsed, ensure_ascii=False, indent=2), None
    if suffix == ".csv":
        text = content.decode("utf-8")
        rows = list(csv.reader(io.StringIO(text)))
        return "\n".join(" | ".join(row) for row in rows), None
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages).strip()
        if text:
            return text, None
        return "", {
            "type": "image",
            "name": name,
            "note": "スキャンPDFです。現在の固定モデルが画像入力に対応する場合のみ解析できます。",
        }
    if suffix in IMAGE_SUFFIXES:
        return "", {
            "type": "image_url",
            "image_url": {
                "url": f"data:{media_type};base64,{base64.b64encode(content).decode('ascii')}"
            },
            "name": name,
        }
    raise ValueError(f"未対応のファイル形式です: {name}")

