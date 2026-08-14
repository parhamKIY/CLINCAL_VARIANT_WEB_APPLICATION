"""Stable ReportData V4 contract for future DOCX and preview renderers."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Literal, TypedDict, cast

from backend.human_links import is_machine_readable_url
from backend.privacy import ClinicalDataPrivacyError, validate_llm_payload
from backend.references import (
    CanonicalReference,
    CanonicalReferenceError,
    validate_canonical_reference,
    validated_reference_url,
)


REPORT_DATA_SCHEMA_VERSION = "4.0"
MAX_REPORT_DATA_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20_000
MAX_SHORT_TEXT_CHARS = 500
MAX_LIST_ITEMS = 50
MAX_MAIN_FINDINGS = 20
MAX_EDIT_HISTORY = 200
MAX_ALLELE_CHARS = 10_000

HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")

AvailabilityStatus = Literal[
    "available",
    "no_match",
    "unsupported",
    "unavailable",
    "not_assessed",
]
PhenotypeConcordance = Literal[
    "supported",
    "partially_supported",
    "no_supported_association",
    "unavailable",
    "not_assessed",
]
ProviderRole = Literal["primary", "fallback"]
InterpretationStatus = Literal["available", "unavailable"]
ConflictStatus = Literal["none", "conflict", "unavailable"]
WarningSeverity = Literal[
    "INFO",
    "PARTIAL",
    "ACTION_REQUIRED",
    "BLOCKING",
]
ReviewStatus = Literal["draft", "confirmed"]

AVAILABILITY_STATUSES = frozenset(AvailabilityStatus.__args__)
PHENOTYPE_CONCORDANCE_VALUES = frozenset(PhenotypeConcordance.__args__)
PROVIDER_ROLES = frozenset(ProviderRole.__args__)
INTERPRETATION_STATUSES = frozenset(InterpretationStatus.__args__)
CONFLICT_STATUSES = frozenset(ConflictStatus.__args__)
WARNING_SEVERITIES = frozenset(WarningSeverity.__args__)
REVIEW_STATUSES = frozenset(ReviewStatus.__args__)
ZYGOSITY_VALUES = frozenset(
    {"heterozygous", "homozygous", "hemizygous", "mosaic"}
)
LITERATURE_IDENTIFIER_TYPES = frozenset({"PMID", "PMCID", "DOI"})


class ReportDataError(ValueError):
    """Raised when ReportData V4 violates its stable contract."""


class ReportVariantIdentity(TypedDict):
    """Stable allele identity; nullable display annotations stay explicit."""

    genome_build: str
    chromosome: str
    position: int
    reference: str
    alternate: str
    gene: str | None
    gene_id: str | None
    transcript: str | None
    hgvs_c: str | None
    hgvs_p: str | None
    zygosity: str | None


class ReportHPOTerm(TypedDict):
    """One accepted or matched locally validated HPO term."""

    hpo_id: str
    label: str | None


class ReportPhenotypeSummary(TypedDict):
    """Typed phenotype context including valid non-concordance."""

    accepted_hpo_terms: list[ReportHPOTerm]
    matched_hpo_terms: list[ReportHPOTerm]
    concordance: PhenotypeConcordance
    score: float | None
    summary: str | None
    evidence_status: AvailabilityStatus


class ReportConclusiveResult(TypedDict):
    """Prominent result-block values with explicit attribution."""

    gene: str | None
    hgvs_c: str | None
    hgvs_p: str | None
    zygosity: str | None
    classification: str | None
    classification_source: str | None
    status: AvailabilityStatus


class ReportCallQuality(TypedDict):
    """Typed input-call quality facts, separate from report prose."""

    qual: float | None
    filter: str | None
    status: Literal["passed", "not_evaluated", "failed"]
    acknowledged_at: str | None
    override_reason: str | None
    override_timestamp: str | None


class ReportPopulationFinding(TypedDict):
    """One exact-allele population observation or missingness state."""

    source: str
    dataset: str | None
    frequency: float | None
    status: AvailabilityStatus
    provider_role: ProviderRole
    fallback_used: bool


class ReportDiseaseAssociation(TypedDict):
    """One source-attributed disease/inheritance context."""

    source: str
    disease_name: str | None
    disease_id: str | None
    inheritance: str | None
    relationship_scope: str
    status: AvailabilityStatus


class ReportComputationalFinding(TypedDict):
    """One retained predictor result without classification inflation."""

    source: str
    tool: str
    prediction: str | None
    score: float | None
    status: AvailabilityStatus


class ReportStableIdentifier(TypedDict):
    """One exact-mapped stable variant identifier."""

    source: str
    identifier_type: str
    identifier: str | None
    status: AvailabilityStatus


class ReportClassificationFinding(TypedDict):
    """One source-attributed classification observation."""

    source: str
    classification: str | None
    review_status: str | None
    criteria: list[str]
    independent_evidence: bool
    status: AvailabilityStatus


class ReportMainFindings(TypedDict):
    """Typed values for the professor-style evidence table."""

    population_frequencies: list[ReportPopulationFinding]
    disease_associations: list[ReportDiseaseAssociation]
    computational_evidence: list[ReportComputationalFinding]
    stable_variant_ids: list[ReportStableIdentifier]
    classifications: list[ReportClassificationFinding]


class ReportInterpretationEdit(TypedDict):
    """Replayable edit to the reviewer interpretation narrative."""

    sequence: int
    old_value: str | None
    new_value: str | None
    timestamp: str
    reviewer_context: str | None


class ReportInterpretation(TypedDict):
    """Original and current interpretation plus exact generation provenance."""

    original_model_interpretation: str | None
    current_reviewer_interpretation: str | None
    model: str | None
    prompt_version: str | None
    generated_at: str | None
    edit_history: list[ReportInterpretationEdit]
    interpretation_status: InterpretationStatus
    failure_type: str | None
    conflict_assessment: str | None


class ReportClassificationSummary(TypedDict):
    """Source-aware classification context, not autonomous adjudication."""

    reviewer_confirmed_classification: str | None
    clinvar_classification: str | None
    automated_classification: str | None
    conflict_status: ConflictStatus
    conflict_severity: str | None
    source_attributions: list[str]
    independent_acmg_adjudication: bool
    summary: str | None


class ReportDataSource(TypedDict):
    """Database/tool provenance kept separate from literature."""

    source: str
    capability: str
    status: AvailabilityStatus
    operational_status: str | None
    provider_role: ProviderRole
    method: str | None
    record_identifier: str | None
    dataset: str | None
    human_url: str | None
    link_status: Literal["validated", "unavailable"]
    fallback_used: bool
    primary_failure: str | None
    retrieved_at: str | None


class ReportWarning(TypedDict):
    """One consequence-oriented report warning."""

    severity: WarningSeverity
    code: str
    message: str
    capability: str | None


class ReportProvenance(TypedDict):
    """Versions and source lineage needed for deterministic rendering."""

    source_report_schema_version: str
    evidence_schema_version: str
    interpretation_schema_version: str
    providers: list[str]
    upstream_sources: list[str]
    generated_at: str


class ReportSelectionRecord(TypedDict):
    """Replayable final-report inclusion decision."""

    sequence: int
    old_value: bool
    new_value: bool
    timestamp: str
    reviewer_context: str | None


class ReportReviewState(TypedDict):
    """Human review, selection, and confirmation state."""

    review_status: ReviewStatus
    include_in_final_report: bool
    confirmed_at: str | None
    reviewer_summary: str | None
    reviewer_notes: list[str]
    selection_history: list[ReportSelectionRecord]


class ReportData(TypedDict):
    """Renderer-neutral source of truth for one allele-level report."""

    schema_version: str
    report_id: str
    analysis_id: str
    input_index: int
    variant_identity: ReportVariantIdentity
    phenotype_summary: ReportPhenotypeSummary
    conclusive_result: ReportConclusiveResult
    call_quality: ReportCallQuality
    main_findings: ReportMainFindings
    interpretation: ReportInterpretation
    classification_summary: ReportClassificationSummary
    literature_references: list[CanonicalReference]
    data_sources: list[ReportDataSource]
    warnings: list[ReportWarning]
    provenance: ReportProvenance
    review_state: ReportReviewState
    template_version: str


def _fields(type_: type[TypedDict]) -> frozenset[str]:
    return frozenset(type_.__required_keys__)


REPORT_DATA_FIELDS = _fields(ReportData)
VARIANT_IDENTITY_FIELDS = _fields(ReportVariantIdentity)
HPO_TERM_FIELDS = _fields(ReportHPOTerm)
PHENOTYPE_SUMMARY_FIELDS = _fields(ReportPhenotypeSummary)
CONCLUSIVE_RESULT_FIELDS = _fields(ReportConclusiveResult)
CALL_QUALITY_FIELDS = _fields(ReportCallQuality)
MAIN_FINDINGS_FIELDS = _fields(ReportMainFindings)
POPULATION_FINDING_FIELDS = _fields(ReportPopulationFinding)
DISEASE_ASSOCIATION_FIELDS = _fields(ReportDiseaseAssociation)
COMPUTATIONAL_FINDING_FIELDS = _fields(ReportComputationalFinding)
STABLE_IDENTIFIER_FIELDS = _fields(ReportStableIdentifier)
CLASSIFICATION_FINDING_FIELDS = _fields(ReportClassificationFinding)
INTERPRETATION_FIELDS = _fields(ReportInterpretation)
INTERPRETATION_EDIT_FIELDS = _fields(ReportInterpretationEdit)
CLASSIFICATION_SUMMARY_FIELDS = _fields(ReportClassificationSummary)
DATA_SOURCE_FIELDS = _fields(ReportDataSource)
WARNING_FIELDS = _fields(ReportWarning)
PROVENANCE_FIELDS = _fields(ReportProvenance)
REVIEW_STATE_FIELDS = _fields(ReportReviewState)
SELECTION_RECORD_FIELDS = _fields(ReportSelectionRecord)


def _mapping(
    value: object,
    fields: frozenset[str],
    path: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReportDataError(f"{path} has invalid fields.")
    return value


def _text(
    value: object,
    path: str,
    *,
    optional: bool = False,
    maximum: int = MAX_SHORT_TEXT_CHARS,
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReportDataError(f"{path} must be non-empty text.")
    normalized = value.strip()
    if len(normalized) > maximum or any(
        ord(char) < 32 and char not in {"\n", "\r", "\t"}
        for char in normalized
    ):
        raise ReportDataError(f"{path} is invalid or too long.")
    return normalized


def _enum(value: object, allowed: frozenset[str], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ReportDataError(f"{path} is invalid.")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ReportDataError(f"{path} must be boolean.")
    return value


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReportDataError(f"{path} is invalid.")
    return value


def _number(
    value: object,
    path: str,
    *,
    optional: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportDataError(f"{path} must be numeric.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ReportDataError(f"{path} must be finite.")
    if minimum is not None and normalized < minimum:
        raise ReportDataError(f"{path} is below its minimum.")
    if maximum is not None and normalized > maximum:
        raise ReportDataError(f"{path} is above its maximum.")
    return normalized


def _sequence(value: object, path: str, *, maximum: int) -> Sequence[object]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > maximum
    ):
        raise ReportDataError(f"{path} must be a bounded list.")
    return value


def _timestamp(value: object, path: str, *, optional: bool = False) -> None:
    normalized = _text(value, path, optional=optional, maximum=64)
    if normalized is None:
        return
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportDataError(f"{path} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ReportDataError(f"{path} must include a timezone.")


def _optional_zygosity(value: object, path: str) -> None:
    if value is None:
        return
    _enum(value, ZYGOSITY_VALUES, path)


def _text_list(value: object, path: str) -> None:
    for index, item in enumerate(
        _sequence(value, path, maximum=MAX_LIST_ITEMS)
    ):
        _text(item, f"{path}[{index}]")


def _validate_variant_identity(value: object) -> None:
    item = _mapping(value, VARIANT_IDENTITY_FIELDS, "variant_identity")
    _enum(
        item["genome_build"],
        frozenset({"GRCh37", "GRCh38"}),
        "variant_identity.genome_build",
    )
    _text(item["chromosome"], "variant_identity.chromosome", maximum=32)
    _integer(item["position"], "variant_identity.position", minimum=1)
    _text(item["reference"], "variant_identity.reference", maximum=MAX_ALLELE_CHARS)
    _text(item["alternate"], "variant_identity.alternate", maximum=MAX_ALLELE_CHARS)
    for field in ("gene", "gene_id", "transcript", "hgvs_c", "hgvs_p"):
        _text(item[field], f"variant_identity.{field}", optional=True)
    _optional_zygosity(item["zygosity"], "variant_identity.zygosity")


def _validate_hpo_terms(value: object, path: str) -> set[str]:
    seen: set[str] = set()
    for index, raw in enumerate(
        _sequence(value, path, maximum=MAX_LIST_ITEMS)
    ):
        item_path = f"{path}[{index}]"
        item = _mapping(raw, HPO_TERM_FIELDS, item_path)
        hpo_id = cast(str, _text(item["hpo_id"], f"{item_path}.hpo_id", maximum=10))
        if not HPO_ID_PATTERN.fullmatch(hpo_id) or hpo_id in seen:
            raise ReportDataError(f"{item_path}.hpo_id is invalid or duplicated.")
        seen.add(hpo_id)
        _text(item["label"], f"{item_path}.label", optional=True)
    return seen


def _validate_phenotype_summary(value: object) -> None:
    item = _mapping(value, PHENOTYPE_SUMMARY_FIELDS, "phenotype_summary")
    accepted = _validate_hpo_terms(
        item["accepted_hpo_terms"],
        "phenotype_summary.accepted_hpo_terms",
    )
    matched = _validate_hpo_terms(
        item["matched_hpo_terms"],
        "phenotype_summary.matched_hpo_terms",
    )
    if not matched.issubset(accepted):
        raise ReportDataError(
            "phenotype_summary.matched_hpo_terms must be accepted terms."
        )
    _enum(
        item["concordance"],
        PHENOTYPE_CONCORDANCE_VALUES,
        "phenotype_summary.concordance",
    )
    _number(
        item["score"],
        "phenotype_summary.score",
        optional=True,
        minimum=0.0,
        maximum=1.0,
    )
    _text(
        item["summary"],
        "phenotype_summary.summary",
        optional=True,
        maximum=MAX_TEXT_CHARS,
    )
    _enum(
        item["evidence_status"],
        AVAILABILITY_STATUSES,
        "phenotype_summary.evidence_status",
    )


def _validate_conclusive_result(value: object) -> None:
    item = _mapping(value, CONCLUSIVE_RESULT_FIELDS, "conclusive_result")
    for field in (
        "gene",
        "hgvs_c",
        "hgvs_p",
        "classification",
        "classification_source",
    ):
        _text(item[field], f"conclusive_result.{field}", optional=True)
    _optional_zygosity(item["zygosity"], "conclusive_result.zygosity")
    status = _enum(item["status"], AVAILABILITY_STATUSES, "conclusive_result.status")
    if status == "available" and (
        item["classification"] is None or item["classification_source"] is None
    ):
        raise ReportDataError(
            "Available conclusive_result requires classification and source."
        )


def _validate_source_only_classification(value: Mapping[str, object]) -> None:
    """Keep provider observations out of the application's result block."""

    conclusive = value["conclusive_result"]
    summary = value["classification_summary"]
    if not isinstance(conclusive, Mapping) or not isinstance(summary, Mapping):
        return
    if summary["independent_acmg_adjudication"] is False and (
        conclusive["classification"] is not None
        or conclusive["classification_source"] is not None
        or conclusive["status"] != "not_assessed"
    ):
        raise ReportDataError(
            "Source-attributed classifications cannot populate conclusive_result."
        )


def _validate_call_quality(value: object) -> None:
    item = _mapping(value, CALL_QUALITY_FIELDS, "call_quality")
    _number(item["qual"], "call_quality.qual", optional=True)
    _text(item["filter"], "call_quality.filter", optional=True)
    status = _enum(
        item["status"],
        frozenset({"passed", "not_evaluated", "failed"}),
        "call_quality.status",
    )
    _timestamp(item["acknowledged_at"], "call_quality.acknowledged_at", optional=True)
    reason = _text(
        item["override_reason"], "call_quality.override_reason", optional=True
    )
    _timestamp(
        item["override_timestamp"],
        "call_quality.override_timestamp",
        optional=True,
    )
    acknowledged = item["acknowledged_at"] is not None
    has_override_timestamp = item["override_timestamp"] is not None
    if (reason is None) == has_override_timestamp:
        raise ReportDataError("Call-quality override fields are inconsistent.")
    if status == "passed" and (acknowledged or reason is not None):
        raise ReportDataError("Passed calls cannot have reviewer exceptions.")
    if status == "not_evaluated" and reason is not None:
        raise ReportDataError("Only failed calls can have an override.")
    if status == "failed" and acknowledged:
        raise ReportDataError("Failed calls require an override, not acknowledgement.")


def _validate_population_findings(value: object) -> None:
    for index, raw in enumerate(
        _sequence(
            value,
            "main_findings.population_frequencies",
            maximum=MAX_MAIN_FINDINGS,
        )
    ):
        path = f"main_findings.population_frequencies[{index}]"
        item = _mapping(raw, POPULATION_FINDING_FIELDS, path)
        _text(item["source"], f"{path}.source")
        _text(item["dataset"], f"{path}.dataset", optional=True)
        frequency = _number(
            item["frequency"],
            f"{path}.frequency",
            optional=True,
            minimum=0.0,
            maximum=1.0,
        )
        status = _enum(item["status"], AVAILABILITY_STATUSES, f"{path}.status")
        role = _enum(item["provider_role"], PROVIDER_ROLES, f"{path}.provider_role")
        fallback = _boolean(item["fallback_used"], f"{path}.fallback_used")
        if status == "available" and frequency is None:
            raise ReportDataError(f"{path} available status requires frequency.")
        if fallback != (role == "fallback"):
            raise ReportDataError(f"{path} fallback provenance is inconsistent.")


def _validate_disease_associations(value: object) -> None:
    for index, raw in enumerate(
        _sequence(
            value,
            "main_findings.disease_associations",
            maximum=MAX_MAIN_FINDINGS,
        )
    ):
        path = f"main_findings.disease_associations[{index}]"
        item = _mapping(raw, DISEASE_ASSOCIATION_FIELDS, path)
        _text(item["source"], f"{path}.source")
        name = _text(item["disease_name"], f"{path}.disease_name", optional=True)
        identifier = _text(item["disease_id"], f"{path}.disease_id", optional=True)
        _text(item["inheritance"], f"{path}.inheritance", optional=True)
        _text(item["relationship_scope"], f"{path}.relationship_scope")
        status = _enum(item["status"], AVAILABILITY_STATUSES, f"{path}.status")
        if status == "available" and name is None and identifier is None:
            raise ReportDataError(f"{path} requires a disease name or identifier.")


def _validate_computational_findings(value: object) -> None:
    for index, raw in enumerate(
        _sequence(
            value,
            "main_findings.computational_evidence",
            maximum=MAX_MAIN_FINDINGS,
        )
    ):
        path = f"main_findings.computational_evidence[{index}]"
        item = _mapping(raw, COMPUTATIONAL_FINDING_FIELDS, path)
        _text(item["source"], f"{path}.source")
        _text(item["tool"], f"{path}.tool")
        prediction = _text(item["prediction"], f"{path}.prediction", optional=True)
        score = _number(item["score"], f"{path}.score", optional=True)
        status = _enum(item["status"], AVAILABILITY_STATUSES, f"{path}.status")
        if status == "available" and prediction is None and score is None:
            raise ReportDataError(f"{path} requires a prediction or score.")


def _validate_stable_identifiers(value: object) -> None:
    for index, raw in enumerate(
        _sequence(value, "main_findings.stable_variant_ids", maximum=MAX_MAIN_FINDINGS)
    ):
        path = f"main_findings.stable_variant_ids[{index}]"
        item = _mapping(raw, STABLE_IDENTIFIER_FIELDS, path)
        _text(item["source"], f"{path}.source")
        _text(item["identifier_type"], f"{path}.identifier_type")
        identifier = _text(item["identifier"], f"{path}.identifier", optional=True)
        status = _enum(item["status"], AVAILABILITY_STATUSES, f"{path}.status")
        if status == "available" and identifier is None:
            raise ReportDataError(f"{path} available status requires identifier.")


def _validate_classification_findings(value: object) -> None:
    for index, raw in enumerate(
        _sequence(value, "main_findings.classifications", maximum=MAX_MAIN_FINDINGS)
    ):
        path = f"main_findings.classifications[{index}]"
        item = _mapping(raw, CLASSIFICATION_FINDING_FIELDS, path)
        _text(item["source"], f"{path}.source")
        classification = _text(
            item["classification"], f"{path}.classification", optional=True
        )
        _text(item["review_status"], f"{path}.review_status", optional=True)
        _text_list(item["criteria"], f"{path}.criteria")
        _boolean(item["independent_evidence"], f"{path}.independent_evidence")
        status = _enum(item["status"], AVAILABILITY_STATUSES, f"{path}.status")
        if status == "available" and classification is None:
            raise ReportDataError(f"{path} available status requires classification.")


def _validate_main_findings(value: object) -> None:
    item = _mapping(value, MAIN_FINDINGS_FIELDS, "main_findings")
    _validate_population_findings(item["population_frequencies"])
    _validate_disease_associations(item["disease_associations"])
    _validate_computational_findings(item["computational_evidence"])
    _validate_stable_identifiers(item["stable_variant_ids"])
    _validate_classification_findings(item["classifications"])


def _validate_interpretation(value: object) -> None:
    item = _mapping(value, INTERPRETATION_FIELDS, "interpretation")
    original = _text(
        item["original_model_interpretation"],
        "interpretation.original_model_interpretation",
        optional=True,
        maximum=MAX_TEXT_CHARS,
    )
    current = _text(
        item["current_reviewer_interpretation"],
        "interpretation.current_reviewer_interpretation",
        optional=True,
        maximum=MAX_TEXT_CHARS,
    )
    for field in ("model", "prompt_version", "failure_type"):
        _text(item[field], f"interpretation.{field}", optional=True)
    _timestamp(item["generated_at"], "interpretation.generated_at", optional=True)
    _text(
        item["conflict_assessment"],
        "interpretation.conflict_assessment",
        optional=True,
        maximum=MAX_TEXT_CHARS,
    )
    status = _enum(
        item["interpretation_status"],
        INTERPRETATION_STATUSES,
        "interpretation.interpretation_status",
    )
    if status == "available" and current is None:
        raise ReportDataError("Available interpretation requires current text.")

    replayed = original
    edits = _sequence(
        item["edit_history"],
        "interpretation.edit_history",
        maximum=MAX_EDIT_HISTORY,
    )
    for index, raw in enumerate(edits):
        path = f"interpretation.edit_history[{index}]"
        edit = _mapping(raw, INTERPRETATION_EDIT_FIELDS, path)
        if _integer(edit["sequence"], f"{path}.sequence", minimum=1) != index + 1:
            raise ReportDataError(f"{path}.sequence is not contiguous.")
        old_value = _text(
            edit["old_value"],
            f"{path}.old_value",
            optional=True,
            maximum=MAX_TEXT_CHARS,
        )
        new_value = _text(
            edit["new_value"],
            f"{path}.new_value",
            optional=True,
            maximum=MAX_TEXT_CHARS,
        )
        _timestamp(edit["timestamp"], f"{path}.timestamp")
        _text(edit["reviewer_context"], f"{path}.reviewer_context", optional=True)
        if old_value != replayed:
            raise ReportDataError(f"{path} does not replay from the prior value.")
        replayed = new_value
    if replayed != current:
        raise ReportDataError(
            "Interpretation edit history does not match current text."
        )


def _validate_classification_summary(value: object) -> None:
    item = _mapping(
        value, CLASSIFICATION_SUMMARY_FIELDS, "classification_summary"
    )
    for field in (
        "reviewer_confirmed_classification",
        "clinvar_classification",
        "automated_classification",
        "conflict_severity",
    ):
        _text(item[field], f"classification_summary.{field}", optional=True)
    _enum(
        item["conflict_status"],
        CONFLICT_STATUSES,
        "classification_summary.conflict_status",
    )
    _text_list(
        item["source_attributions"],
        "classification_summary.source_attributions",
    )
    independent = _boolean(
        item["independent_acmg_adjudication"],
        "classification_summary.independent_acmg_adjudication",
    )
    if independent:
        raise ReportDataError(
            "ReportData V4 does not support independent ACMG adjudication."
        )
    _text(
        item["summary"],
        "classification_summary.summary",
        optional=True,
        maximum=MAX_TEXT_CHARS,
    )


def _validate_literature_references(value: object) -> None:
    seen: set[str] = set()
    for index, raw in enumerate(
        _sequence(value, "literature_references", maximum=MAX_LIST_ITEMS)
    ):
        try:
            reference = validate_canonical_reference(raw)
        except CanonicalReferenceError as exc:
            raise ReportDataError(
                f"literature_references[{index}] is invalid."
            ) from exc
        if reference["identifier_type"] not in LITERATURE_IDENTIFIER_TYPES:
            raise ReportDataError(
                f"literature_references[{index}] is not a literature record."
            )
        if reference["reference_id"] in seen:
            raise ReportDataError("Literature reference IDs must be unique.")
        seen.add(reference["reference_id"])


def _validate_data_sources(value: object) -> None:
    for index, raw in enumerate(
        _sequence(value, "data_sources", maximum=MAX_LIST_ITEMS)
    ):
        path = f"data_sources[{index}]"
        item = _mapping(raw, DATA_SOURCE_FIELDS, path)
        source = _text(item["source"], f"{path}.source")
        _text(item["capability"], f"{path}.capability")
        _enum(item["status"], AVAILABILITY_STATUSES, f"{path}.status")
        _text(item["operational_status"], f"{path}.operational_status", optional=True)
        role = _enum(item["provider_role"], PROVIDER_ROLES, f"{path}.provider_role")
        for field in ("method", "record_identifier", "dataset", "primary_failure"):
            _text(item[field], f"{path}.{field}", optional=True)
        url = _text(
            item["human_url"],
            f"{path}.human_url",
            optional=True,
            maximum=2_000,
        )
        link_status = _enum(
            item["link_status"],
            frozenset({"validated", "unavailable"}),
            f"{path}.link_status",
        )
        fallback = _boolean(item["fallback_used"], f"{path}.fallback_used")
        _timestamp(item["retrieved_at"], f"{path}.retrieved_at", optional=True)
        if fallback != (role == "fallback"):
            raise ReportDataError(f"{path} fallback provenance is inconsistent.")
        if fallback and item["primary_failure"] is None:
            raise ReportDataError(f"{path} fallback requires primary_failure.")
        if url is None and link_status != "unavailable":
            raise ReportDataError(f"{path} link status is inconsistent.")
        if url is not None and (
            link_status != "validated" or validated_reference_url(url) != url
        ):
            raise ReportDataError(f"{path} human URL is not validated.")
        if url is not None and is_machine_readable_url(source, url):
            raise ReportDataError(f"{path} human URL is machine-readable.")


def _validate_warnings(value: object) -> None:
    for index, raw in enumerate(
        _sequence(value, "warnings", maximum=MAX_LIST_ITEMS)
    ):
        path = f"warnings[{index}]"
        item = _mapping(raw, WARNING_FIELDS, path)
        _enum(item["severity"], WARNING_SEVERITIES, f"{path}.severity")
        _text(item["code"], f"{path}.code")
        _text(item["message"], f"{path}.message", maximum=MAX_TEXT_CHARS)
        _text(item["capability"], f"{path}.capability", optional=True)


def _validate_provenance(value: object) -> None:
    item = _mapping(value, PROVENANCE_FIELDS, "provenance")
    for field in (
        "source_report_schema_version",
        "evidence_schema_version",
        "interpretation_schema_version",
    ):
        _text(item[field], f"provenance.{field}", maximum=32)
    _text_list(item["providers"], "provenance.providers")
    _text_list(item["upstream_sources"], "provenance.upstream_sources")
    _timestamp(item["generated_at"], "provenance.generated_at")


def _validate_review_state(value: object) -> None:
    item = _mapping(value, REVIEW_STATE_FIELDS, "review_state")
    status = _enum(item["review_status"], REVIEW_STATUSES, "review_state.review_status")
    included = _boolean(
        item["include_in_final_report"],
        "review_state.include_in_final_report",
    )
    _timestamp(item["confirmed_at"], "review_state.confirmed_at", optional=True)
    _text(
        item["reviewer_summary"],
        "review_state.reviewer_summary",
        optional=True,
        maximum=MAX_TEXT_CHARS,
    )
    _text_list(item["reviewer_notes"], "review_state.reviewer_notes")
    if (status == "confirmed") != (item["confirmed_at"] is not None):
        raise ReportDataError("Review status and confirmation time are inconsistent.")

    replayed = True
    history = _sequence(
        item["selection_history"],
        "review_state.selection_history",
        maximum=MAX_EDIT_HISTORY,
    )
    for index, raw in enumerate(history):
        path = f"review_state.selection_history[{index}]"
        record = _mapping(raw, SELECTION_RECORD_FIELDS, path)
        if _integer(record["sequence"], f"{path}.sequence", minimum=1) != index + 1:
            raise ReportDataError(f"{path}.sequence is not contiguous.")
        old_value = _boolean(record["old_value"], f"{path}.old_value")
        new_value = _boolean(record["new_value"], f"{path}.new_value")
        _timestamp(record["timestamp"], f"{path}.timestamp")
        _text(record["reviewer_context"], f"{path}.reviewer_context", optional=True)
        if old_value != replayed or old_value == new_value:
            raise ReportDataError(f"{path} does not replay a real selection change.")
        replayed = new_value
    if replayed != included:
        raise ReportDataError("Selection history does not match current inclusion.")


def validate_report_data(value: object) -> ReportData:
    """Validate and copy one renderer-neutral ReportData V4 record."""

    legacy_fields = REPORT_DATA_FIELDS - {"call_quality"}
    fields = REPORT_DATA_FIELDS if (
        isinstance(value, Mapping) and "call_quality" in value
    ) else legacy_fields
    item = _mapping(value, fields, "report_data")
    if item["schema_version"] != REPORT_DATA_SCHEMA_VERSION:
        raise ReportDataError("report_data.schema_version is unsupported.")
    _text(item["report_id"], "report_data.report_id", maximum=128)
    _text(item["analysis_id"], "report_data.analysis_id", maximum=128)
    _integer(item["input_index"], "report_data.input_index")
    _validate_variant_identity(item["variant_identity"])
    _validate_phenotype_summary(item["phenotype_summary"])
    _validate_conclusive_result(item["conclusive_result"])
    if "call_quality" in item:
        _validate_call_quality(item["call_quality"])
    _validate_main_findings(item["main_findings"])
    _validate_interpretation(item["interpretation"])
    _validate_classification_summary(item["classification_summary"])
    _validate_source_only_classification(item)
    _validate_literature_references(item["literature_references"])
    _validate_data_sources(item["data_sources"])
    _validate_warnings(item["warnings"])
    _validate_provenance(item["provenance"])
    _validate_review_state(item["review_state"])
    _text(item["template_version"], "report_data.template_version", maximum=64)

    try:
        validate_llm_payload(item)
    except ClinicalDataPrivacyError as exc:
        raise ReportDataError(
            "ReportData contains prohibited clinical data."
        ) from exc
    try:
        serialized = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReportDataError("ReportData must be JSON-safe.") from exc
    if len(serialized) > MAX_REPORT_DATA_BYTES:
        raise ReportDataError("ReportData exceeds the serialized size limit.")
    return deepcopy(cast(ReportData, item))


__all__ = [
    "AVAILABILITY_STATUSES",
    "PHENOTYPE_CONCORDANCE_VALUES",
    "REPORT_DATA_SCHEMA_VERSION",
    "ReportData",
    "ReportDataError",
    "validate_report_data",
]
