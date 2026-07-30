"""Evidence-object contracts for downstream interpretation and reporting."""

import json
import math
import re
from collections.abc import Iterable
from typing import Any, TypedDict, cast
from urllib.parse import urlsplit


EVIDENCE_SCHEMA_VERSION = "1.0"
HPO_ID_PATTERN = re.compile(r"HP:[0-9]{7}")
GENOME_ASSEMBLIES = {"GRCh37", "GRCh38"}
SOURCE_STATUS_VALUES = {
    "pending",
    "success",
    "not_found",
    "not_applicable",
    "unsupported",
    "error",
}


class EvidenceObjectError(ValueError):
    """Raised when an evidence object violates the Stage 7 contract."""


class EvidenceVariant(TypedDict):
    """Minimal allele representation without sample or raw VCF data."""

    chrom: str
    pos: int
    ref: str
    alt: str


class EvidenceReference(TypedDict):
    """Human-readable provenance link for one evidence source."""

    source: str
    url: str


class EvidenceClinGenCuration(TypedDict):
    """Compact ClinGen gene-disease validity evidence."""

    disease: str
    disease_id: str | None
    classification: str
    mode_of_inheritance: str | None
    pmids: list[str]
    report_url: str | None


class EvidenceSourceStatuses(TypedDict):
    """Availability state of each Stage 5 external source."""

    vep: str
    myvariant: str
    clinvar: str
    clingen: str


class EvidenceObject(TypedDict):
    """Versioned, JSON-safe evidence supplied to later stages."""

    schema_version: str
    variant: EvidenceVariant
    assembly: str
    gene: str | None
    gene_id: str | None
    transcript: str | None
    consequence: str | None
    impact: str | None
    protein_change: str | None
    population_frequency: float | None
    clinvar_accession: str | None
    clinvar_significance: str | None
    clinvar_review_status: str | None
    clinvar_conditions: list[str]
    clingen_curations: list[EvidenceClinGenCuration]
    phenotype_score: float | None
    hpo_terms: list[str]
    matched_hpo_terms: list[str]
    source_statuses: EvidenceSourceStatuses
    references: list[EvidenceReference]
    warnings: list[str]


EVIDENCE_OBJECT_FIELDS = frozenset(EvidenceObject.__required_keys__)
EVIDENCE_VARIANT_FIELDS = frozenset(EvidenceVariant.__required_keys__)
EVIDENCE_REFERENCE_FIELDS = frozenset(EvidenceReference.__required_keys__)
EVIDENCE_CLINGEN_FIELDS = frozenset(
    EvidenceClinGenCuration.__required_keys__
)
EVIDENCE_SOURCE_STATUS_FIELDS = frozenset(
    EvidenceSourceStatuses.__required_keys__
)
EVIDENCE_SOURCE_NAMES = ("vep", "myvariant", "clinvar", "clingen")


def _validate_exact_fields(
    value: dict[object, object],
    expected_fields: frozenset[str],
    path: str,
) -> None:
    """Reject omitted fields and unapproved raw or personal data."""
    actual_fields = set(value)
    missing_fields = expected_fields - actual_fields
    extra_fields = actual_fields - expected_fields
    if missing_fields:
        raise EvidenceObjectError(
            f"{path} is missing required fields: "
            f"{', '.join(sorted(missing_fields))}."
        )
    if extra_fields:
        raise EvidenceObjectError(
            f"{path} contains unsupported fields: "
            f"{', '.join(sorted(str(field) for field in extra_fields))}."
        )


def _validate_required_string(value: object, path: str) -> str:
    """Return one non-empty string or raise a field-specific error."""
    if not isinstance(value, str) or not value.strip():
        raise EvidenceObjectError(
            f"{path} must be a non-empty string."
        )
    return value


def _validate_optional_string(value: object, path: str) -> None:
    """Allow an explicit null or a non-empty string."""
    if value is not None:
        _validate_required_string(value, path)


def _validate_unique_strings(
    value: object,
    path: str,
) -> list[str]:
    """Validate a JSON list of unique, non-empty strings."""
    if not isinstance(value, list):
        raise EvidenceObjectError(f"{path} must be a list.")

    validated: list[str] = []
    for index, item in enumerate(value):
        validated.append(
            _validate_required_string(item, f"{path}[{index}]")
        )
    if len(set(validated)) != len(validated):
        raise EvidenceObjectError(
            f"{path} must not contain duplicate values."
        )
    return validated


def _validate_probability(
    value: object,
    path: str,
) -> None:
    """Validate an explicit null or one finite value from zero to one."""
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise EvidenceObjectError(
            f"{path} must be null or a finite number from 0.0 to 1.0."
        )


def _validate_url(value: object, path: str) -> str:
    """Validate one absolute HTTP(S) provenance URL."""
    url = _validate_required_string(value, path)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise EvidenceObjectError(
            f"{path} must be an absolute HTTP(S) URL."
        ) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EvidenceObjectError(
            f"{path} must be an absolute HTTP(S) URL."
        )
    return url


def _validate_variant(value: object) -> None:
    """Validate the minimal allele object and exclude raw VCF fields."""
    if not isinstance(value, dict):
        raise EvidenceObjectError("evidence.variant must be a dictionary.")
    _validate_exact_fields(
        value,
        EVIDENCE_VARIANT_FIELDS,
        "evidence.variant",
    )
    _validate_required_string(value["chrom"], "evidence.variant.chrom")
    if (
        isinstance(value["pos"], bool)
        or not isinstance(value["pos"], int)
        or value["pos"] <= 0
    ):
        raise EvidenceObjectError(
            "evidence.variant.pos must be a positive integer."
        )
    _validate_required_string(value["ref"], "evidence.variant.ref")
    _validate_required_string(value["alt"], "evidence.variant.alt")


def _validate_source_statuses(value: object) -> None:
    """Validate explicit availability for all annotation sources."""
    if not isinstance(value, dict):
        raise EvidenceObjectError(
            "evidence.source_statuses must be a dictionary."
        )
    _validate_exact_fields(
        value,
        EVIDENCE_SOURCE_STATUS_FIELDS,
        "evidence.source_statuses",
    )
    for source, status in value.items():
        if (
            not isinstance(status, str)
            or status not in SOURCE_STATUS_VALUES
        ):
            raise EvidenceObjectError(
                "evidence.source_statuses."
                f"{source} has an unsupported status."
            )


def _validate_references(value: object) -> None:
    """Validate compact source names and provenance links."""
    if not isinstance(value, list):
        raise EvidenceObjectError("evidence.references must be a list.")

    seen_references: set[tuple[str, str]] = set()
    for index, reference in enumerate(value):
        path = f"evidence.references[{index}]"
        if not isinstance(reference, dict):
            raise EvidenceObjectError(f"{path} must be a dictionary.")
        _validate_exact_fields(
            reference,
            EVIDENCE_REFERENCE_FIELDS,
            path,
        )
        source = _validate_required_string(
            reference["source"],
            f"{path}.source",
        )
        url = _validate_url(reference["url"], f"{path}.url")
        reference_key = (source, url)
        if reference_key in seen_references:
            raise EvidenceObjectError(
                "evidence.references must not contain duplicates."
            )
        seen_references.add(reference_key)


def _validate_clingen_curations(value: object) -> None:
    """Validate compact, JSON-safe ClinGen curation records."""
    if not isinstance(value, list):
        raise EvidenceObjectError(
            "evidence.clingen_curations must be a list."
        )

    for index, curation in enumerate(value):
        path = f"evidence.clingen_curations[{index}]"
        if not isinstance(curation, dict):
            raise EvidenceObjectError(f"{path} must be a dictionary.")
        _validate_exact_fields(
            curation,
            EVIDENCE_CLINGEN_FIELDS,
            path,
        )
        _validate_required_string(
            curation["disease"],
            f"{path}.disease",
        )
        _validate_optional_string(
            curation["disease_id"],
            f"{path}.disease_id",
        )
        _validate_required_string(
            curation["classification"],
            f"{path}.classification",
        )
        _validate_optional_string(
            curation["mode_of_inheritance"],
            f"{path}.mode_of_inheritance",
        )
        pmids = _validate_unique_strings(
            curation["pmids"],
            f"{path}.pmids",
        )
        if any(not pmid.isdigit() for pmid in pmids):
            raise EvidenceObjectError(
                f"{path}.pmids must contain numeric PMID values."
            )
        if curation["report_url"] is not None:
            _validate_url(
                curation["report_url"],
                f"{path}.report_url",
            )


def validate_evidence_object(value: object) -> EvidenceObject:
    """Validate and return one complete Stage 7 evidence object.

    The exact-field contract prevents raw VCF fields, raw API payloads, and
    unapproved personal data from silently reaching reporting or LLM stages.
    Missing evidence remains explicit through nulls, empty lists, and source
    statuses.
    """
    if not isinstance(value, dict):
        raise EvidenceObjectError(
            "Evidence object must be a dictionary."
        )
    _validate_exact_fields(value, EVIDENCE_OBJECT_FIELDS, "evidence")

    if value["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise EvidenceObjectError(
            "evidence.schema_version must be "
            f"{EVIDENCE_SCHEMA_VERSION}."
        )
    _validate_variant(value["variant"])
    if value["assembly"] not in GENOME_ASSEMBLIES:
        raise EvidenceObjectError(
            "evidence.assembly must be GRCh37 or GRCh38."
        )

    for field in (
        "gene",
        "gene_id",
        "transcript",
        "consequence",
        "impact",
        "protein_change",
        "clinvar_accession",
        "clinvar_significance",
        "clinvar_review_status",
    ):
        _validate_optional_string(value[field], f"evidence.{field}")

    _validate_probability(
        value["population_frequency"],
        "evidence.population_frequency",
    )
    _validate_unique_strings(
        value["clinvar_conditions"],
        "evidence.clinvar_conditions",
    )
    _validate_clingen_curations(value["clingen_curations"])
    _validate_probability(
        value["phenotype_score"],
        "evidence.phenotype_score",
    )

    hpo_terms = _validate_unique_strings(
        value["hpo_terms"],
        "evidence.hpo_terms",
    )
    matched_hpo_terms = _validate_unique_strings(
        value["matched_hpo_terms"],
        "evidence.matched_hpo_terms",
    )
    for path, hpo_ids in (
        ("evidence.hpo_terms", hpo_terms),
        ("evidence.matched_hpo_terms", matched_hpo_terms),
    ):
        if any(HPO_ID_PATTERN.fullmatch(hpo_id) is None for hpo_id in hpo_ids):
            raise EvidenceObjectError(
                f"{path} must contain canonical HP:0000000 identifiers."
            )
    if not set(matched_hpo_terms).issubset(hpo_terms):
        raise EvidenceObjectError(
            "evidence.matched_hpo_terms must be a subset of "
            "evidence.hpo_terms."
        )
    if not hpo_terms and value["phenotype_score"] is not None:
        raise EvidenceObjectError(
            "evidence.phenotype_score must be null when no HPO terms "
            "are provided."
        )

    _validate_source_statuses(value["source_statuses"])
    _validate_references(value["references"])
    _validate_unique_strings(value["warnings"], "evidence.warnings")

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceObjectError(
            "Evidence object must be JSON serializable."
        ) from exc

    return cast(EvidenceObject, value)


def _require_candidate_mapping(
    value: object,
    path: str,
) -> dict[str, Any]:
    """Return one candidate mapping with a field-specific error."""
    if not isinstance(value, dict):
        raise EvidenceObjectError(f"{path} must be a dictionary.")
    return value


def _deduplicate_strings(
    values: object,
    path: str,
) -> list[str]:
    """Copy a string list while preserving its first-seen order."""
    if not isinstance(values, list):
        raise EvidenceObjectError(f"{path} must be a list.")
    result: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        text = _validate_required_string(value, f"{path}[{index}]")
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _map_clinvar_conditions(clinvar: dict[str, Any]) -> list[str]:
    """Reduce standardized ClinVar condition objects to unique names."""
    raw_conditions = clinvar.get("conditions", [])
    if not isinstance(raw_conditions, list):
        raise EvidenceObjectError(
            "candidate.sources.clinvar.conditions must be a list."
        )

    names: list[str] = []
    seen: set[str] = set()
    for index, condition in enumerate(raw_conditions):
        path = f"candidate.sources.clinvar.conditions[{index}]"
        condition_data = _require_candidate_mapping(condition, path)
        name = _validate_required_string(
            condition_data.get("name"),
            f"{path}.name",
        )
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _map_clingen_curations(
    clingen: dict[str, Any],
) -> list[EvidenceClinGenCuration]:
    """Select compact ClinGen fields from standardized GenCC evidence."""
    raw_curations = clingen.get("curations", [])
    if not isinstance(raw_curations, list):
        raise EvidenceObjectError(
            "candidate.sources.clingen.curations must be a list."
        )

    curations: list[EvidenceClinGenCuration] = []
    for index, curation in enumerate(raw_curations):
        path = f"candidate.sources.clingen.curations[{index}]"
        curation_data = _require_candidate_mapping(curation, path)
        pmids = _deduplicate_strings(
            curation_data.get("pmids", []),
            f"{path}.pmids",
        )
        curations.append(
            {
                "disease": curation_data.get("disease"),
                "disease_id": curation_data.get("disease_id"),
                "classification": curation_data.get(
                    "classification"
                ),
                "mode_of_inheritance": curation_data.get(
                    "mode_of_inheritance"
                ),
                "pmids": pmids,
                "report_url": curation_data.get("report_url"),
            }
        )
    return curations


def _map_references(candidate: dict[str, Any]) -> list[EvidenceReference]:
    """Copy and deduplicate source references without raw metadata."""
    raw_references = candidate.get("references", [])
    if not isinstance(raw_references, list):
        raise EvidenceObjectError("candidate.references must be a list.")

    references: list[EvidenceReference] = []
    seen: set[tuple[str, str]] = set()
    for index, reference in enumerate(raw_references):
        path = f"candidate.references[{index}]"
        reference_data = _require_candidate_mapping(reference, path)
        source = _validate_required_string(
            reference_data.get("source"),
            f"{path}.source",
        )
        url = _validate_url(
            reference_data.get("url"),
            f"{path}.url",
        )
        key = (source, url)
        if key in seen:
            continue
        seen.add(key)
        references.append({"source": source, "url": url})
    return references


def _map_phenotype(
    candidate: dict[str, Any],
) -> tuple[float | None, list[str], list[str]]:
    """Map an optional complete Stage 6 phenotype result."""
    phenotype_fields = {
        "phenotype_score",
        "hpo_terms",
        "matched_hpo_terms",
    }
    present_fields = phenotype_fields.intersection(candidate)
    if not present_fields:
        return None, [], []
    if present_fields != phenotype_fields:
        missing = phenotype_fields - present_fields
        raise EvidenceObjectError(
            "Candidate has an incomplete phenotype result; missing: "
            f"{', '.join(sorted(missing))}."
        )

    raw_hpo_terms = candidate["hpo_terms"]
    raw_matched_hpo_terms = candidate["matched_hpo_terms"]
    if not isinstance(raw_hpo_terms, list):
        raise EvidenceObjectError("candidate.hpo_terms must be a list.")
    if not isinstance(raw_matched_hpo_terms, list):
        raise EvidenceObjectError(
            "candidate.matched_hpo_terms must be a list."
        )
    hpo_terms = list(raw_hpo_terms)
    matched_hpo_terms = list(raw_matched_hpo_terms)
    match_count = candidate.get("phenotype_match_count")
    if match_count is not None and (
        isinstance(match_count, bool)
        or not isinstance(match_count, int)
        or match_count != len(matched_hpo_terms)
    ):
        raise EvidenceObjectError(
            "candidate.phenotype_match_count must equal the number of "
            "matched HPO terms."
        )
    return (
        candidate["phenotype_score"],
        hpo_terms,
        matched_hpo_terms,
    )


def build_evidence_object(candidate: object) -> EvidenceObject:
    """Convert one Stage 5/6 candidate to the Stage 7 schema."""
    candidate_data = _require_candidate_mapping(candidate, "candidate")
    variant = _require_candidate_mapping(
        candidate_data.get("variant"),
        "candidate.variant",
    )
    sources = _require_candidate_mapping(
        candidate_data.get("sources"),
        "candidate.sources",
    )
    source_payloads = {
        source: _require_candidate_mapping(
            sources.get(source),
            f"candidate.sources.{source}",
        )
        for source in EVIDENCE_SOURCE_NAMES
    }
    clinvar = source_payloads["clinvar"]
    clingen = source_payloads["clingen"]
    phenotype_score, hpo_terms, matched_hpo_terms = _map_phenotype(
        candidate_data
    )

    clinvar_accession = (
        clinvar.get("accession_version")
        or clinvar.get("accession")
    )
    evidence: EvidenceObject = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "variant": {
            "chrom": variant.get("chrom"),
            "pos": variant.get("pos"),
            "ref": variant.get("ref"),
            "alt": variant.get("alt"),
        },
        "assembly": candidate_data.get("assembly"),
        "gene": candidate_data.get("gene"),
        "gene_id": candidate_data.get("gene_id"),
        "transcript": candidate_data.get("transcript"),
        "consequence": candidate_data.get("consequence"),
        "impact": candidate_data.get("impact"),
        "protein_change": candidate_data.get("protein_change"),
        "population_frequency": candidate_data.get(
            "population_frequency"
        ),
        "clinvar_accession": clinvar_accession,
        "clinvar_significance": clinvar.get(
            "clinical_significance"
        ),
        "clinvar_review_status": clinvar.get("review_status"),
        "clinvar_conditions": _map_clinvar_conditions(clinvar),
        "clingen_curations": _map_clingen_curations(clingen),
        "phenotype_score": phenotype_score,
        "hpo_terms": hpo_terms,
        "matched_hpo_terms": matched_hpo_terms,
        "source_statuses": {
            source: payload.get("status")
            for source, payload in source_payloads.items()
        },
        "references": _map_references(candidate_data),
        "warnings": _deduplicate_strings(
            candidate_data.get("warnings", []),
            "candidate.warnings",
        ),
    }
    return validate_evidence_object(evidence)


def build_evidence_objects(
    candidates: Iterable[dict[str, Any]],
) -> list[EvidenceObject]:
    """Convert an iterable of candidates without mutating its items."""
    if isinstance(candidates, (str, bytes, dict)):
        raise EvidenceObjectError(
            "Candidates must be an iterable of dictionaries."
        )
    try:
        iterator = iter(candidates)
    except TypeError as exc:
        raise EvidenceObjectError(
            "Candidates must be an iterable of dictionaries."
        ) from exc

    evidence_objects: list[EvidenceObject] = []
    for index, candidate in enumerate(iterator):
        try:
            evidence_objects.append(build_evidence_object(candidate))
        except EvidenceObjectError as exc:
            raise EvidenceObjectError(
                f"Candidate at index {index} is invalid: {exc}"
            ) from exc
    return evidence_objects
