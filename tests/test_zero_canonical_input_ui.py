"""User-facing semantics when every selected input identity is unresolved."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pyarrow.ipc as pyarrow_ipc
from streamlit.testing.v1 import AppTest

from backend.pipeline import run_annovar_like_input_processing
from frontend.analysis_summary import (
    build_analysis_summary,
    build_identity_resolution_failure_presentation,
)
from frontend.ui import _result_status
from frontend.xlsx_selection import selected_source_rows_message


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _reference_unavailable(**kwargs: object) -> dict[str, object]:
    return {
        "status": "unavailable",
        "assembly": kwargs["assembly"],
        "chrom": kwargs["chrom"],
        "start": kwargs["start"],
        "end": kwargs["end"],
        "sequence": None,
        "source": "reference_mesh",
        "fallback_used": True,
        "failure_reason": "REFERENCE_LOOKUP_UNAVAILABLE",
        "provider_attempts": [],
    }


def _all_unresolved_result() -> dict[str, object]:
    return run_annovar_like_input_processing(
        [
            {
                "worksheet": "Noticable",
                "row": 4,
                "chrom": "13",
                "start": 102875767,
                "end": 102875770,
                "ref": "GTGC",
                "alt": 0,
                "filter": "QDfilter",
            },
            {
                "worksheet": "Noticable",
                "row": 5,
                "chrom": "13",
                "start": 102875773,
                "end": 102875773,
                "ref": "C",
                "alt": 0,
                "filter": "QDfilter",
            },
        ],
        phenotypes=[],
        reference_fetcher=_reference_unavailable,
    )


def test_all_unresolved_inputs_have_a_distinct_actionable_presentation() -> None:
    result = _all_unresolved_result()

    presentation = build_identity_resolution_failure_presentation(result)

    assert presentation is not None
    assert presentation["headline"] == "Analysis could not proceed"
    assert presentation["selected_count"] == 2
    assert presentation["canonical_count"] == 0
    assert presentation["unresolved_count"] == 2
    assert [row["Source row"] for row in presentation["rows"]] == [4, 5]
    assert all(
        row["Reason"] == "GRCh38 reference verification was unavailable."
        for row in presentation["rows"]
    )
    assert all("Retry" in row["Next action"] for row in presentation["rows"])
    assert "QDfilter" not in str(presentation)
    assert build_analysis_summary(result)["headline"] == (
        "Analysis could not proceed"
    )
    assert _result_status(result) == (
        "Analysis could not proceed",
        "error",
        True,
    )


def test_zero_canonical_ui_explains_rejections_without_empty_report_review() -> None:
    result = _all_unresolved_result()
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=10)
    app.session_state["pipeline_result"] = result
    app.run(timeout=10)

    assert not app.exception
    assert any(
        item.value == "Analysis could not proceed" for item in app.subheader
    )
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["Selected source rows"] == "2"
    assert metrics["Canonical variants"] == "0"
    assert metrics["Rows requiring attention"] == "2"
    assert any(
        "None of the selected rows had a safely resolvable GRCh38 variant identity"
        in item.value
        for item in app.warning
    )
    tables = [
        pyarrow_ipc.open_stream(
            BytesIO(dataframe.proto.arrow_data.data)
        ).read_all()
        for dataframe in app.dataframe
    ]
    resolution_table = next(
        table for table in tables if "Next action" in table.column_names
    )
    assert resolution_table.num_rows == 2
    assert all(
        row["Reason"] == "GRCh38 reference verification was unavailable."
        for row in resolution_table.to_pylist()
    )
    assert any(
        "No clinical report was generated because variant identity validation"
        in item.value
        for item in app.info
    )
    assert all(
        item.value != "Draft Variant Review — Evidence and interpretation"
        for item in app.subheader
    )


def test_mixed_or_successful_inputs_keep_existing_partial_semantics() -> None:
    result = _all_unresolved_result()
    result["variants"] = [
        {"variant": {"chrom": "1", "pos": 100, "ref": "A", "alt": "G"}}
    ]
    result["variant_count"] = 1

    assert build_identity_resolution_failure_presentation(result) is None
    assert build_analysis_summary(result)["headline"] == (
        "Analysis completed partially"
    )


def test_selected_rows_message_describes_pending_identity_validation() -> None:
    assert selected_source_rows_message(2) == (
        "2 source rows selected. Their variant identities will be validated "
        "when analysis starts."
    )
