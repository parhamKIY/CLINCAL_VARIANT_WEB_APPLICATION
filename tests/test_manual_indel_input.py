"""Manual gap notation must cross a reference-verified canonical boundary."""
import pandas as pd
import pytest

from backend import pipeline
from backend.vcf_processing import parse_manual_variants, VCFProcessingError
from frontend import execution, ui

pytestmark = [pytest.mark.stage59_testing_v3, pytest.mark.testing_v3_input]


def row(ref, alt, pos=100):
    return dict(chrom="1", pos=pos, ref=ref, alt=alt, qual=None, filter=None)


def reference(**query):
    sequence = "GG" if query["end"] > query["start"] else "A"
    return dict(query, status="success", sequence=sequence,
                source="ensembl_grch38_sequence", provider_attempts=[])


@pytest.mark.parametrize("gap", ["-", "", None])
@pytest.mark.parametrize("deletion", [False, True])
def test_manual_gap_reaches_canonical_pipeline(monkeypatch, gap, deletion):
    raw = row("GG" if deletion else gap, gap if deletion else "GG")
    table = pd.DataFrame([raw], columns=ui.MANUAL_VARIANT_COLUMNS)
    rows = ui._normalize_manual_table(table)
    request = execution.prepare_analysis_recovery_request(
        uploaded_vcf=None, manual_variants=rows, phenotypes=[], llm_model=None)
    assert request["input_type"] == "manual"
    records = request["excel_input_records"]
    assert execution._validate_excel_recovery_records(records) == records
    monkeypatch.setattr(pipeline, "fetch_grch38_reference_sequence", reference)
    captured = []

    def run(**kwargs):
        captured.extend(kwargs["manual_variants"])
        assert kwargs["input_type"] == "manual"
        return pipeline.run_variant_processing(vcf_path=None,
            manual_variants=kwargs["manual_variants"], phenotypes=[])

    monkeypatch.setattr(execution, "run_analysis", run)
    result = execution.execute_analysis(uploaded_vcf=None, manual_variants=None,
        excel_input_records=records, input_type=request["input_type"],
        phenotypes=[], llm_model=None)
    assert [(v["pos"], v["ref"], v["alt"]) for v in captured] == [
        (99, "AGG", "A") if deletion else (100, "A", "AGG")]
    selected = result["input_preprocessing_results"][0]
    assert selected["source_type"] == "manual"
    assert selected["status"] == "NORMALIZED_AND_ACCEPTED"
    provenance = selected["source_provenance"]
    assert provenance["source_alt" if deletion else "source_ref"] == ("-" if gap == "-" else None)
    assert provenance["normalization_provenance"].startswith("manual_gap_")


def test_canonical_parser_stays_strict():
    with pytest.raises(VCFProcessingError):
        parse_manual_variants([row("-", "GG")])
    assert parse_manual_variants([row("A", "AGG")])[0]["alt"] == "AGG"


@pytest.mark.parametrize("ref,alt", [("-", "-"), ("", ""), ("-", "G,T"),
    ("-", "N"), ("-", "<INS>"), ("GG", "-,A")])
def test_ambiguous_manual_input_rejected(ref, alt):
    with pytest.raises(execution.FrontendExecutionError):
        execution.prepare_analysis_recovery_request(uploaded_vcf=None,
            manual_variants=[row(ref, alt)], phenotypes=[], llm_model=None)


@pytest.mark.parametrize("outage", [False, True])
@pytest.mark.parametrize("with_sibling", [False, True])
def test_unresolved_manual_indel_never_reaches_analysis(monkeypatch, outage, with_sibling):
    calls = []
    def fetch(**query):
        calls.append(query)
        return dict(query, status="unavailable" if outage else "success",
            sequence=None if outage else "CC", source="ensembl_grch38_sequence",
            provider_attempts=[])
    monkeypatch.setattr(pipeline, "fetch_grch38_reference_sequence", fetch)
    captured = []
    def run(**kwargs):
        captured.extend(kwargs["manual_variants"])
        return pipeline.run_variant_processing(vcf_path=None,
            manual_variants=kwargs["manual_variants"], phenotypes=[])
    monkeypatch.setattr(execution, "run_analysis", run)
    rows = [row("GG", "-")] + ([row("A", "T", 200)] if with_sibling else [])
    result = execution.execute_analysis(uploaded_vcf=None, manual_variants=rows,
        phenotypes=[], llm_model=None)
    assert len(calls) == 1  # A valid mismatch cannot trigger anchor lookup/provider shopping.
    assert captured == ([row("A", "T", 200)] if with_sibling else [])
    failed = result["input_preprocessing_results"][0]
    assert failed["status"] == "IDENTITY_UNRESOLVED"
    assert failed["source_type"] == "manual"
    assert failed["canonical_variant"] is None
    if not outage:
        assert failed["failure_reason"] == "REFERENCE_MISMATCH"


def test_gap_checkpoint_survives_disk_reload(monkeypatch, tmp_path):
    monkeypatch.setattr(execution.settings, "DATABASE_PATH", tmp_path / "analysis.db")
    request = execution.prepare_analysis_recovery_request(uploaded_vcf=None,
        manual_variants=[row("", "GG")], phenotypes=[], llm_model=None)
    token = "job-" + "a" * 32
    execution._persist_recovery_request(token, request)
    restored = execution._load_recovery_request(token)
    assert restored["input_type"] == "manual"
    assert restored["manual_variants"] == []
    assert restored["excel_input_records"] == request["excel_input_records"]


def test_existing_anchored_manual_path_unchanged(monkeypatch):
    rows = [row("A", "AGG"), row("AGG", "A", 200)]
    request = execution.prepare_analysis_recovery_request(uploaded_vcf=None,
        manual_variants=rows, phenotypes=[], llm_model=None)
    assert "excel_input_records" not in request
    assert request["manual_variants"] == rows
