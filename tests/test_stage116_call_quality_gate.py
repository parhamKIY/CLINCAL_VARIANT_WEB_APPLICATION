"""Stage 116 call-quality contract and pre-annotation gate."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from backend.call_quality import (
    CallQualityError,
    build_call_quality_records,
)
from backend.database import load_pipeline_state
from backend.pipeline import run_analysis
from backend.excel_processing import parse_excel_variants
from backend.vcf_processing import parse_manual_variants, parse_vcf


@pytest.mark.stage116_call_quality
@pytest.mark.parametrize(
    ("raw_filter", "expected"),
    (
        (" PASS ", "passed"),
        ("pass", "passed"),
        (None, "not_evaluated"),
        ("", "not_evaluated"),
        (".", "not_evaluated"),
        ("q10", "failed"),
        ("q10;LowQual", "failed"),
        ("VQSRTrancheSNP99.00to99.90", "failed"),
    ),
)
def test_stage116_evaluates_filter_without_assigning_provider_semantics(
    raw_filter: str | None,
    expected: str,
) -> None:
    records = build_call_quality_records(
        [{"input_index": 0, "filter": raw_filter}],
    )

    assert records[0]["status"] == expected


@pytest.mark.stage116_call_quality
def test_stage116_blocks_unacknowledged_or_unoverridden_records() -> None:
    with pytest.raises(CallQualityError):
        build_call_quality_records(
            [
                {"input_index": 0, "filter": None},
                {"input_index": 1, "filter": "q10"},
            ],
            require_ready=True,
        )


@pytest.mark.stage116_call_quality
def test_stage116_marks_vcf_excel_and_manual_pass_inputs_as_passed(
    tmp_path: Path,
) -> None:
    vcf_path = tmp_path / "passed.vcf"
    vcf_path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
        "1\t100\t.\tA\tG\t50\tPASS\t.\n",
        encoding="utf-8",
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["CHROM", "POS", "REF", "ALT", "QUAL", "FILTER"])
    sheet.append(["1", 101, "A", "G", 50, "PASS"])
    workbook_bytes = BytesIO()
    workbook.save(workbook_bytes)
    source_variants = (
        parse_vcf(vcf_path),
        parse_excel_variants(workbook_bytes.getvalue()),
        parse_manual_variants(
            [
                {
                    "chrom": "1",
                    "pos": 102,
                    "ref": "A",
                    "alt": "G",
                    "qual": 50.0,
                    "filter": "PASS",
                }
            ]
        ),
    )

    for variants in source_variants:
        records = build_call_quality_records(
            [
                {**variant, "input_index": index}
                for index, variant in enumerate(variants)
            ],
            require_ready=True,
        )
        assert [record["status"] for record in records] == ["passed"]


@pytest.mark.stage116_call_quality
def test_stage116_preserves_row_quality_across_manual_and_multiallelic_input() -> None:
    variants = parse_manual_variants(
        [
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "C,G",
                "qual": 99.0,
                "filter": "q10;LowQual",
            }
        ]
    )
    indexed = [
        {**variant, "input_index": index}
        for index, variant in enumerate(variants)
    ]

    records = build_call_quality_records(indexed)

    assert [record["status"] for record in records] == ["failed", "failed"]


@pytest.mark.stage116_call_quality
def test_stage116_blocks_before_annotation_and_llm_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def annotation_should_not_run(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(
        "backend.pipeline._annotate_and_match",
        annotation_should_not_run,
    )

    result = run_analysis(
        vcf_path=None,
        manual_variants=[
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "G",
                "qual": 50.0,
                "filter": "q10",
            }
        ],
        phenotypes=[],
        persist_analysis=False,
    )

    assert calls == 0
    assert result["current_stage"] == "vcf_processing"
    assert result["variants"][0]["call_quality"]["status"] == "failed"


@pytest.mark.stage116_call_quality
def test_stage116_accepts_audited_override_and_persists_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def stop_after_gate(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("stop after call-quality gate")

    monkeypatch.setattr("backend.pipeline._annotate_and_match", stop_after_gate)
    result = run_analysis(
        vcf_path=None,
        manual_variants=[
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "G",
                "qual": 50.0,
                "filter": "q10",
            }
        ],
        phenotypes=[],
        call_quality_overrides={
            0: {
                "reason": "Validated against the upstream assay record.",
                "timestamp": "2026-08-13T10:00:00Z",
            }
        },
        database_path=tmp_path / "analysis.sqlite3",
    )

    assert calls == 1
    assert result["analysis_id"] is not None
    assert result["variants"][0]["filter"] == "q10"
    assert result["variants"][0]["call_quality"]["override"] == {
        "reason": "Validated against the upstream assay record.",
        "timestamp": "2026-08-13T10:00:00Z",
    }
    restored = load_pipeline_state(
        result["analysis_id"],
        database_path=tmp_path / "analysis.sqlite3",
    )
    assert restored["variants"] == result["variants"]
