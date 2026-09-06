from copy import deepcopy

import pytest

from backend.gene_identity import evidence_has_resolved_gene_identity
from backend.llm import LLMClient
from backend.report import build_evidence_object
from backend.shadow_composition import semantic_evidence_for_llm
from backend.variant_interpretation import interpret_variants
from backend.variant_report import build_draft_variant_report
from tests.test_stage12_3_gene_identity_resolution import (
    AnnotationFactory,
    FakeLLMAdapter,
    FakeResponse,
    _annotate,
    _valid_response,
)


@pytest.mark.parametrize("provider", ["VariantValidator", "GeneBe"])
@pytest.mark.parametrize(
    "transcript", ["ENST000001", "NM_000001.1", "NM_000001.2", None]
)
def test_gene_verification_preserves_primary_transcript_context(
    provider: str, transcript: str | None
) -> None:
    response = deepcopy(AnnotationFactory._vep_response())
    primary = response["transcript_consequences"][0]
    primary["transcript_id"] = transcript
    primary["hgvsc"] = None
    # Retain a primary protein only for its original transcript.
    primary["hgvsp"] = (
        "ENSP000001:p.Lys34Arg" if transcript == "ENST000001" else None
    )
    annotation, _ = _annotate(
        response,
        variantvalidator_response=(
            FakeResponse(200, AnnotationFactory._variantvalidator_response())
            if provider == "VariantValidator"
            else FakeResponse(503, {})
        ),
        genebe_response=(
            AnnotationFactory._genebe_variant_response()
            if provider == "GeneBe"
            else None
        ),
    )
    resolution = annotation["gene_identity_resolution"]
    assert resolution["source"] == "Ensembl VEP"
    assert resolution["provenance"]["verified_by"] == provider
    assert resolution["gene"] == "GENE1"
    assert resolution["transcript"] == transcript
    assert resolution["hgvs_c"] is None
    assert resolution["hgvs_p"] == primary["hgvsp"]
    assert "hgvs_c" in resolution["missing_context_fields"]
    if transcript is None:
        assert "transcript" in resolution["missing_context_fields"]
    if primary["hgvsp"] is None:
        assert "hgvs_p" in resolution["missing_context_fields"]

    before = deepcopy(annotation)
    evidence = build_evidence_object(annotation)
    projection = semantic_evidence_for_llm(evidence)
    projected_vep = projection["annotations"]["vep"]
    expected_resolution = deepcopy(resolution)
    expected_resolution["provenance"].pop("retrieved_at")
    assert projected_vep["gene_identity_resolution"] == expected_resolution
    # Supporting transcript evidence survives under its own source.
    if provider == "VariantValidator":
        verification = projected_vep["gene_identity_verification"]
        assert verification["validated_transcript_hgvs"] == "NM_000001.2:c.100A>G"
    else:
        assert projection["annotations"]["genebe"]["transcript"] == "NM_000001.2"
    assert evidence_has_resolved_gene_identity(evidence) is True

    adapter = FakeLLMAdapter(_valid_response())
    result = interpret_variants(
        [evidence], client=LLMClient(adapter), max_retries=0
    )[0]
    assert result["status"] == "success"
    assert result["ai_classification"] == "Uncertain significance"
    assert "The available evidence supports cautious review." in result["interpretation"]
    assert len(adapter.requests) == 1
    report = build_draft_variant_report(evidence, result, variant_index=0)
    assert report["reviewed_report"]["variant_summary"]["gene"] == "GENE1"
    assert annotation == before
