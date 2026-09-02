from __future__ import annotations

import json
from copy import deepcopy

from backend.annotation import annotate_variants
from backend.gene_identity import (
    GENE_IDENTITY_UNRESOLVED,
    resolve_gene_identity,
)
from backend.llm import LLMClient, LLMResponse
from backend.report import build_evidence_object
from backend.shadow_composition import semantic_evidence_for_llm
from backend.variant_interpretation import interpret_variants
from backend.variant_report import build_draft_variant_report
from frontend.interpretation_failure_semantics import (
    interpretation_failure_message,
)
from tests.test_pipeline import (
    FakeLLMAdapter,
    FakeResponse,
    FakeSession,
    TestAnnotation as AnnotationFactory,
)


def _valid_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": (
                    "The available evidence supports a cautious draft "
                    "interpretation."
                ),
                "conflict_assessment": "No material conflict was identified.",
                "phenotype_conclusion": "phenotype evidence unavailable",
                "warnings": [],
            }
        ),
        model="stage12-4-test-model",
        finish_reason="stop",
    )


def _annotate(
    variant: dict[str, object],
    vep_response: object,
    *,
    variantvalidator_response: object | None = None,
    genebe_response: dict[str, object] | None = None,
) -> tuple[dict[str, object], FakeSession]:
    session = FakeSession(
        [vep_response],
        variantvalidator_responses=(
            [variantvalidator_response]
            if variantvalidator_response is not None
            else []
        ),
        genebe_responses=(
            [FakeResponse(200, {"variants": [genebe_response]})]
            if genebe_response is not None
            else []
        ),
    )
    annotation = annotate_variants(
        [variant],  # type: ignore[list-item]
        session=session,  # type: ignore[arg-type]
        max_retries=0,
        use_cache=False,
    )[0]
    return annotation, session


def _vep_response_for_variant(
    variant: dict[str, object],
    *,
    gene: str | None = "GENE1",
    transcript: str | None = "ENST000001",
    hgvs_c: str | None = "ENST000001:c.100A>G",
    hgvs_p: str | None = "ENSP000001:p.Lys34Arg",
    consequence: str = "missense_variant",
) -> dict[str, object]:
    response = deepcopy(AnnotationFactory._vep_response())
    response["input"] = (
        f"{variant['chrom']} {variant['pos']} cv_0 {variant['ref']} "
        f"{variant['alt']} . . ."
    )
    response["most_severe_consequence"] = consequence
    records = response["transcript_consequences"]
    assert isinstance(records, list)
    record = records[0]
    assert isinstance(record, dict)
    record.update(
        {
            "gene_symbol": gene,
            "transcript_id": transcript,
            "hgvsc": hgvs_c,
            "hgvsp": hgvs_p,
            "consequence_terms": [consequence],
            "biotype": "protein_coding" if hgvs_p else "non_coding",
        }
    )
    return response


def test_coding_indel_keeps_exact_identity_and_vep_gene_context() -> None:
    variant = {
        "chrom": "1",
        "pos": 100,
        "ref": "AT",
        "alt": "A",
        "qual": 50.0,
        "filter": "PASS",
        "genotype": "0/1",
    }
    response = _vep_response_for_variant(
        variant,
        transcript="ENST000001",
        hgvs_c="ENST000001:c.100del",
        hgvs_p="ENSP000001:p.Lys34ArgfsTer5",
        consequence="frameshift_variant",
    )
    annotation, session = _annotate(variant, FakeResponse(200, [response]))

    resolution = annotation["gene_identity_resolution"]
    assert resolution["source"] == "Ensembl VEP"
    assert resolution["identity_proof"]["observed"] == {
        "assembly": "GRCh38",
        "chrom": "1",
        "pos": 100,
        "ref": "AT",
        "alt": "A",
    }
    assert session.variantvalidator_get_calls == []


def test_vep_operational_failure_is_rescued_by_exact_variantvalidator() -> None:
    variant = dict(AnnotationFactory._variant())
    annotation, session = _annotate(
        variant,
        FakeResponse(503, {"error": "unavailable"}),
        variantvalidator_response=FakeResponse(
            200,
            AnnotationFactory._variantvalidator_response(),
        ),
    )

    resolution = annotation["gene_identity_resolution"]
    assert resolution["status"] == "resolved"
    assert resolution["source"] == "VariantValidator"
    assert resolution["provenance"]["provider_role"] == "fallback"
    assert resolution["identity_proof"]["decision"] == "EXACT"
    assert len(session.variantvalidator_get_calls) == 1


def test_transcript_poor_noncoding_vep_gene_remains_interpretable() -> None:
    variant = dict(AnnotationFactory._variant())
    response = _vep_response_for_variant(
        variant,
        transcript=None,
        hgvs_c=None,
        hgvs_p=None,
        consequence="regulatory_region_variant",
    )
    annotation, session = _annotate(
        variant,
        FakeResponse(200, [response]),
        variantvalidator_response=FakeResponse(503, {"error": "unavailable"}),
    )
    evidence = build_evidence_object(annotation)
    adapter = FakeLLMAdapter(_valid_response())

    interpretation = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        max_retries=0,
    )[0]

    assert len(session.variantvalidator_get_calls) == 1
    assert annotation["gene_identity_resolution"]["status"] == "resolved"
    assert annotation["gene_identity_resolution"]["source"] == "Ensembl VEP"
    assert annotation["gene_identity_resolution"]["gene"] == "GENE1"
    assert annotation["gene_identity_resolution"]["transcript"] is None
    assert annotation["gene_identity_resolution"]["missing_context_fields"] == [
        "transcript",
        "hgvs_c",
        "hgvs_p",
    ]
    assert interpretation["status"] == "success"
    assert len(adapter.requests) == 1


def test_exact_variantvalidator_gene_without_coding_fields_can_resolve() -> None:
    variant = dict(AnnotationFactory._variant())
    vep_response = _vep_response_for_variant(variant, gene=None)
    vv_response = AnnotationFactory._variantvalidator_response()
    vv_record = vv_response["NM_000001.2:c.100A>G"]
    assert isinstance(vv_record, dict)
    vv_record["hgvs_transcript_variant"] = None
    vv_record["hgvs_predicted_protein_consequence"] = {}
    annotation, _ = _annotate(
        variant,
        FakeResponse(200, [vep_response]),
        variantvalidator_response=FakeResponse(200, vv_response),
    )

    resolution = annotation["gene_identity_resolution"]
    assert resolution["status"] == "resolved"
    assert resolution["source"] == "VariantValidator"
    assert resolution["gene"] == "GENE1"
    assert resolution["transcript"] is None
    assert resolution["hgvs_c"] is None


def test_verified_noncoding_resolution_records_all_missing_optional_context() -> None:
    variant = dict(AnnotationFactory._variant())
    vep_response = _vep_response_for_variant(
        variant,
        transcript=None,
        hgvs_c=None,
        hgvs_p=None,
        consequence="regulatory_region_variant",
    )
    vv_response = AnnotationFactory._variantvalidator_response()
    vv_record = vv_response["NM_000001.2:c.100A>G"]
    assert isinstance(vv_record, dict)
    vv_record["hgvs_transcript_variant"] = None
    vv_record["hgvs_predicted_protein_consequence"] = {}
    annotation, _ = _annotate(
        variant,
        FakeResponse(200, [vep_response]),
        variantvalidator_response=FakeResponse(200, vv_response),
    )

    resolution = annotation["gene_identity_resolution"]
    assert resolution["source"] == "Ensembl VEP"
    assert resolution["provenance"]["verified_by"] == "VariantValidator"
    assert resolution["consequence"] == "regulatory_region_variant"
    assert resolution["missing_context_fields"] == [
        "transcript",
        "hgvs_c",
        "hgvs_p",
    ]


def test_authoritative_gene_conflict_fails_closed() -> None:
    variant = dict(AnnotationFactory._variant())
    vep_response = _vep_response_for_variant(variant, hgvs_c=None)
    vv_response = AnnotationFactory._variantvalidator_response()
    vv_record = vv_response["NM_000001.2:c.100A>G"]
    assert isinstance(vv_record, dict)
    vv_record["gene_symbol"] = "GENE2"
    annotation, _ = _annotate(
        variant,
        FakeResponse(200, [vep_response]),
        variantvalidator_response=FakeResponse(200, vv_response),
        genebe_response=AnnotationFactory._genebe_variant_response(),
    )
    evidence = build_evidence_object(annotation)
    adapter = FakeLLMAdapter(_valid_response())

    interpretation = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        max_retries=0,
    )[0]

    assert annotation["gene_identity_resolution"]["status"] == "unresolved"
    assert interpretation["error_type"] == GENE_IDENTITY_UNRESOLVED
    assert adapter.requests == []


def test_cross_check_disagreement_is_retained_without_overwrite() -> None:
    variant = dict(AnnotationFactory._variant())
    response = _vep_response_for_variant(variant)
    annotation, _ = _annotate(variant, FakeResponse(200, [response]))
    annotation["sources"]["myvariant"]["gene"] = "OTHER1"
    annotation["sources"]["clinvar"]["gene"] = "OTHER2"

    resolution = resolve_gene_identity(annotation)

    assert resolution["gene"] == "GENE1"
    assert resolution["cross_checks"] == {
        "myvariant": {"gene": "OTHER1", "agreement": "conflicts"},
        "clinvar": {"gene": "OTHER2", "agreement": "conflicts"},
    }


def test_resolved_gene_context_reaches_evidence_llm_projection_and_report() -> None:
    variant = dict(AnnotationFactory._variant())
    response = _vep_response_for_variant(variant)
    annotation, _ = _annotate(variant, FakeResponse(200, [response]))
    evidence = build_evidence_object(annotation)
    projection = semantic_evidence_for_llm(evidence)
    adapter = FakeLLMAdapter(_valid_response())
    interpretation = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        max_retries=0,
    )[0]
    report = build_draft_variant_report(
        evidence,
        interpretation,
        variant_index=0,
    )

    assert projection["gene"] == "GENE1"
    assert projection["gene_id"] == "ENSG000001"
    assert projection["transcript"] == "ENST000001"
    assert projection["variant_context"]["hgvs_c"] == (
        "ENST000001:c.100A>G"
    )
    assert projection["consequence"] == "missense_variant"
    assert "shadow_composition" not in projection
    assert "annotation_promotion" not in projection
    assert report["reviewed_report"]["variant_summary"]["gene"] == "GENE1"


def test_three_variant_batch_isolates_unresolved_middle_variant() -> None:
    variant = dict(AnnotationFactory._variant())
    response = _vep_response_for_variant(variant)
    resolved_annotation, _ = _annotate(
        variant,
        FakeResponse(200, [response]),
    )
    unresolved_response = _vep_response_for_variant(
        variant,
        gene=None,
        transcript=None,
        hgvs_c=None,
        hgvs_p=None,
        consequence="intergenic_variant",
    )
    unresolved_annotation, _ = _annotate(
        variant,
        FakeResponse(200, [unresolved_response]),
        variantvalidator_response=FakeResponse(404, {"flag": "empty_result"}),
    )
    resolved = build_evidence_object(resolved_annotation)
    unresolved = build_evidence_object(unresolved_annotation)
    adapter = FakeLLMAdapter(_valid_response())

    results = interpret_variants(
        [resolved, unresolved, deepcopy(resolved)],
        client=LLMClient(adapter),
        max_retries=0,
    )

    assert [item["status"] for item in results] == [
        "success",
        "failed",
        "success",
    ]
    assert results[1]["error_type"] == GENE_IDENTITY_UNRESOLVED
    assert len(adapter.requests) == 2
    failed_report = build_draft_variant_report(
        unresolved,
        results[1],
        variant_index=1,
    )
    assert failed_report["reviewed_report"]["variant_summary"]["gene"] is None
    assert failed_report["reviewed_report"]["variant_interpretation"][
        "failure_type"
    ] == GENE_IDENTITY_UNRESOLVED
    assert "exact, provenance-backed gene identity" in (
        interpretation_failure_message(results[1]["error_type"])
    )
