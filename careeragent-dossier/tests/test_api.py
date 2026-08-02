"""
tests/test_api.py

Pure helpers in the API layer — UUID validation and the EditError -> HTTP mapping.
Importing backend.api builds the FastAPI app but opens no DB connection (the
Store is created only in the lifespan), so these stay hermetic.
"""
from backend.api import _edit_http, _valid_uuid
from store import EditError


def test_valid_uuid_accepts_a_uuid():
    assert _valid_uuid("88bb2bd9-7a7c-4fab-831b-5bda2fb83cc2")


def test_valid_uuid_rejects_garbage():
    assert not _valid_uuid("not-a-uuid")


def test_edit_error_not_found_maps_to_422():
    exc = _edit_http(EditError("not_found"))
    assert exc.status_code == 422


def test_edit_error_not_unique_maps_to_409_with_count():
    exc = _edit_http(EditError("not_unique", 3))
    assert exc.status_code == 409
    assert "3" in exc.detail
