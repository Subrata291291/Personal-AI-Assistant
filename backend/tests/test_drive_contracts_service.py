import asyncio
import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from app.db.models.drive_selected_file import DriveSelectedFile
from app.services.drive_contracts_service import (
    DriveConnectionMissingError,
    DriveContractsService,
    DriveFileInaccessibleError,
    DriveFileNotSelectedError,
    DriveScopeMissingError,
    DriveUnsupportedFileError,
    SelectedContract,
)
from app.services.drive_text_extraction import (
    DOCX_MIME_TYPE,
    GOOGLE_DOC_MIME_TYPE,
    PDF_MIME_TYPE,
)

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CONNECTION_ID = uuid4()
FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz"

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _service(
    *,
    connection=SimpleNamespace(id=CONNECTION_ID),
    credential=SimpleNamespace(scopes=[DRIVE_FILE_SCOPE]),
    selected_file=None,
):
    service = DriveContractsService.__new__(DriveContractsService)
    service.db = SimpleNamespace(commit=lambda: None, rollback=lambda: None)
    service.api_client = SimpleNamespace()
    service.selected_file_repository = SimpleNamespace(
        get_by_user_and_file=lambda **kwargs: selected_file,
        upsert=lambda **kwargs: SimpleNamespace(**kwargs),
        delete_by_user_and_file=lambda **kwargs: True,
    )
    service.connection_repository = SimpleNamespace(
        get_active_google_connection=lambda **kwargs: connection,
    )
    service.credential_repository = SimpleNamespace(
        get_by_connection=lambda **kwargs: credential,
    )
    return service


def _selected_file(user_id=USER_ID):
    return SimpleNamespace(
        user_id=user_id,
        connection_id=CONNECTION_ID,
        provider_file_id=FILE_ID,
    )


def _contract(mime_type=PDF_MIME_TYPE):
    return SelectedContract(
        provider_file_id=FILE_ID,
        name="Master Services Agreement.pdf",
        mime_type=mime_type,
        modified_time=None,
        size_bytes=2048,
    )


def _build_pdf(text: str) -> bytes:
    """Build a tiny single-page PDF with extractable text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>"
        ),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        (
            b"<</Length "
            + str(len(stream)).encode()
            + b">>stream\n"
            + stream
            + b"\nendstream"
        ),
    ]

    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n".encode()
    pdf += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return pdf


def _build_docx(paragraphs: list[tuple[str, str | None]]) -> bytes:
    body = "".join(
        (
            "<w:p>"
            + (
                f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
                if style
                else ""
            )
            + f"<w:r><w:t>{text}</w:t></w:r>"
            + "</w:p>"
        )
        for text, style in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def test_normalize_file_id_rejects_invalid_values():
    for value in ("", "a b", "abc!", "x" * 201):
        with pytest.raises(DriveFileNotSelectedError):
            DriveContractsService.normalize_file_id(value)

    assert DriveContractsService.normalize_file_id(f"  {FILE_ID}  ") == FILE_ID


def test_validate_connection_requires_an_owned_active_google_connection():
    service = _service(connection=None)

    with pytest.raises(DriveConnectionMissingError):
        service._validate_connection(USER_ID, CONNECTION_ID)


def test_validate_connection_requires_a_contracts_read_scope():
    service = _service(
        credential=SimpleNamespace(
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        ),
    )

    with pytest.raises(DriveScopeMissingError):
        service._validate_connection(USER_ID, CONNECTION_ID)


@pytest.mark.parametrize(
    "scope",
    [
        DRIVE_FILE_SCOPE,
        "https://www.googleapis.com/auth/drive.readonly",
    ],
)
def test_validate_connection_accepts_narrow_or_existing_read_grants(scope):
    service = _service(credential=SimpleNamespace(scopes=[scope]))

    service._validate_connection(USER_ID, CONNECTION_ID)


def test_allowlist_rejects_a_file_that_was_not_selected():
    service = _service(selected_file=None)

    with pytest.raises(DriveFileNotSelectedError):
        service.get_allowlisted_file(user_id=USER_ID, file_id=FILE_ID)


def test_cross_user_file_id_is_not_visible_through_the_allowlist():
    calls = []

    def _get(**kwargs):
        calls.append(kwargs)
        return None

    service = _service()
    service.selected_file_repository.get_by_user_and_file = _get

    with pytest.raises(DriveFileNotSelectedError):
        service.get_allowlisted_file(user_id=OTHER_USER_ID, file_id=FILE_ID)

    assert calls == [{
        "user_id": OTHER_USER_ID,
        "provider_file_id": FILE_ID,
    }]


def test_extract_rejects_a_file_outside_the_user_allowlist():
    service = _service(selected_file=None)
    service._download = AsyncMock(return_value=b"not used")

    with pytest.raises(DriveFileNotSelectedError):
        asyncio.run(service.extract_selected_file_text(
            user_id=USER_ID,
            file_id=FILE_ID,
        ))

    service._download.assert_not_awaited()


def test_contract_metadata_rejects_trashed_unsupported_and_mismatched_files():
    base = {
        "id": FILE_ID,
        "name": "Contract",
        "mimeType": PDF_MIME_TYPE,
        "trashed": False,
    }

    contract = DriveContractsService._contract_from_metadata(
        base,
        expected_file_id=FILE_ID,
    )
    assert contract.provider_file_id == FILE_ID

    with pytest.raises(DriveFileInaccessibleError):
        DriveContractsService._contract_from_metadata(
            {**base, "trashed": True},
            expected_file_id=FILE_ID,
        )

    with pytest.raises(DriveFileInaccessibleError):
        DriveContractsService._contract_from_metadata(
            {**base, "id": "different"},
            expected_file_id=FILE_ID,
        )

    with pytest.raises(DriveUnsupportedFileError):
        DriveContractsService._contract_from_metadata(
            {**base, "mimeType": "text/plain"},
            expected_file_id=FILE_ID,
        )


def test_register_selected_file_stores_metadata_only():
    service = _service()
    service._validate_connection = lambda *args, **kwargs: None
    service._fetch_live_contract = AsyncMock(return_value=_contract())
    captured = {}

    def _upsert(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    service.selected_file_repository.upsert = _upsert

    asyncio.run(service.register_selected_file(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        file_id=FILE_ID,
    ))

    assert captured == {
        "user_id": USER_ID,
        "connection_id": CONNECTION_ID,
        "provider_file_id": FILE_ID,
        "name": "Master Services Agreement.pdf",
        "mime_type": PDF_MIME_TYPE,
        "modified_time": None,
        "size_bytes": 2048,
    }
    assert "text" not in captured
    assert "content" not in captured


def test_pdf_extraction_returns_text_and_page_metadata():
    service = _service(selected_file=_selected_file())
    service._validate_connection = lambda *args, **kwargs: None
    service._fetch_live_contract = AsyncMock(return_value=_contract())
    service._download = AsyncMock(return_value=_build_pdf("Hello Contract"))

    extracted = asyncio.run(service.extract_selected_file_text(
        user_id=USER_ID,
        file_id=FILE_ID,
    ))

    assert extracted.document.extraction == "pdf"
    assert extracted.document.page_count == 1
    assert "Hello Contract" in extracted.document.text


def test_docx_extraction_preserves_paragraphs_and_headings():
    service = _service(selected_file=_selected_file())
    service._validate_connection = lambda *args, **kwargs: None
    service._fetch_live_contract = AsyncMock(
        return_value=_contract(DOCX_MIME_TYPE),
    )
    service._download = AsyncMock(return_value=_build_docx([
        ("Services Agreement", "Heading1"),
        ("Payment is due within 30 days.", None),
    ]))

    extracted = asyncio.run(service.extract_selected_file_text(
        user_id=USER_ID,
        file_id=FILE_ID,
    ))

    assert extracted.document.extraction == "docx"
    assert "## Services Agreement" in extracted.document.text
    assert "Payment is due within 30 days." in extracted.document.text


def test_google_doc_extraction_uses_exported_plain_text():
    service = _service(selected_file=_selected_file())
    service._validate_connection = lambda *args, **kwargs: None
    service._fetch_live_contract = AsyncMock(
        return_value=_contract(GOOGLE_DOC_MIME_TYPE),
    )
    service._download = AsyncMock(return_value=b"Section 7 - Termination\n30 days")

    extracted = asyncio.run(service.extract_selected_file_text(
        user_id=USER_ID,
        file_id=FILE_ID,
    ))

    assert extracted.document.extraction == "google_doc"
    assert "Section 7 - Termination" in extracted.document.text


def test_removing_a_selection_only_deletes_app_metadata():
    service = _service()
    captured = {}

    def _delete(**kwargs):
        captured.update(kwargs)
        return True

    service.selected_file_repository.delete_by_user_and_file = _delete

    assert service.remove_selected_file(
        user_id=USER_ID,
        file_id=FILE_ID,
    ) is True
    assert captured == {
        "user_id": USER_ID,
        "provider_file_id": FILE_ID,
    }


def test_drive_selected_file_model_stores_no_contract_content():
    columns = set(DriveSelectedFile.__table__.columns.keys())

    assert columns == {
        "id",
        "user_id",
        "connection_id",
        "provider_file_id",
        "name",
        "mime_type",
        "modified_time",
        "size_bytes",
        "selected_at",
    }
    assert "text" not in columns
    assert "content" not in columns


def test_drive_service_exposes_no_drive_mutation_methods():
    for name in (
        "upload",
        "create_file",
        "update_file",
        "delete_file",
        "move_file",
        "share_file",
        "create_permission",
    ):
        assert not hasattr(DriveContractsService, name)


def test_pdf_reader_round_trip_is_available_for_test_fixture():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert buffer.getvalue().startswith(b"%PDF")
