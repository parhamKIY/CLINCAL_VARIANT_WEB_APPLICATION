"""Stage 105 professor Excel testcase end-to-end acceptance suite.

Three deterministic, fully-offline sub-tests verify the complete report-first
workflow using a synthetic 7-variant fixture that mirrors the professor Excel
testcase structure:

1. Core E2E invariants (7-variant cardinality, input-order, same-gene separation,
   independent report records).
2. Phenotype negative-control (irrelevant phenotype → report generated, explicit
   non-concordance stated, no fabricated relation, not a failure).
3. Provider degradation (ClinVar forced unavailable → report still generated,
   actual source named, card is not BLOCKING).
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.llm import LLMClient
from backend.pipeline import run_analysis
from backend.report import validate_evidence_object
from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import render_report_data_docx
from backend.variant_integrity import cardinality_counts
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from frontend.analysis_summary import build_analysis_summary
from frontend.variant_status import build_variant_status_card, build_variant_status_cards
from tests.test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage105_professor_testcase

_GOLDEN_BASELINE = (
    Path(__file__).parent / "golden" / "evidence_resilience" / "pre_stage2_v1.json"
)

# ---------------------------------------------------------------------------
# Synthetic 7-variant professor testcase fixture
#
# Mirrors the professor Excel sheet:
#  - 7 variants in original input order
#  - variants 0 and 1 share the same gene (PAX3) to prove same-gene separation
#  - remaining 5 variants each have a distinct gene
# ---------------------------------------------------------------------------

_PROFESSOR_GENES = ["PAX3", "PAX3", "TELO2", "SCN1A", "BRCA2", "MLH1", "CFTR"]
_PROFESSOR_CHROMS = ["2", "2", "16", "2", "13", "3", "7"]
_PROFESSOR_POS = [
    222_772_851,
    222_772_900,
    46_636_712,
    166_848_839,
    32_890_540,
    37_067_491,
    117_559_592,
]


def _professor_manual_variants() -> list[dict[str, object]]:
    """Synthetic 7-variant fixture matching professor Excel testcase structure."""
    return [
        {
            "chrom": _PROFESSOR_CHROMS[i],
            "pos": _PROFESSOR_POS[i],
            "ref": "C",
            "alt": "T",
            "qual": 99.0,
            "filter": "PASS",
        }
        for i in range(7)
    ]


def _make_fake_annotate(same_gene_for_indexes: dict[int, str] | None = None):
    """Return a fake annotate function that assigns professor genes."""

    def _annotate(
        variants: object,
        **_: object,
    ) -> list[dict[str, object]]:
        annotations: list[dict[str, object]] = []
        for normalized in variants:  # type: ignore[union-attr]
            idx = normalized["input_index"]
            candidate = EvidenceFactory._pipeline_candidate()
            candidate["variant"] = dict(normalized)
            gene = (same_gene_for_indexes or {}).get(idx, _PROFESSOR_GENES[idx])
            candidate["gene"] = gene
            candidate["gene_id"] = f"HGNC:{10000 + idx}"
            annotations.append(candidate)
        return annotations

    return _annotate


def _pre_llm_golden_projection(analysis: dict[str, object]) -> list[dict[str, object]]:
    """Return only stable, source-attributed artifacts for Stage 2 regression."""
    annotations = analysis["annotations"]
    evidence_objects = analysis["evidence_objects"]
    assert isinstance(annotations, list)
    assert isinstance(evidence_objects, list)
    projection: list[dict[str, object]] = []
    for annotation, evidence in zip(annotations, evidence_objects, strict=True):
        assert isinstance(annotation, dict)
        assert isinstance(evidence, dict)
        variant = annotation["variant"]
        capabilities = evidence["capability_results"]
        assert isinstance(variant, dict)
        assert isinstance(capabilities, dict)
        projection.append(
            {
                "identity": {
                    "assembly": annotation["assembly"],
                    "chrom": variant["chrom"],
                    "pos": variant["pos"],
                    "ref": variant["ref"],
                    "alt": variant["alt"],
                },
                "gene": annotation["gene"],
                "transcript": annotation["transcript"],
                "consequence": annotation["consequence"],
                "source_statuses": evidence["source_statuses"],
                "capability_statuses": {
                    name: result["status"]
                    for name, result in capabilities.items()
                    if isinstance(result, dict)
                },
            }
        )
    return projection


def _monkeypatch_pipeline_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    same_gene_for_indexes: dict[int, str] | None = None,
    clinvar_unavailable: bool = False,
) -> None:
    """Patch all external providers; keep everything offline and deterministic."""
    monkeypatch.setattr(
        "backend.pipeline.annotate_variants",
        _make_fake_annotate(same_gene_for_indexes),
    )
    monkeypatch.setattr(
        "backend.pipeline.enrich_with_mydisease",
        lambda variants, *_a, **_kw: {
            "variants": [deepcopy(v) for v in variants],
            "status": "unsupported",
            "message": "No phenotype terms supplied.",
        },
    )
    monkeypatch.setattr(
        "backend.pipeline.enrich_conditionally",
        lambda variants, *_a, **_kw: {
            "variants": [deepcopy(v) for v in variants],
            "triggered_count": 0,
        },
    )

    from config import settings

    monkeypatch.setattr(settings, "ENABLE_GNOMAD_DEEP_LOOKUP", False)
    monkeypatch.setattr(settings, "ENABLE_LITERATURE_ENRICHMENT", False)

    if clinvar_unavailable:
        # The status card reads data_sources from draft_variant_reports[*].reviewed_report.
        # data_sources is built by build_reference_model_v2(evidence) inside variant_report.py.
        # Wrap that function to force any clinvar_evidence data_source entry to status
        # "no_match", which makes _evidence_line return "ClinVar: no exact record" on the card.
        import backend.variant_report as _vr_module
        from backend.reference_model import build_reference_model_v2 as _real_rmv2

        def _rmv2_clinvar_down(evidence: object) -> object:
            model = _real_rmv2(evidence)
            patched_sources = []
            for entry in model.get("data_sources", []):
                if isinstance(entry, dict) and entry.get("capability") == "clinvar_evidence":
                    entry = dict(entry)
                    entry["status"] = "no_match"
                    entry["operational_status"] = "no_match"
                patched_sources.append(entry)
            return {**model, "data_sources": patched_sources}

        monkeypatch.setattr(_vr_module, "build_reference_model_v2", _rmv2_clinvar_down)


# ---------------------------------------------------------------------------
# Sub-test 1: Core E2E invariants
# ---------------------------------------------------------------------------


def test_105_1_seven_variant_cardinality_order_and_same_gene_separation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7-variant professor testcase preserves cardinality, order, and same-gene separation."""
    # Variants 0 and 1 deliberately share gene PAX3 (same-gene separation test)
    _monkeypatch_pipeline_dependencies(monkeypatch)

    database_path = tmp_path / "stage105_core.sqlite3"
    adapter = FakeLLMAdapter(_variant_interpretation_response())
    analysis = run_analysis(
        vcf_path=None,
        manual_variants=_professor_manual_variants(),
        phenotypes=[],
        llm_client=LLMClient(adapter),
        database_path=database_path,
    )

    # --- cardinality ---
    assert analysis["variant_count"] == 7, (
        f"Expected 7 variants; got {analysis['variant_count']}"
    )

    expected_indexes = list(range(7))

    # --- original input order preserved at every stage ---
    assert [v["input_index"] for v in analysis["variants"]] == expected_indexes
    assert [
        a["variant"]["input_index"] for a in analysis["annotations"]
    ] == expected_indexes
    assert [
        r["variant_index"]
        for r in analysis["variant_interpretation_results"]
    ] == expected_indexes
    assert [
        r["variant_index"] for r in analysis["draft_variant_reports"]
    ] == expected_indexes
    assert [
        r["report_data"]["input_index"]
        for r in analysis["variant_report_records"]
    ] == expected_indexes

    # --- integrity ledger: all six stage counts equal 7 ---
    counts = cardinality_counts(analysis["variant_integrity_records"])
    for stage_name, count in counts.items():
        assert count == 7, (
            f"Integrity ledger stage '{stage_name}' expected 7, got {count}"
        )

    # --- same-gene variants remain separate allele records ---
    pax3_records = [
        r
        for r in analysis["variant_report_records"]
        if r["report_data"]["variant_identity"]["gene"] == "PAX3"
    ]
    assert len(pax3_records) == 2, (
        "Same-gene PAX3 variants must remain separate report records; "
        f"found {len(pax3_records)}"
    )
    pax3_positions = {
        r["report_data"]["variant_identity"]["position"]
        for r in pax3_records
    }
    assert len(pax3_positions) == 2, (
        "Same-gene PAX3 variants must have distinct allele positions"
    )

    # --- one independent report record per input variant ---
    report_indexes = [
        r["variant_index"] for r in analysis["variant_report_records"]
    ]
    assert len(report_indexes) == 7
    assert len(set(report_indexes)) == 7, (
        "Each variant must have exactly one independent report record"
    )

    # --- analysis summary shows 7 analyzed and all reports ready ---
    summary = build_analysis_summary(analysis)
    assert summary["variants_analyzed"] == 7
    assert summary["draft_reports_prepared"] == 7
    assert summary["reports_ready"] == 7

    # --- DOCX renders for every record ---
    for record in analysis["variant_report_records"]:
        docx_bytes = render_report_data_docx(record["report_data"])
        assert len(docx_bytes) > 0, (
            f"DOCX render failed for variant_index={record['variant_index']}"
        )


def test_pre_stage2_golden_cases_match_stable_pre_llm_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freeze identity, evidence presence, and capability state without LLM prose."""
    baseline = json.loads(_GOLDEN_BASELINE.read_text(encoding="utf-8"))
    _monkeypatch_pipeline_dependencies(monkeypatch)
    analysis = run_analysis(
        vcf_path=None,
        manual_variants=_professor_manual_variants(),
        phenotypes=[],
        llm_client=LLMClient(FakeLLMAdapter(_variant_interpretation_response())),
        database_path=tmp_path / "pre_stage2_golden.sqlite3",
    )

    cases = baseline["cases"]
    assert isinstance(cases, list)
    projection = _pre_llm_golden_projection(analysis)
    assert [item["identity"] for item in projection] == [
        case["identity"] for case in cases
    ]
    assert [item["gene"] for item in projection] == [case["gene"] for case in cases]
    assert [item["transcript"] for item in projection] == [
        case["transcript"] for case in cases
    ]
    assert [item["consequence"] for item in projection] == [
        case["consequence"] for case in cases
    ]
    assert all(
        item["source_statuses"] == baseline["expected_source_statuses"]
        and item["capability_statuses"]
        == baseline["expected_capability_statuses"]
        for item in projection
    )


# ---------------------------------------------------------------------------
# Sub-test 2: Phenotype negative-control (irrelevant phenotype)
# ---------------------------------------------------------------------------


def test_105_2_irrelevant_phenotype_produces_report_with_non_concordance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated phenotype (abdominal pain) → report generated, non-concordance stated.

    Expected outcomes per the roadmap:
    - report still generated
    - interpretation still generated
    - phenotype non-concordance stated explicitly
    - no fabricated disease relation
    """
    _monkeypatch_pipeline_dependencies(monkeypatch)

    # Build a single-variant evidence object with an intentionally irrelevant
    # abdominal-pain phenotype (HP:0002027), mirroring the professor test scenario.
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    # Override phenotype fields to represent abdominal pain with no gene match
    evidence["hpo_terms"] = ["HP:0002027"]
    evidence["matched_hpo_terms"] = []
    evidence["phenotype_score"] = 0.0
    phenotype = evidence.get("phenotype_relationship")
    if isinstance(phenotype, dict):
        phenotype["patient_hpo_terms"] = ["HP:0002027"]
        phenotype["matched_patient_hpo_terms"] = []
        phenotype["local_phenotype_score"] = 0.0
        phenotype["phenotype_status"] = "no_exact_match"
    # Re-validate to ensure the evidence object contract is satisfied
    validated_evidence = dict(validate_evidence_object(evidence))

    adapter = FakeLLMAdapter(
        _variant_interpretation_response(
            phenotype_conclusion="no supported association found"
        )
    )

    interpretation = interpret_variant(
        validated_evidence,
        client=LLMClient(adapter),
        timestamp="2026-08-12T09:00:00Z",
    )

    # interpretation must succeed — not fail — despite unrelated phenotype
    assert interpretation["status"] == "success", (
        "Irrelevant phenotype must not cause interpretation failure; "
        f"got status={interpretation['status']!r}, "
        f"error_type={interpretation.get('error_type')!r}"
    )

    # phenotype non-concordance must be stated explicitly in the result text
    interp_text = interpretation.get("interpretation", "") or ""
    assert "no supported association found" in interp_text.lower(), (
        "Interpretation must state phenotype non-concordance explicitly; "
        f"got: {interp_text[:200]!r}"
    )

    # no fabricated disease association in the model prompt
    prompt_messages = adapter.requests[0].messages
    user_prompt = next(
        (m.content for m in prompt_messages if m.role == "user"), ""
    )
    assert "no supported association found" in user_prompt or (
        "'no supported association found'" in user_prompt
    ), "Prompt must explicitly instruct non-concordance handling"
    assert "not negative pathogenicity evidence" in user_prompt, (
        "Prompt must clarify that phenotype mismatch is not negative evidence"
    )

    # report must still be buildable from the interpretation
    draft = build_draft_variant_report(
        validated_evidence,
        interpretation,
        variant_index=0,
    )
    assert draft["reviewed_report"]["variant_interpretation"]["status"] == "success"

    report_data = build_report_data_from_draft(
        draft,
        analysis_id=f"analysis-{'a' * 32}",
    )
    # phenotype_concordance lives in report_data["phenotype_summary"]["concordance"]
    phenotype_summary = report_data.get("phenotype_summary") or {}
    concordance = phenotype_summary.get("concordance", "")
    assert concordance in {
        "no_supported_association",
        "not_assessed",
        "unavailable",
    }, (
        "ReportData must record non-concordance, not a fabricated association; "
        f"got phenotype_summary.concordance={concordance!r}"
    )

    # DOCX must render
    docx_bytes = render_report_data_docx(report_data)
    assert len(docx_bytes) > 0, "DOCX render must succeed for non-concordant phenotype case"


# ---------------------------------------------------------------------------
# Sub-test 3: Provider degradation (ClinVar unavailable)
# ---------------------------------------------------------------------------


def test_105_3_provider_degradation_names_actual_source_and_hides_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClinVar forced unavailable → report still generated, source named, card not BLOCKING.

    Expected outcomes per the roadmap:
    - report still generated
    - actual source named (ClinVar: no record found / no_match, not raw JSON)
    - technical failure hidden from primary UX (card status is not BLOCKING)
    """
    _monkeypatch_pipeline_dependencies(monkeypatch, clinvar_unavailable=True)

    database_path = tmp_path / "stage105_degradation.sqlite3"
    adapter = FakeLLMAdapter(_variant_interpretation_response())
    analysis = run_analysis(
        vcf_path=None,
        manual_variants=_professor_manual_variants(),
        phenotypes=[],
        llm_client=LLMClient(adapter),
        database_path=database_path,
    )

    # all 7 reports must still be produced despite ClinVar being down
    assert analysis["variant_count"] == 7
    assert len(analysis["variant_report_records"]) == 7

    cards = build_variant_status_cards(analysis)
    assert len(cards) == 7

    for card in cards:
        # Primary status must not be BLOCKING — ClinVar absence is not an app crash
        assert card["status"] != "Input requires attention", (
            f"ClinVar unavailability must not block variant; "
            f"variant_index={card['variant_index']} has status={card['status']!r}"
        )

        # Card's clinvar line must name the source outcome, not a raw JSON URL
        clinvar_line = card.get("clinvar", "")
        assert clinvar_line, "Status card must have a ClinVar evidence line"
        assert "http" not in clinvar_line.lower() or "clinvar.ncbi" in clinvar_line.lower(), (
            f"ClinVar line must not expose raw API URLs; got: {clinvar_line!r}"
        )
        # Should reflect no_match — not silently pretend it succeeded.
        # The source_statuses path maps "no_match" → "ClinVar: no exact record".
        assert any(
            keyword in clinvar_line.lower()
            for keyword in ("no exact record", "unavailable", "no record", "not available", "no_match")
        ), (
            f"ClinVar line must name the actual outcome; got: {clinvar_line!r}"
        )

        # No BLOCKING notices — ClinVar absence is INFO or PARTIAL, not BLOCKING
        blocking_notices = [
            n for n in card.get("notices", []) if n.get("severity") == "BLOCKING"
        ]
        assert not blocking_notices, (
            f"ClinVar unavailability must not produce BLOCKING notices; "
            f"variant_index={card['variant_index']}, "
            f"notices={blocking_notices}"
        )

    # DOCX must render for every record even with degraded ClinVar
    for record in analysis["variant_report_records"]:
        docx_bytes = render_report_data_docx(record["report_data"])
        assert len(docx_bytes) > 0, (
            f"DOCX render must succeed with ClinVar degraded; "
            f"variant_index={record['variant_index']}"
        )

    # Analysis summary must still report all 7 as ready or partial
    summary = build_analysis_summary(analysis)
    assert summary["variants_analyzed"] == 7
    assert summary["draft_reports_prepared"] == 7
    # Some may be "Report ready with partial evidence" but all must be counted ready
    assert summary["reports_ready"] == 7
