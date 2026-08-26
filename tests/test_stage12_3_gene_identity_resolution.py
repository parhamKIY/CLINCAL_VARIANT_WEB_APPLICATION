from __future__ import annotations

import json
from copy import deepcopy

from backend.annotation import annotate_variants
from backend.execution_trace import (
    AnalysisExecutionTrace,
    bind_execution_trace,
    reset_execution_trace,
)
from backend.gene_identity import (
    GENE_IDENTITY_UNRESOLVED,
    evidence_has_resolved_gene_identity,
)
from backend.llm import LLMClient, LLMResponse
from backend.report import build_evidence_object
from backend.variant_interpretation import interpret_variants
from tests.test_pipeline import (
    FakeLLMAdapter,
    FakeResponse,
    FakeSession,
    TestAnnotation as AnnotationFactory,
    TestEvidenceObject as EvidenceFactory,
)


def _valid_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "ai_classification": "Uncertain significance",
                "interpretation": "The available evidence supports cautious review.",
                "conflict_assessment": "No material conflict was identified.",
                "phenotype_conclusion": "partially supported",
                "warnings": [],
            }
        ),
        model="stage12-3-test-model",
        finish_reason="stop",
    )


def _vep_without_gene() -> dict[str, object]:
    response = deepcopy(AnnotationFactory._vep_response())
    consequences = response["transcript_consequences"]
    assert isinstance(consequences, list)
    consequence = consequences[0]
    assert isinstance(consequence, dict)
    consequence["gene_symbol"] = None
    consequence["gene_id"] = None
    return response


def _annotate(
    vep_response: dict[str, object],
    *,
    variantvalidator_response: object | None = None,
    genebe_response: dict[str, object] | None = None,
) -> tuple[dict[str, object], FakeSession]:
    session = FakeSession(
        [FakeResponse(200, [vep_response])],
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
        [AnnotationFactory._variant()],
        session=session,  # type: ignore[arg-type]
        max_retries=0,
        use_cache=False,
    )[0]
    return annotation, session


def test_complete_vep_gene_context_is_accepted_and_llm_proceeds() -> None:
    annotation, session = _annotate(AnnotationFactory._vep_response())
    evidence = build_evidence_object(annotation)
    adapter = FakeLLMAdapter(_valid_response())

    results = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        max_retries=0,
    )

    assert session.variantvalidator_get_calls == []
    assert annotation["gene_identity_resolution"]["status"] == "resolved"
    assert annotation["gene_identity_resolution"]["source"] == "Ensembl VEP"
    assert evidence_has_resolved_gene_identity(evidence) is True
    assert results[0]["status"] == "success"
    assert len(adapter.requests) == 1


def test_incomplete_successful_vep_triggers_variantvalidator_verification() -> None:
    annotation, session = _annotate(
        _vep_without_gene(),
        variantvalidator_response=FakeResponse(
            200,
            AnnotationFactory._variantvalidator_response(),
        ),
    )

    verification = annotation["sources"]["vep"]["gene_identity_verification"]
    assert len(session.variantvalidator_get_calls) == 1
    assert verification["trigger"] == "vep_gene_context_unresolved"
    assert verification["status"] == "success"


def test_exact_variantvalidator_match_promotes_gene_with_provenance() -> None:
    annotation, _ = _annotate(
        _vep_without_gene(),
        variantvalidator_response=FakeResponse(
            200,
            AnnotationFactory._variantvalidator_response(),
        ),
    )
    evidence = build_evidence_object(annotation)

    assert annotation["gene_identity_resolution"]["source"] == "VariantValidator"
    assert evidence["gene"] == "GENE1"
    assert evidence["annotation_promotion"]["fields"]["gene"]["source"] == (
        "VariantValidator"
    )
    assert evidence_has_resolved_gene_identity(evidence) is True


def test_incomplete_vep_gene_is_retained_only_after_exact_verification() -> None:
    response = deepcopy(AnnotationFactory._vep_response())
    consequences = response["transcript_consequences"]
    assert isinstance(consequences, list)
    consequence = consequences[0]
    assert isinstance(consequence, dict)
    consequence["hgvsc"] = None
    annotation, _ = _annotate(
        response,
        variantvalidator_response=FakeResponse(
            200,
            AnnotationFactory._variantvalidator_response(),
        ),
    )
    evidence = build_evidence_object(annotation)

    resolution = annotation["gene_identity_resolution"]
    assert resolution["source"] == "Ensembl VEP"
    assert resolution["reason"] == "vep_primary_verified_by_variantvalidator"
    assert resolution["provenance"]["verified_by"] == "VariantValidator"
    assert evidence_has_resolved_gene_identity(evidence) is True


def test_variantvalidator_identity_mismatch_is_rejected_without_gene_fabrication() -> None:
    annotation, _ = _annotate(
        _vep_without_gene(),
        variantvalidator_response=FakeResponse(
            200,
            AnnotationFactory._variantvalidator_response(position=101),
        ),
    )
    evidence = build_evidence_object(annotation)

    assert annotation["gene_identity_resolution"]["status"] == "unresolved"
    assert annotation["gene_identity_resolution"]["reason"] == (
        GENE_IDENTITY_UNRESOLVED
    )
    assert annotation["gene"] is None
    assert evidence["gene"] is None
    assert evidence_has_resolved_gene_identity(evidence) is False


def test_exact_coherent_genebe_bundle_resolves_gene_identity() -> None:
    annotation, _ = _annotate(
        _vep_without_gene(),
        variantvalidator_response=FakeResponse(503, {"error": "unavailable"}),
        genebe_response=AnnotationFactory._genebe_variant_response(),
    )
    evidence = build_evidence_object(annotation)

    assert annotation["gene_identity_resolution"]["status"] == "resolved"
    assert annotation["gene_identity_resolution"]["source"] == "GeneBe"
    assert evidence["gene"] == "GENE1"
    assert evidence["annotation_promotion"]["fields"]["gene"]["source"] == "GeneBe"
    assert evidence_has_resolved_gene_identity(evidence) is True


def test_unresolved_gene_is_variant_local_and_sibling_still_completes() -> None:
    unresolved = EvidenceFactory._complete_evidence_object()
    unresolved["gene"] = None
    unresolved["variant_context"]["gene"] = None
    unresolved["annotations"]["vep"]["gene"] = None
    resolved = EvidenceFactory._complete_evidence_object()
    adapter = FakeLLMAdapter(_valid_response())

    results = interpret_variants(
        [unresolved, resolved],
        client=LLMClient(adapter),
        max_retries=0,
    )

    assert [item["status"] for item in results] == ["failed", "success"]
    assert results[0]["error_type"] == GENE_IDENTITY_UNRESOLVED
    assert results[1]["interpretation"] is not None
    assert len(adapter.requests) == 1


def test_llm_never_receives_an_unresolved_gene_variant() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    evidence["gene"] = None
    evidence["variant_context"]["gene"] = None
    evidence["annotations"]["vep"]["gene"] = None
    adapter = FakeLLMAdapter(_valid_response())

    trace = AnalysisExecutionTrace()
    token = bind_execution_trace(trace, "run-00000000000000000000000000000001")
    try:
        result = interpret_variants(
            [evidence],
            client=LLMClient(adapter),
            max_retries=0,
        )[0]
    finally:
        reset_execution_trace(token)

    assert result["status"] == "failed"
    assert result["error_type"] == GENE_IDENTITY_UNRESOLVED
    assert adapter.requests == []
    failed_event = trace.snapshot()["events"][-1]
    assert failed_event["event_type"] == "variant_interpretation_failed"
    assert failed_event["reason_category"] == GENE_IDENTITY_UNRESOLVED


def test_historical_provenance_backed_evidence_remains_interpretable() -> None:
    evidence = EvidenceFactory._complete_evidence_object()
    assert "gene_identity_resolution" not in evidence["annotations"]["vep"]
    adapter = FakeLLMAdapter(_valid_response())

    result = interpret_variants(
        [evidence],
        client=LLMClient(adapter),
        max_retries=0,
    )[0]

    assert result["status"] == "success"
    assert len(adapter.requests) == 1
