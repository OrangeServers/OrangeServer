"""Document ingestion trust-boundary and real converter checks."""
from io import BytesIO
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from werkzeug.datastructures import FileStorage

from app.ai.knowledge import KnowledgeValidationError
from app.ai.knowledge_ingestion import _convert_binary, preview_document


def _upload(data: bytes, filename: str, mimetype: str) -> FileStorage:
    return FileStorage(stream=BytesIO(data), filename=filename, content_type=mimetype)


def _docx(text: str, *, macro: bool = False) -> bytes:
    content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'''
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', content_types)
        archive.writestr('_rels/.rels', relationships)
        archive.writestr('word/document.xml', document)
        if macro:
            archive.writestr('word/vbaProject.bin', b'macro')
    return output.getvalue()


def _pdf(text: str) -> bytes:
    stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode()
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream',
    ]
    data = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(b'%d 0 obj\n' % number + body + b'\nendobj\n')
    xref = len(data)
    data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        data.extend(b'%010d 00000 n \n' % offset)
    data.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % xref)
    return bytes(data)


@pytest.mark.parametrize(('upload', 'kind', 'expected'), [
    (_upload(b'# Disk\nCheck usage.', 'disk.md', 'text/markdown'), 'markdown', '# Disk'),
    (_upload(b'Check inode usage.', 'inode.txt', 'text/plain'), 'text', 'inode'),
    (_upload(_pdf('systemd unit failed'), 'unit.pdf', 'application/pdf'), 'pdf', 'systemd'),
    (_upload(
        _docx('journalctl error code E42'), 'service.docx',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    ), 'docx', 'E42'),
])
def test_all_supported_formats_produce_reviewable_markdown(upload, kind, expected):
    preview = preview_document(upload)
    assert preview['detected_type'] == kind
    assert expected in preview['content']


def test_signature_mime_macro_and_filename_boundaries():
    with pytest.raises(KnowledgeValidationError, match='MIME'):
        preview_document(_upload(b'%PDF-1.4', 'fake.pdf', 'text/plain'))
    with pytest.raises(KnowledgeValidationError, match='signature'):
        preview_document(_upload(b'not a pdf', 'fake.pdf', 'application/pdf'))
    with pytest.raises(KnowledgeValidationError, match='macro'):
        preview_document(_upload(_docx('x', macro=True), 'macro.docx', 'application/zip'))
    preview = preview_document(_upload(b'content', r'C:\private\safe.md', 'text/plain'))
    assert preview['title'] == 'safe'


def test_converter_timeout_encryption_output_limit_and_no_original_file(
    monkeypatch, tmp_path,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 1, stdout=b'', stderr=b'PDFPasswordIncorrect: encrypted',
    ))
    with pytest.raises(KnowledgeValidationError, match='encrypted PDF'):
        preview_document(_upload(_pdf('secret'), 'secret.pdf', 'application/pdf'))
    assert list(tmp_path.iterdir()) == []

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired('markitdown', 20)

    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(KnowledgeValidationError, match='timed out'):
        preview_document(_upload(_docx('slow'), 'slow.docx', 'application/zip'))

    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, stdout=b'x' * (1024 * 1024 + 1), stderr=b'',
    ))
    with pytest.raises(KnowledgeValidationError, match='1 MiB'):
        preview_document(_upload(_pdf('large'), 'large.pdf', 'application/pdf'))


def test_converter_child_does_not_inherit_application_secrets(monkeypatch):
    captured = {}
    monkeypatch.setenv('OGS_MYSQL_PASSWORD', 'must-not-leak')

    def run(*args, **kwargs):
        captured.update(kwargs['env'])
        return subprocess.CompletedProcess(args[0], 0, stdout=b'converted', stderr=b'')

    monkeypatch.setattr(subprocess, 'run', run)
    assert _convert_binary(b'input', '.pdf') == 'converted'
    assert captured['PYTHONIOENCODING'] == 'utf-8'
    assert 'OGS_MYSQL_PASSWORD' not in captured
