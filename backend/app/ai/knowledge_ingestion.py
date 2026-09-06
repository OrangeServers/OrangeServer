"""Bounded, review-before-save document conversion for the knowledge base."""
from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path
import subprocess
import sys
from typing import Any
from zipfile import BadZipFile, ZipFile

from app.ai.knowledge import (
    MAX_DOCUMENT_BYTES,
    KnowledgeValidationError,
    _bounded_text,
    _document_content,
)


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
CONVERSION_TIMEOUT_SECONDS = 20
MAX_ARCHIVE_MEMBERS = 2_000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
SUPPORTED_TYPES = {
    '.md': 'markdown',
    '.txt': 'text',
    '.pdf': 'pdf',
    '.docx': 'docx',
}
ALLOWED_MIME_TYPES = {
    '.md': {'application/octet-stream', 'text/markdown', 'text/plain'},
    '.txt': {'application/octet-stream', 'text/plain'},
    '.pdf': {'application/octet-stream', 'application/pdf'},
    '.docx': {
        'application/octet-stream',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/zip',
    },
}


def _convert_binary(data: bytes, extension: str) -> str:
    environment = {'PYTHONIOENCODING': 'utf-8'}
    for name in ('SYSTEMROOT', 'WINDIR'):
        if name in os.environ:
            environment[name] = os.environ[name]
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name('knowledge_converter.py')),
                extension,
                str(MAX_DOCUMENT_BYTES + 1),
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CONVERSION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise KnowledgeValidationError('document conversion timed out') from exc
    except OSError as exc:
        raise KnowledgeValidationError('document converter is unavailable') from exc
    if result.returncode != 0:
        error = result.stderr.decode('utf-8', errors='replace').lower()
        if extension == '.pdf' and ('password' in error or 'encrypt' in error):
            raise KnowledgeValidationError('encrypted PDF is not supported')
        raise KnowledgeValidationError('document conversion failed')
    if len(result.stdout) > MAX_DOCUMENT_BYTES:
        raise KnowledgeValidationError('document exceeds 1 MiB')
    return result.stdout.decode('utf-8', errors='strict')


def _validate_signature(data: bytes, extension: str) -> None:
    if extension in {'.md', '.txt'}:
        if b'\x00' in data:
            raise KnowledgeValidationError('text file signature does not match extension')
        return
    if extension == '.pdf':
        if b'%PDF-' not in data[:1024]:
            raise KnowledgeValidationError('PDF signature does not match extension')
        return
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                raise KnowledgeValidationError('DOCX signature does not match extension')
            members = archive.infolist()
            if (
                len(members) > MAX_ARCHIVE_MEMBERS
                or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES
            ):
                raise KnowledgeValidationError('DOCX expanded content is too large')
            if 'word/vbaProject.bin' in names or any(
                item.flag_bits & 0x1 for item in members
            ):
                raise KnowledgeValidationError('encrypted or macro-enabled DOCX is not supported')
    except BadZipFile as exc:
        raise KnowledgeValidationError('DOCX signature does not match extension') from exc


def preview_document(file_storage: Any) -> dict[str, Any]:
    if file_storage is None or not str(file_storage.filename or '').strip():
        raise KnowledgeValidationError('file is required')

    filename = Path(str(file_storage.filename).replace('\\', '/')).name
    extension = Path(filename).suffix.lower()
    detected_type = SUPPORTED_TYPES.get(extension)
    if detected_type is None:
        raise KnowledgeValidationError('file type must be md, txt, pdf, or docx')
    mimetype = str(file_storage.mimetype or 'application/octet-stream').lower()
    if mimetype not in ALLOWED_MIME_TYPES[extension]:
        raise KnowledgeValidationError('file MIME type does not match extension')

    data = file_storage.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise KnowledgeValidationError('upload exceeds 10 MiB')
    if not data:
        raise KnowledgeValidationError('file is empty')
    _validate_signature(data, extension)

    if extension in {'.md', '.txt'}:
        try:
            content = data.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise KnowledgeValidationError('text file must use UTF-8') from exc
    else:
        content = _convert_binary(data, extension)

    try:
        content = _document_content(content)
    except KnowledgeValidationError as exc:
        if extension == '.pdf' and 'required' in str(exc):
            raise KnowledgeValidationError(
                'PDF contains no extractable text; OCR is not enabled'
            ) from exc
        raise

    title = _bounded_text(Path(filename).stem, 'title', 128)
    return {
        'title': title,
        'content': content,
        'detected_type': detected_type,
        'warnings': [],
    }
