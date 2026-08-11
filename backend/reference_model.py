"""Stage 92 Reference Model V2: literature and data-source separation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Literal, TypedDict, cast

from backend.human_links import (
    is_machine_readable_url,
    resolve_ensembl_human_url,
    resolve_genebe_human_url,
)
from backend.provider_resilience import (
    ProviderContractError,
    validate_capability_result,
)
from backend.references import (
    CanonicalReference,
    CanonicalReferenceError,
    build_canonical_references,
    validate_canonical_reference,
    validated_reference_url,
)


REFERENCE_MODEL_SCHEMA_VERSION = "2.0"
MAX_REFERENCE_MODEL_ITEMS = 50
LITERATURE_IDENTIFIER_TYPES = frozenset({"PMID", "PMCID", "DOI"})
LITERATURE_SOURCE_NAMES = frozenset(
    {"doi", "europe pmc", "pubmed", "pubmed central"}
)
DATA_SOURCE_STATUSES = frozenset(
    {"available", "no_match", "unsupported", "unavailable", "not_assessed"}
)

ProviderRole = Literal["primary", "fallback"]
DataSourceStatus = Literal[
    "available",
    "no_match",
    "unsupported",
    "unavailable",
    "not_assessed",
]


class ReferenceModelError(ValueError):
    """Raised when Reference Model V2 violates its category boundary."""


class DataSourceRecord(TypedDict):
    """One unnumbered database/tool provenance record."""

    source: str
    capability: str
    status: DataSourceStatus
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


class ReferenceModelV2(TypedDict):
    """Strictly separated report references and provider provenance."""

    schema_version: str
    literature_references: list[CanonicalReference]
    data_sources: list[DataSourceRecord]


DATA_SOURCE_FIELDS = frozenset(DataSourceRecord.__required_keys__)
REFERENCE_MODEL_FIELDS = frozenset(ReferenceModelV2.__required_keys__)

_PROVIDER_LABELS = {
    "cached_cspec": "CSpec",
    "clingen": "ClinGen / GenCC",
    "clingen_cspec": "CSpec",
    "ensembl_variation": "Ensembl",
    "ensembl_vep": "Ensembl",
    "genebe": "GeneBe",
    "gnomad": "Population source (gnomAD)",
    "local_hpo_disease_fallback": "MyDisease/local HPO disease context",
    "local_hpo_gene_fallback": "Phen2Gene/local HPO-Gene",
    "mydisease": "MyDisease",
    "myvariant": "MyVariant.info",
    "ncbi_clinvar": "ClinVar",
    "phen2gene": "Phen2Gene/local HPO-Gene",
    "variantvalidator": "VariantValidator",
    "vep": "Ensembl",
}
_SOURCE_CAPABILITIES = {
    "clinvar": "clinvar_evidence",
    "clingen": "gene_disease_validity",
    "cspec": "cspec_context",
    "genebe": "automated_classification",
    "myvariant": "variant_context",
    "vep": "variant_annotation",
}
_CAPABILITY_ORDER = (
    "variant_annotation",
    "variant_context",
    "clinvar_evidence",
    "cspec_context",
    "phenotype_gene",
    "disease_context",
    "population_frequency",
    "literature",
)


def _text(value: object, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ReferenceModelError(f"{path} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > 2_000 or any(
        ord(character) < 32 for character in normalized
    ):
        raise ReferenceModelError(f"{path} is invalid.")
    return normalized


def _status(value: object) -> DataSourceStatus:
    normalized = str(value or "").strip().casefold().replace(" ", "_")
    if normalized in {"success", "available", "available_via_fallback"}:
        return "available"
    if normalized in {"no_match", "not_found", "no_exact_match"}:
        return "no_match"
    if normalized in {"unsupported", "not_supported"}:
        return "unsupported"
    if normalized in {
        "not_applicable",
        "not_assessed",
        "not_triggered",
        "skipped",
    }:
        return "not_assessed"
    return "unavailable"


def _provider_label(provider: str) -> str:
    configured = _PROVIDER_LABELS.get(provider.casefold())
    if configured is not None:
        return configured
    return (
        provider.replace("_", " ").strip().title()
        if "_" in provider
        else provider.strip()
    )


def _optional_provenance_text(
    capability: Mapping[str, object],
    field: str,
) -> str | None:
    provenance = capability.get("provenance")
    value = provenance.get(field) if isinstance(provenance, Mapping) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _reference_group(source: str) -> str:
    normalized = source.casefold()
    if "clinvar" in normalized:
        return "clinvar"
    if "cspec" in normalized:
        return "cspec"
    if "clingen" in normalized or "gencc" in normalized:
        return "clingen"
    if "genebe" in normalized:
        return "genebe"
    if "myvariant" in normalized:
        return "myvariant"
    if "ensembl" in normalized or "variantvalidator" in normalized:
        return "ensembl"
    return normalized


def _provider_group(provider: str) -> str:
    normalized = provider.casefold()
    if normalized in {"clinvar", "ncbi_clinvar"}:
        return "clinvar"
    if normalized in {"clingen_cspec", "cached_cspec", "cspec"}:
        return "cspec"
    if normalized in {
        "ensembl_vep",
        "ensembl_variation",
        "variantvalidator",
        "vep",
    }:
        return "ensembl"
    return normalized


def _population_value(evidence: Mapping[str, object], field: str) -> object:
    annotations = evidence.get("annotations")
    population = (
        annotations.get("population")
        if isinstance(annotations, Mapping)
        else None
    )
    return population.get(field) if isinstance(population, Mapping) else None


def _human_link(
    *,
    evidence: Mapping[str, object],
    provider_group: str,
    reference: CanonicalReference | None,
) -> tuple[str | None, str | None]:
    """Resolve a reviewer-facing record without exposing provider APIs."""

    identifier = reference["identifier"] if reference is not None else None
    if provider_group == "genebe":
        resolved = resolve_genebe_human_url(
            assembly=evidence.get("assembly"),
            variant=evidence.get("variant"),
        )
        return resolved if resolved is not None else (None, identifier)
    if provider_group == "ensembl":
        rsid = _population_value(evidence, "rsid")
        return (
            resolve_ensembl_human_url(
                assembly=evidence.get("assembly"),
                identifier=rsid,
            ),
            str(rsid).strip() if isinstance(rsid, str) and rsid.strip() else identifier,
        )
    if provider_group == "myvariant":
        variant_id = _population_value(evidence, "variant_id")
        return (
            None,
            str(variant_id).strip()
            if isinstance(variant_id, str) and variant_id.strip()
            else identifier,
        )
    url = reference["canonical_url"] if reference is not None else None
    if is_machine_readable_url(
        reference["source"] if reference is not None else provider_group,
        url,
    ):
        url = None
    return url, identifier


def _renumber_literature(
    references: Sequence[CanonicalReference],
) -> list[CanonicalReference]:
    result: list[CanonicalReference] = []
    for reference in references:
        if reference["identifier_type"] not in LITERATURE_IDENTIFIER_TYPES:
            continue
        copied = deepcopy(reference)
        copied["reference_id"] = f"R{len(result) + 1}"
        result.append(validate_canonical_reference(copied))
    return result


def build_reference_model_v2(
    evidence: Mapping[str, object],
) -> ReferenceModelV2:
    """Build separated literature and provider collections from Evidence."""

    canonical = build_canonical_references(evidence)
    literature = _renumber_literature(canonical)
    database_references = [
        item
        for item in canonical
        if item["identifier_type"] not in LITERATURE_IDENTIFIER_TYPES
    ]
    by_group: dict[str, CanonicalReference] = {}
    for reference in database_references:
        by_group.setdefault(_reference_group(reference["source"]), reference)

    data_sources: list[DataSourceRecord] = []
    seen: set[tuple[str, str]] = set()
    seen_provider_groups: set[str] = set()

    def add(
        *,
        provider: str,
        capability: str,
        status: object,
        operational_status: object = None,
        provider_role: object = "primary",
        method: object = None,
        fallback_used: object = False,
        primary_failure: object = None,
        dataset: object = None,
        retrieved_at: object = None,
    ) -> None:
        if len(data_sources) >= MAX_REFERENCE_MODEL_ITEMS:
            return
        key = (_provider_group(provider), capability)
        if key in seen:
            return
        seen.add(key)
        seen_provider_groups.add(key[0])
        reference = by_group.get(key[0])
        role: ProviderRole = "fallback" if provider_role == "fallback" else "primary"
        fallback = bool(fallback_used)
        url, record_identifier = _human_link(
            evidence=evidence,
            provider_group=key[0],
            reference=reference,
        )
        data_sources.append(
            {
                "source": _provider_label(provider),
                "capability": capability,
                "status": _status(status),
                "operational_status": (
                    str(operational_status).strip() if operational_status else None
                ),
                "provider_role": role,
                "method": str(method).strip() if method else None,
                "record_identifier": record_identifier,
                "dataset": str(dataset).strip() if dataset else None,
                "human_url": url,
                "link_status": "validated" if url else "unavailable",
                "fallback_used": fallback,
                "primary_failure": (
                    str(primary_failure).strip() if primary_failure else None
                ),
                "retrieved_at": str(retrieved_at).strip() if retrieved_at else None,
            }
        )

    capabilities = evidence.get("capability_results")
    if isinstance(capabilities, Mapping):
        capability_names = [
            *(
                name
                for name in _CAPABILITY_ORDER
                if name in capabilities
            ),
            *sorted(set(capabilities) - set(_CAPABILITY_ORDER)),
        ]
        for capability_name in capability_names:
            raw = capabilities[capability_name]
            if capability_name == "literature" or not isinstance(raw, Mapping):
                continue
            try:
                item = validate_capability_result(raw)
            except ProviderContractError as exc:
                raise ReferenceModelError(
                    f"Invalid capability provenance: {capability_name}."
                ) from exc
            add(
                provider=item["provider"],
                capability=item["capability"],
                status=item["status"],
                operational_status=item["status"],
                provider_role=item["provider_role"],
                method=item["method"],
                fallback_used=item["fallback_used"],
                primary_failure=item["primary_failure"],
                dataset=_optional_provenance_text(item, "dataset"),
                retrieved_at=_optional_provenance_text(item, "retrieved_at"),
            )

    source_statuses = evidence.get("source_statuses")
    if isinstance(source_statuses, Mapping):
        for source_key in _SOURCE_CAPABILITIES:
            source_status = source_statuses.get(source_key)
            if source_status is None:
                continue
            add(
                provider=source_key,
                capability=_SOURCE_CAPABILITIES[source_key],
                status=source_status,
                operational_status=source_status,
            )

    annotations = evidence.get("annotations")
    if isinstance(annotations, Mapping) and "genebe" in annotations:
        genebe = annotations.get("genebe")
        add(
            provider="genebe",
            capability="automated_classification",
            status=(
                genebe.get("status", "not_assessed")
                if isinstance(genebe, Mapping)
                else "not_assessed"
            ),
            operational_status=(
                genebe.get("status") if isinstance(genebe, Mapping) else None
            ),
            method="automated_classification_lookup",
            dataset=(
                genebe.get("provider_version")
                if isinstance(genebe, Mapping)
                else None
            ),
            retrieved_at=(
                genebe.get("retrieved_at")
                if isinstance(genebe, Mapping)
                else None
            ),
        )

    for reference in database_references:
        group = _reference_group(reference["source"])
        if group in seen_provider_groups:
            continue
        add(
            provider=reference["source"],
            capability="database_record",
            status="available",
            operational_status="success",
        )

    model: ReferenceModelV2 = {
        "schema_version": REFERENCE_MODEL_SCHEMA_VERSION,
        "literature_references": literature,
        "data_sources": data_sources,
    }
    return validate_reference_model_v2(model)


def validate_data_source_record(value: object) -> DataSourceRecord:
    if not isinstance(value, dict) or set(value) != DATA_SOURCE_FIELDS:
        raise ReferenceModelError("Data source has invalid fields.")
    source = cast(str, _text(value["source"], "data_source.source"))
    capability = cast(str, _text(value["capability"], "data_source.capability"))
    status = value["status"]
    if status not in DATA_SOURCE_STATUSES:
        raise ReferenceModelError("Data source status is invalid.")
    role = value["provider_role"]
    if role not in {"primary", "fallback"}:
        raise ReferenceModelError("Data source provider role is invalid.")
    if not isinstance(value["fallback_used"], bool):
        raise ReferenceModelError("Data source fallback flag is invalid.")
    if value["fallback_used"] != (role == "fallback"):
        raise ReferenceModelError("Data source fallback provenance is inconsistent.")
    primary_failure = _text(
        value["primary_failure"], "data_source.primary_failure", optional=True
    )
    if role == "fallback" and primary_failure is None:
        raise ReferenceModelError("Fallback data source requires primary failure.")
    if role == "primary" and primary_failure is not None:
        raise ReferenceModelError("Primary data source cannot retain primary failure.")
    url = _text(value["human_url"], "data_source.human_url", optional=True)
    link_status = value["link_status"]
    if link_status not in {"validated", "unavailable"}:
        raise ReferenceModelError("Data source link status is invalid.")
    if (url is None) != (link_status == "unavailable"):
        raise ReferenceModelError("Data source link status is inconsistent.")
    if url is not None and validated_reference_url(url) != url:
        raise ReferenceModelError("Data source URL is not validated.")
    if url is not None and is_machine_readable_url(source, url):
        raise ReferenceModelError("Data source URL is machine-readable.")
    return cast(
        DataSourceRecord,
        {
            "source": source,
            "capability": capability,
            "status": status,
            "operational_status": _text(
                value["operational_status"],
                "data_source.operational_status",
                optional=True,
            ),
            "provider_role": role,
            "method": _text(value["method"], "data_source.method", optional=True),
            "record_identifier": _text(
                value["record_identifier"],
                "data_source.record_identifier",
                optional=True,
            ),
            "dataset": _text(
                value["dataset"], "data_source.dataset", optional=True
            ),
            "human_url": url,
            "link_status": link_status,
            "fallback_used": value["fallback_used"],
            "primary_failure": primary_failure,
            "retrieved_at": _text(
                value["retrieved_at"],
                "data_source.retrieved_at",
                optional=True,
            ),
        },
    )


def validate_reference_model_v2(value: object) -> ReferenceModelV2:
    if not isinstance(value, dict) or set(value) != REFERENCE_MODEL_FIELDS:
        raise ReferenceModelError("Reference Model V2 has invalid fields.")
    if value["schema_version"] != REFERENCE_MODEL_SCHEMA_VERSION:
        raise ReferenceModelError("Reference Model V2 schema is unsupported.")
    raw_literature = value["literature_references"]
    raw_sources = value["data_sources"]
    if (
        not isinstance(raw_literature, Sequence)
        or isinstance(raw_literature, (str, bytes))
        or len(raw_literature) > MAX_REFERENCE_MODEL_ITEMS
    ):
        raise ReferenceModelError("Literature references must be a bounded list.")
    literature: list[CanonicalReference] = []
    seen_literature: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_literature):
        try:
            reference = validate_canonical_reference(raw)
        except CanonicalReferenceError as exc:
            raise ReferenceModelError("Literature reference is invalid.") from exc
        if reference["identifier_type"] not in LITERATURE_IDENTIFIER_TYPES:
            raise ReferenceModelError("Database/tool source found in literature.")
        if reference["reference_id"] != f"R{index + 1}":
            raise ReferenceModelError("Literature numbering is not contiguous.")
        literature_key = (
            reference["identifier_type"] or "",
            reference["identifier"] or reference["canonical_url"] or "",
        )
        if literature_key in seen_literature:
            raise ReferenceModelError("Literature references must be unique.")
        seen_literature.add(literature_key)
        literature.append(reference)
    if (
        not isinstance(raw_sources, Sequence)
        or isinstance(raw_sources, (str, bytes))
        or len(raw_sources) > MAX_REFERENCE_MODEL_ITEMS
    ):
        raise ReferenceModelError("Data sources must be a bounded list.")
    sources = [validate_data_source_record(item) for item in raw_sources]
    source_keys: set[tuple[str, str]] = set()
    for source in sources:
        if source["source"].casefold() in LITERATURE_SOURCE_NAMES:
            raise ReferenceModelError("Literature provider found in data sources.")
        key = (source["source"].casefold(), source["capability"].casefold())
        if key in source_keys:
            raise ReferenceModelError("Data sources must be unique.")
        source_keys.add(key)
    return {
        "schema_version": REFERENCE_MODEL_SCHEMA_VERSION,
        "literature_references": literature,
        "data_sources": sources,
    }


__all__ = [
    "DataSourceRecord",
    "LITERATURE_IDENTIFIER_TYPES",
    "REFERENCE_MODEL_SCHEMA_VERSION",
    "ReferenceModelError",
    "ReferenceModelV2",
    "build_reference_model_v2",
    "validate_data_source_record",
    "validate_reference_model_v2",
]
