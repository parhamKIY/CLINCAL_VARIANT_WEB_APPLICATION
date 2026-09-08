"""Tests for Streamlit /_stcore/upload_file transport endpoint.

Verifies actual browser upload endpoint behavior beyond parser unit tests:
- Small valid XLSX (<1 MB) upload success (HTTP 204)
- >1 MB valid XLSX within application limit upload success (HTTP 204)
- Over-limit (>25 MB) file rejection (HTTP 413)
- Missing/invalid XSRF token rejection (HTTP 403)
- Invalid/expired session_id rejection (HTTP 400)
"""

from __future__ import annotations

from io import BytesIO
import os
from unittest.mock import MagicMock

import pytest
from openpyxl import Workbook
from starlette.applications import Starlette
from starlette.testclient import TestClient

from streamlit import config
from streamlit.runtime.memory_uploaded_file_manager import MemoryUploadedFileManager
from streamlit.web.server.starlette import starlette_app_utils
from streamlit.web.server.starlette.starlette_app import create_upload_routes


def _create_test_xlsx(rows: int, entropy_bytes: int = 0) -> bytes:
    """Generate a valid XLSX file with optional non-compressible padding."""
    wb = Workbook()
    ws = wb.active
    ws.append(("Chr", "Start", "End", "Ref", "Alt", "Quality", "Filter"))
    for i in range(rows):
        pad = os.urandom(entropy_bytes).hex() if entropy_bytes > 0 else "G"
        ws.append(("1", 1000 + i, 1000 + i, "A", pad, 30.0, "PASS"))
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def upload_client():
    """Create a TestClient configured with an active Streamlit runtime session and valid XSRF."""
    runtime = MagicMock()
    runtime.is_active_session.return_value = True
    upload_mgr = MemoryUploadedFileManager("/_stcore/upload_file")

    routes = create_upload_routes(runtime, upload_mgr, "")
    app = Starlette(routes=routes)

    token = starlette_app_utils.generate_xsrf_token_string()
    client = TestClient(app, cookies={"_streamlit_xsrf": token})

    return {
        "client": client,
        "headers": {"X-Xsrftoken": token},
        "runtime": runtime,
        "upload_mgr": upload_mgr,
    }


def test_upload_small_valid_xlsx(upload_client) -> None:
    """Small XLSX (<1 MB) uploads successfully to /_stcore/upload_file with 204."""
    client = upload_client["client"]
    headers = upload_client["headers"]
    upload_mgr = upload_client["upload_mgr"]

    data = _create_test_xlsx(rows=10)
    assert len(data) < 100_000  # Well under 1 MB

    response = client.put(
        "/_stcore/upload_file/sess_test/file_small",
        files={"file": ("variants.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )

    assert response.status_code == 204
    files = upload_mgr.get_files("sess_test", ["file_small"])
    assert len(files) == 1
    file_rec = files[0]
    assert file_rec.name == "variants.xlsx"
    assert file_rec.data == data


def test_upload_greater_than_1mb_valid_xlsx(upload_client) -> None:
    """Valid XLSX >1 MB within the 25 MB limit uploads successfully to Streamlit with 204."""
    client = upload_client["client"]
    headers = upload_client["headers"]
    upload_mgr = upload_client["upload_mgr"]

    # Generate a valid XLSX ~1.8 MB
    data = _create_test_xlsx(rows=12000, entropy_bytes=80)
    size_mb = len(data) / (1024 * 1024)
    assert size_mb > 1.0, f"Expected >1 MB, got {size_mb:.2f} MB"
    assert size_mb < 25.0, f"Expected <25 MB, got {size_mb:.2f} MB"

    response = client.put(
        "/_stcore/upload_file/sess_test/file_large",
        files={"file": ("large_cohort.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )

    assert response.status_code == 204
    files = upload_mgr.get_files("sess_test", ["file_large"])
    assert len(files) == 1
    file_rec = files[0]
    assert file_rec.name == "large_cohort.xlsx"
    assert len(file_rec.data) == len(data)


def test_upload_over_limit_rejected(upload_client) -> None:
    """Payload exceeding server.maxUploadSize (25 MB) is rejected with 413 File too large."""
    client = upload_client["client"]
    headers = upload_client["headers"]

    max_mb = config.get_option("server.maxUploadSize")
    over_limit_bytes = (max_mb + 1) * 1024 * 1024
    dummy_payload = b"X" * over_limit_bytes

    response = client.put(
        "/_stcore/upload_file/sess_test/file_oversized",
        files={"file": ("too_big.xlsx", dummy_payload, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )

    assert response.status_code == 413
    assert "File too large" in response.text


def test_upload_without_xsrf_rejected(upload_client) -> None:
    """PUT request missing XSRF token is rejected with 403."""
    client = upload_client["client"]
    data = _create_test_xlsx(rows=5)

    # Request without X-Xsrftoken header
    response = client.put(
        "/_stcore/upload_file/sess_test/file_noxsrf",
        files={"file": ("variants.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )

    assert response.status_code == 403
    assert "XSRF token missing or invalid" in response.text


def test_upload_invalid_session_rejected(upload_client) -> None:
    """PUT request with inactive or expired session_id is rejected with 400."""
    client = upload_client["client"]
    headers = upload_client["headers"]
    runtime = upload_client["runtime"]

    runtime.is_active_session.return_value = False
    data = _create_test_xlsx(rows=5)

    response = client.put(
        "/_stcore/upload_file/expired_session/file_test",
        files={"file": ("variants.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )

    assert response.status_code == 400
    assert "Invalid session_id" in response.text


def test_proxy_client_max_body_size_mismatch_and_bounded_fix(upload_client) -> None:
    """Demonstrate how omitting client_max_body_size (default 1m) causes 413, while 30m fixes it."""
    client = upload_client["client"]
    headers = upload_client["headers"]

    # Generate a valid XLSX > 1 MB (e.g. 1.86 MB)
    data = _create_test_xlsx(rows=12000, entropy_bytes=80)
    file_bytes_len = len(data)
    assert file_bytes_len > 1024 * 1024  # > 1 MB

    # 1. Nginx default: client_max_body_size = 1m (1,048,576 bytes)
    nginx_default_limit = 1 * 1024 * 1024
    if file_bytes_len > nginx_default_limit:
        simulated_nginx_response_status = 413
    else:
        simulated_nginx_response_status = 204

    # With default Nginx, the request never reaches Streamlit:
    assert simulated_nginx_response_status == 413

    # 2. Configured bounded limit: client_max_body_size 30m
    nginx_bounded_limit = 30 * 1024 * 1024
    assert file_bytes_len < nginx_bounded_limit

    # The request reaches Streamlit backend directly and succeeds:
    backend_response = client.put(
        "/_stcore/upload_file/sess_test/file_large_proxy",
        files={"file": ("large.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        headers=headers,
    )
    assert backend_response.status_code == 204
