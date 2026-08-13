"""Validated cross-provider identifiers and retrieval-outcome diagnostics."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, TypedDict, cast


IDENTIFIER_BUNDLE_SCHEMA_VERSION = "1.0"
RETRIEVAL_ASSESSMENT_SCHEMA_VERSION = "1.0"
MAX_IDENTIFIERS_PER_TYPE = 50
MAX_IDENTIFIER_PROVENANCE = 150
MAX_IDENTIFIER_CHARS = 500
IDENTIFIER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
GENE_SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HGNC_PATTERN = re.compile(r"HGNC:[1-9][0-9]*")
RSID_PATTERN = re.compile(r"rs[1-9][0-9]*", re.IGNORECASE)
VCV_PATTERN = re.compile(r"VCV[0-9]{9}(?:\.[0-9]+)?")
RCV_PATTERN = re.compile(r"RCV[0-9]{9}(?:\.[0-9]+)?")
MONDO_PATTERN = re.compile(r"MONDO:[0-9]{7}")
OMIM_PATTERN = re.compile(r"OMIM:[1-9][0-9]{5}")
MEDGEN_PATTERN = re.compile(r"C[0-9]{7}")

RetrievalCause = Literal[
    "TRUE_SOURCE_ABSENCE",
    "QUERY_WEAKNESS",
    "IDENTIFIER_GAP",
    "NORMALIZATION_MISMATCH",
    "OVERSTRICT_FILTERING",
    "PROVIDER_SEMANTIC_MISMATCH",
    "OPERATIONAL_FAILURE",
    "LIVE_VERIFICATION_REQUIRED",
]

RETRIEVAL_CAUSES = frozenset(
    {
        "TRUE_SOURCE_ABSENCE",
        "QUERY_WEAKNESS",
        "IDENTIFIER_GAP",
        "NORMALIZATION_MISMATCH",
        "OVERSTRICT_FILTERING",
        "PROVIDER_SEMANTIC_MISMATCH",
        "OPERATIONAL_FAILURE",
        "LIVE_VERIFICATION_REQUIRED",
    }
)
SCOPES = frozenset({"allele", "transcript", "gene", "disease"})
VALIDATIONS = frozenset(
    {
        "deterministic_normalization",
        "provider_exact_allele",
        "provider_transcript_annotation",
        "provider_gene_mapping",
        "provider_condition_mapping",
    }
)


class IdentifierReference(TypedDict):
    """One identifier named in a query diagnostic."""

    type: str
    value: str


class IdentifierProvenance(TypedDict):
    """Source and validation scope for one retained identifier."""

    identifier_type: str
    value: str
    source: str
    scope: str
    validation: str


class VariantIdentifierBundle(TypedDict):
    """Bounded allele-specific identity assembled across trusted providers."""

    schema_version: str
    input_index: int
    genome_build: str
    chromosome: str
    position: int
    reference: str
    alternate: str
    minimal_representation_status: str
    left_normalization_status: str
    genomic_hgvs: list[str]
    transcript_hgvs: list[str]
    protein_hgvs: list[str]
    transcript_ids: list[str]
    gene_symbol: str | None
    hgnc_id: str | None
    rsids: list[str]
    clinvar_variation_ids: list[str]
    vcv_accessions: list[str]
    rcv_accessions: list[str]
    mondo_ids: list[str]
    omim_ids: list[str]
    medgen_ids: list[str]
    provenance: list[IdentifierProvenance]


class RetrievalAssessment(TypedDict):
    """Non-generic explanation for one sparse provider outcome."""

    schema_version: str
    provider: str
    status: str
    cause: RetrievalCause
    query_strategy: str
    identifiers_used: list[IdentifierReference]
    unused_eligible_identifiers: list[IdentifierReference]
    candidates_returned: int | None
    candidates_rejected: int | None
    live_verification_required: bool


BUNDLE_FIELDS = frozenset(VariantIdentifierBundle.__required_keys__)
PROVENANCE_FIELDS = frozenset(IdentifierProvenance.__required_keys__)
REFERENCE_FIELDS = frozenset(IdentifierReference.__required_keys__)
ASSESSMENT_FIELDS = frozenset(RetrievalAssessment.__required_keys__)
LIST_FIELDS = (
    "genomic_hgvs",
    "transcript_hgvs",
    "protein_hgvs",
    "transcript_ids",
    "rsids",
    "clinvar_variation_ids",
    "vcv_accessions",
    "rcv_accessions",
    "mondo_ids",
    "omim_ids",
    "medgen_ids",
)
LIST_FIELD_PATTERNS = {
    "rsids": RSID_PATTERN,
    "clinvar_variation_ids": re.compile(r"[1-9][0-9]*"),
    "vcv_accessions": VCV_PATTERN,
    "rcv_accessions": RCV_PATTERN,
    "mondo_ids": MONDO_PATTERN,
    "omim_ids": OMIM_PATTERN,
    "medgen_ids": MEDGEN_PATTERN,
}


class RetrievalIntelligenceError(ValueError):
    """Raised when identifier or retrieval provenance is invalid."""


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _items(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    return []


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_IDENTIFIER_CHARS
    ):
        raise RetrievalIntelligenceError(
            f"{field} must be a bounded non-empty string."
        )
    return value.strip()


def _identifier_name(value: object, field: str) -> str:
    text = _text(value, field)
    if IDENTIFIER_NAME_PATTERN.fullmatch(text) is None:
        raise RetrievalIntelligenceError(
            f"{field} must be a lowercase identifier."
        )
    return text


class _IdentifierCollector:
    def __init__(self) -> None:
        self.values: dict[str, list[str]] = {
            field: [] for field in LIST_FIELDS
        }
        self.provenance: list[IdentifierProvenance] = []

    def add(
        self,
        field: str,
        value: object,
        *,
        source: str,
        scope: str,
        validation: str,
        pattern: re.Pattern[str] | None = None,
        normalize: bool = False,
    ) -> None:
        if field not in self.values or not isinstance(value, str):
            return
        candidate = value.strip()
        if not candidate or len(candidate) > MAX_IDENTIFIER_CHARS:
            return
        if normalize:
            candidate = candidate.upper()
        if pattern is not None and pattern.fullmatch(candidate) is None:
            return
        values = self.values[field]
        if candidate not in values and len(values) < MAX_IDENTIFIERS_PER_TYPE:
            values.append(candidate)
        record: IdentifierProvenance = {
            "identifier_type": field.removesuffix("s"),
            "value": candidate,
            "source": source,
            "scope": scope,
            "validation": validation,
        }
        if (
            record not in self.provenance
            and len(self.provenance) < MAX_IDENTIFIER_PROVENANCE
        ):
            self.provenance.append(record)


def _condition_identifiers(
    collector: _IdentifierCollector,
    conditions: object,
    *,
    source: str,
) -> None:
    for condition in _items(conditions):
        for identifier in _items(condition.get("identifiers")):
            identifier_source = str(identifier.get("source", "")).casefold()
            value = identifier.get("id")
            if identifier_source == "mondo":
                collector.add(
                    "mondo_ids", value, source=source, scope="disease",
                    validation="provider_condition_mapping",
                    pattern=MONDO_PATTERN, normalize=True,
                )
            elif identifier_source == "omim":
                normalized = (
                    f"OMIM:{value}"
                    if isinstance(value, str)
                    and not value.upper().startswith("OMIM:")
                    else value
                )
                collector.add(
                    "omim_ids", normalized, source=source, scope="disease",
                    validation="provider_condition_mapping",
                    pattern=OMIM_PATTERN,
                    normalize=True,
                )
            elif identifier_source == "medgen":
                collector.add(
                    "medgen_ids", value, source=source, scope="disease",
                    validation="provider_condition_mapping",
                    pattern=MEDGEN_PATTERN, normalize=True,
                )


def _add_transcript_context(
    collector: _IdentifierCollector,
    payload: Mapping[str, Any],
    *,
    source: str,
    transcript_key: str,
    hgvs_c_key: str,
    hgvs_p_key: str,
) -> None:
    transcript = payload.get(transcript_key)
    collector.add(
        "transcript_ids", transcript, source=source, scope="transcript",
        validation="provider_transcript_annotation",
    )
    for field, key in (
        ("transcript_hgvs", hgvs_c_key),
        ("protein_hgvs", hgvs_p_key),
    ):
        value = payload.get(key)
        collector.add(
            field, value, source=source, scope="transcript",
            validation="provider_transcript_annotation",
        )
        if isinstance(value, str) and ":" in value:
            collector.add(
                "transcript_ids", value.split(":", 1)[0], source=source,
                scope="transcript",
                validation="provider_transcript_annotation",
            )


def _unambiguous(values: list[str]) -> str | None:
    unique = {value.casefold(): value for value in values}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _chromosome(value: object) -> str:
    chromosome = str(value).strip()
    if chromosome.casefold().startswith("chr"):
        chromosome = chromosome[3:]
    chromosome = chromosome.upper()
    return "MT" if chromosome == "M" else chromosome


def build_variant_identifier_bundle(
    annotation: Mapping[str, Any],
    *,
    input_index: int,
    normalized_genomic_hgvs: str | None,
) -> VariantIdentifierBundle:
    """Aggregate only validated or deterministically normalized identifiers."""

    variant = _mapping(annotation.get("variant"))
    if (
        isinstance(input_index, bool)
        or not isinstance(input_index, int)
        or input_index < 0
    ):
        raise RetrievalIntelligenceError("input_index must be non-negative.")
    collector = _IdentifierCollector()
    if normalized_genomic_hgvs is not None:
        collector.add(
            "genomic_hgvs", normalized_genomic_hgvs,
            source="input_normalization", scope="allele",
            validation="deterministic_normalization",
        )

    sources = _mapping(annotation.get("sources"))
    vep = _mapping(sources.get("vep"))
    genebe = _mapping(sources.get("genebe"))
    myvariant = _mapping(sources.get("myvariant"))
    clinvar = _mapping(sources.get("clinvar"))
    clingen = _mapping(sources.get("clingen"))

    collector.add(
        "genomic_hgvs", vep.get("validated_genomic_hgvs"),
        source=str(vep.get("provider") or "Ensembl VEP"), scope="allele",
        validation="provider_exact_allele",
    )
    collector.add(
        "genomic_hgvs", myvariant.get("variant_id"),
        source=str(myvariant.get("provider") or "MyVariant.info"),
        scope="allele", validation="provider_exact_allele",
    )
    _add_transcript_context(
        collector, annotation, source="Ensembl VEP",
        transcript_key="transcript", hgvs_c_key="hgvsc", hgvs_p_key="hgvsp",
    )
    for consequence in _items(vep.get("transcript_consequences")):
        _add_transcript_context(
            collector, consequence, source="Ensembl VEP",
            transcript_key="transcript_id", hgvs_c_key="hgvsc",
            hgvs_p_key="hgvsp",
        )
    _add_transcript_context(
        collector, genebe, source="GeneBe", transcript_key="transcript",
        hgvs_c_key="hgvs_c", hgvs_p_key="hgvs_p",
    )
    for consequence in _items(genebe.get("consequences")):
        _add_transcript_context(
            collector, consequence, source="GeneBe",
            transcript_key="transcript", hgvs_c_key="hgvs_c",
            hgvs_p_key="hgvs_p",
        )

    gene_candidates: list[str] = []
    for payload, field, source in (
        (annotation, "gene", "Ensembl VEP"),
        (genebe, "gene", "GeneBe"),
        (myvariant, "gene", "MyVariant.info"),
        (clinvar, "gene", "NCBI ClinVar"),
        (clingen, "gene", "ClinGen"),
    ):
        value = payload.get(field)
        if (
            isinstance(value, str)
            and GENE_SYMBOL_PATTERN.fullmatch(value.strip())
            and not value.upper().startswith("ENSG")
        ):
            gene_candidates.append(value.strip())
            if len(collector.provenance) < MAX_IDENTIFIER_PROVENANCE:
                record: IdentifierProvenance = {
                    "identifier_type": "gene_symbol",
                    "value": value.strip(),
                    "source": source,
                    "scope": "gene",
                    "validation": "provider_gene_mapping",
                }
                if record not in collector.provenance:
                    collector.provenance.append(record)

    hgnc_candidates: list[tuple[str, str]] = []
    raw_hgnc = genebe.get("gene_hgnc_id")
    if isinstance(raw_hgnc, int) and not isinstance(raw_hgnc, bool) and raw_hgnc > 0:
        hgnc_candidates.append((f"HGNC:{raw_hgnc}", "GeneBe"))
    for value, source in (
        (annotation.get("gene_id"), "Ensembl VEP"),
        (clingen.get("gene_id"), "ClinGen"),
    ):
        if isinstance(value, str) and HGNC_PATTERN.fullmatch(value.strip().upper()):
            hgnc_candidates.append((value.strip().upper(), source))
    for value, source in hgnc_candidates:
        record = {
            "identifier_type": "hgnc_id",
            "value": value,
            "source": source,
            "scope": "gene",
            "validation": "provider_gene_mapping",
        }
        if record not in collector.provenance:
            collector.provenance.append(cast(IdentifierProvenance, record))

    collector.add(
        "rsids", myvariant.get("rsid"), source="MyVariant.info",
        scope="allele", validation="provider_exact_allele",
        pattern=RSID_PATTERN,
    )
    ensembl_variation = _mapping(myvariant.get("ensembl_variation"))
    collector.add(
        "rsids", ensembl_variation.get("id"),
        source="Ensembl REST Variation", scope="allele",
        validation="provider_exact_allele", pattern=RSID_PATTERN,
    )
    derived = _mapping(myvariant.get("clinvar_derived"))
    for payload, source in (
        (derived, "MyVariant.info"),
        (clinvar, "NCBI ClinVar"),
    ):
        collector.add(
            "clinvar_variation_ids", payload.get("variation_id"),
            source=source, scope="allele", validation="provider_exact_allele",
            pattern=re.compile(r"[1-9][0-9]*"),
        )
        raw_rcv_accessions = payload.get("rcv_accessions")
        rcv_accessions = (
            raw_rcv_accessions
            if isinstance(raw_rcv_accessions, list)
            else []
        )
        for accession in rcv_accessions:
            collector.add(
                "rcv_accessions", accession, source=source, scope="disease",
                validation="provider_condition_mapping", pattern=RCV_PATTERN,
                normalize=True,
            )
        _condition_identifiers(
            collector, payload.get("conditions"), source=source
        )
    accession = clinvar.get("accession_version") or clinvar.get(
        "accession"
    )
    collector.add(
        "vcv_accessions",
        accession,
        source="NCBI ClinVar",
        scope="allele",
        validation="provider_exact_allele",
        pattern=VCV_PATTERN,
        normalize=True,
    )
    for curation in _items(clingen.get("curations")):
        collector.add(
            "mondo_ids", curation.get("disease_id"), source="ClinGen",
            scope="disease", validation="provider_condition_mapping",
            pattern=MONDO_PATTERN, normalize=True,
        )

    bundle: VariantIdentifierBundle = {
        "schema_version": IDENTIFIER_BUNDLE_SCHEMA_VERSION,
        "input_index": input_index,
        "genome_build": str(annotation.get("assembly", "")),
        "chromosome": _chromosome(variant.get("chrom")),
        "position": variant.get("pos"),
        "reference": str(variant.get("ref", "")).upper(),
        "alternate": str(variant.get("alt", "")).upper(),
        "minimal_representation_status": (
            "applied"
            if (
                str(variant.get("ref", ""))[0:1].upper()
                == str(variant.get("alt", ""))[0:1].upper()
                or str(variant.get("ref", ""))[-1:].upper()
                == str(variant.get("alt", ""))[-1:].upper()
            )
            else "not_needed"
        ),
        "left_normalization_status": (
            "unverified_without_reference"
            if len(str(variant.get("ref", "")))
            != len(str(variant.get("alt", "")))
            else "not_required"
        ),
        **collector.values,
        "gene_symbol": _unambiguous(gene_candidates),
        "hgnc_id": _unambiguous(
            [value for value, _source in hgnc_candidates]
        ),
        "provenance": collector.provenance,
    }
    return validate_variant_identifier_bundle(bundle)


def validate_variant_identifier_bundle(
    value: object,
) -> VariantIdentifierBundle:
    """Validate and detach one identifier bundle."""

    if not isinstance(value, Mapping) or set(value) != BUNDLE_FIELDS:
        raise RetrievalIntelligenceError(
            "Variant identifier bundle fields do not match the contract."
        )
    if value["schema_version"] != IDENTIFIER_BUNDLE_SCHEMA_VERSION:
        raise RetrievalIntelligenceError("Unsupported identifier bundle version.")
    if (
        isinstance(value["input_index"], bool)
        or not isinstance(value["input_index"], int)
        or value["input_index"] < 0
    ):
        raise RetrievalIntelligenceError("input_index must be non-negative.")
    if value["genome_build"] not in {"GRCh37", "GRCh38"}:
        raise RetrievalIntelligenceError("Unsupported genome build.")
    chromosome = _text(value["chromosome"], "chromosome")
    if chromosome not in {
        *(str(number) for number in range(1, 23)),
        "X",
        "Y",
        "MT",
    }:
        raise RetrievalIntelligenceError("chromosome is invalid.")
    if (
        isinstance(value["position"], bool)
        or not isinstance(value["position"], int)
        or value["position"] < 1
    ):
        raise RetrievalIntelligenceError("position must be positive.")
    _text(value["reference"], "reference")
    _text(value["alternate"], "alternate")
    if value["minimal_representation_status"] not in {
        "applied",
        "not_needed",
    }:
        raise RetrievalIntelligenceError(
            "minimal_representation_status is invalid."
        )
    if value["left_normalization_status"] not in {
        "not_required",
        "unverified_without_reference",
    }:
        raise RetrievalIntelligenceError(
            "left_normalization_status is invalid."
        )
    for field in LIST_FIELDS:
        items = value[field]
        if not isinstance(items, list) or len(items) > MAX_IDENTIFIERS_PER_TYPE:
            raise RetrievalIntelligenceError(f"{field} must be a bounded list.")
        normalized = [_text(item, field) for item in items]
        if len(set(normalized)) != len(normalized):
            raise RetrievalIntelligenceError(f"{field} must be unique.")
        pattern = LIST_FIELD_PATTERNS.get(field)
        if pattern is not None and any(
            pattern.fullmatch(item) is None for item in normalized
        ):
            raise RetrievalIntelligenceError(
                f"{field} contains an invalid identifier."
            )
    for field, pattern in (
        ("gene_symbol", GENE_SYMBOL_PATTERN),
        ("hgnc_id", HGNC_PATTERN),
    ):
        item = value[field]
        if item is not None and (
            not isinstance(item, str)
            or pattern.fullmatch(item) is None
        ):
            raise RetrievalIntelligenceError(f"{field} is invalid.")
    provenance = value["provenance"]
    if not isinstance(provenance, list) or len(provenance) > MAX_IDENTIFIER_PROVENANCE:
        raise RetrievalIntelligenceError("provenance must be a bounded list.")
    for record in provenance:
        if not isinstance(record, Mapping) or set(record) != PROVENANCE_FIELDS:
            raise RetrievalIntelligenceError("Identifier provenance is invalid.")
        _identifier_name(record["identifier_type"], "identifier_type")
        _text(record["value"], "identifier value")
        _text(record["source"], "identifier source")
        if record["scope"] not in SCOPES or record["validation"] not in VALIDATIONS:
            raise RetrievalIntelligenceError("Identifier provenance is invalid.")
    return cast(VariantIdentifierBundle, deepcopy(dict(value)))


def _validate_references(value: object, field: str) -> list[IdentifierReference]:
    if not isinstance(value, list) or len(value) > MAX_IDENTIFIERS_PER_TYPE:
        raise RetrievalIntelligenceError(f"{field} must be a bounded list.")
    result: list[IdentifierReference] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != REFERENCE_FIELDS:
            raise RetrievalIntelligenceError(f"{field} contains an invalid reference.")
        reference: IdentifierReference = {
            "type": _identifier_name(item["type"], f"{field} type"),
            "value": _text(item["value"], f"{field} value"),
        }
        if reference not in result:
            result.append(reference)
    return result


def build_retrieval_assessment(
    *,
    provider: str,
    status: str,
    query_strategy: str,
    identifiers_used: list[IdentifierReference],
    unused_eligible_identifiers: list[IdentifierReference],
    candidates_returned: int | None = None,
    candidates_rejected: int | None = None,
    operational_failure: bool = False,
    identifier_gap: bool = False,
    normalization_mismatch: bool = False,
    provider_semantic_mismatch: bool = False,
    source_absence_confirmed: bool = False,
) -> RetrievalAssessment:
    """Classify a sparse outcome without flattening distinct causes."""

    if operational_failure and status == "no_match":
        raise RetrievalIntelligenceError(
            "no_match cannot be an operational failure."
        )
    if operational_failure:
        cause: RetrievalCause = "OPERATIONAL_FAILURE"
    elif normalization_mismatch:
        cause = "NORMALIZATION_MISMATCH"
    elif provider_semantic_mismatch:
        cause = "PROVIDER_SEMANTIC_MISMATCH"
    elif candidates_returned and candidates_rejected == candidates_returned:
        cause = "OVERSTRICT_FILTERING"
    elif identifier_gap:
        cause = "IDENTIFIER_GAP"
    elif unused_eligible_identifiers:
        cause = "QUERY_WEAKNESS"
    elif source_absence_confirmed:
        cause = "TRUE_SOURCE_ABSENCE"
    else:
        cause = "LIVE_VERIFICATION_REQUIRED"
    return validate_retrieval_assessment(
        {
            "schema_version": RETRIEVAL_ASSESSMENT_SCHEMA_VERSION,
            "provider": provider,
            "status": status,
            "cause": cause,
            "query_strategy": query_strategy,
            "identifiers_used": identifiers_used,
            "unused_eligible_identifiers": unused_eligible_identifiers,
            "candidates_returned": candidates_returned,
            "candidates_rejected": candidates_rejected,
            "live_verification_required": cause == "LIVE_VERIFICATION_REQUIRED",
        }
    )


def validate_retrieval_assessment(value: object) -> RetrievalAssessment:
    """Validate and detach one retrieval assessment."""

    if not isinstance(value, Mapping) or set(value) != ASSESSMENT_FIELDS:
        raise RetrievalIntelligenceError(
            "Retrieval assessment fields do not match the contract."
        )
    if value["schema_version"] != RETRIEVAL_ASSESSMENT_SCHEMA_VERSION:
        raise RetrievalIntelligenceError("Unsupported retrieval assessment version.")
    _identifier_name(value["provider"], "provider")
    _identifier_name(value["status"], "status")
    _identifier_name(value["query_strategy"], "query_strategy")
    if value["cause"] not in RETRIEVAL_CAUSES:
        raise RetrievalIntelligenceError("Unsupported retrieval cause.")
    if (
        value["status"] == "no_match"
        and value["cause"] == "OPERATIONAL_FAILURE"
    ):
        raise RetrievalIntelligenceError(
            "no_match cannot be an operational failure."
        )
    _validate_references(value["identifiers_used"], "identifiers_used")
    _validate_references(
        value["unused_eligible_identifiers"],
        "unused_eligible_identifiers",
    )
    for field in ("candidates_returned", "candidates_rejected"):
        item = value[field]
        if item is not None and (
            isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
        ):
            raise RetrievalIntelligenceError(f"{field} must be non-negative.")
    if (
        value["candidates_returned"] is not None
        and value["candidates_rejected"] is not None
        and value["candidates_rejected"] > value["candidates_returned"]
    ):
        raise RetrievalIntelligenceError(
            "candidates_rejected cannot exceed candidates_returned."
        )
    if value["live_verification_required"] != (
        value["cause"] == "LIVE_VERIFICATION_REQUIRED"
    ):
        raise RetrievalIntelligenceError(
            "live_verification_required is inconsistent with cause."
        )
    return cast(RetrievalAssessment, deepcopy(dict(value)))
