"""Bounded MyDisease.info gene-disease-phenotype context."""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

import requests

from backend.provider_resilience import (
    ProviderCircuitState,
    ProviderInvalidResponseError,
    ProviderRetryPolicy,
    ProviderTimeouts,
    call_provider_with_policy,
)
from config import settings


MYDISEASE_PROVIDER_NAME = "MyDisease.info"
MYDISEASE_SCHEMA_VERSION = "stage28-v1"
MYDISEASE_QUERY_FIELDS = (
    "_id",
    "mondo.mondo",
    "mondo.label",
    "mondo.synonym",
    "mondo.has_material_basis_in_germline_mutation_in",
    "mondo.xrefs",
    "disease_ontology.doid",
    "disease_ontology.name",
    "disease_ontology.synonyms",
    "disease_ontology.xrefs",
    "hpo.omim",
    "hpo.phenotype_related_to_disease",
    "hpo.inheritance",
    "hpo.clinical_course",
    "hpo.clinical_modifier",
    "ctd.pathway_related_to_disease",
    "umls.umls",
)
MYDISEASE_MAX_PATHWAY_CONTEXTS = 10
MYDISEASE_MAX_SYNONYMS = 20
MYDISEASE_MAX_CROSS_REFERENCES = 20
MYDISEASE_MAX_REFERENCE_ITEMS = 20
MYDISEASE_GENE_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
)
MYDISEASE_HGNC_PATTERN = re.compile(r"HGNC:[1-9][0-9]*")
MYDISEASE_HPO_PATTERN = re.compile(r"HP:[0-9]{7}")
MYDISEASE_MONDO_PATTERN = re.compile(r"MONDO:[0-9]{7}")
MYDISEASE_DOID_PATTERN = re.compile(r"DOID:[0-9]+")
MYDISEASE_HGNC_URL_PATTERN = re.compile(
    r"https?://identifiers\.org/hgnc/([1-9][0-9]*)",
    re.IGNORECASE,
)
_CACHE: OrderedDict[tuple[object, ...], dict[str, Any]] = OrderedDict()
_CACHE_LOCK = threading.RLock()


MyDiseaseStatus = Literal[
    "available",
    "partial",
    "no_association",
    "unsupported",
    "unavailable",
    "invalid_response",
]
PhenotypeMatchStatus = Literal[
    "exact_match",
    "partial_match",
    "no_exact_match",
    "no_disease_hpo_annotations",
    "unavailable",
]


class MyDiseaseError(RuntimeError):
    """Raised when a MyDisease request or response is unusable."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        kind: str = "invalid_response",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.kind = kind
        self.status_code = status_code


class MyDiseaseEvidence(TypedDict):
    """Normalized MyDisease evidence attached to one variant."""

    status: MyDiseaseStatus
    provider: str
    provider_version: str | None
    retrieved_at: str | None
    query_gene: str | None
    query_gene_id: str | None
    query: str | None
    http_status: int | None
    provider_total: int | None
    provider_returned_count: int
    disease_count: int
    diseases: list[dict[str, Any]]
    inferred_pathway_context: list[dict[str, Any]]
    upstream_sources: list[str]
    warnings: list[str]
    failure_reason: str | None
    cache_state: Literal["hit", "miss", "not_applicable"]


class MyDiseaseEnrichmentResult(TypedDict):
    """Analysis-level result with evidence for every input variant."""

    variants: list[dict[str, Any]]
    status: MyDiseaseStatus
    message: str
    request_attempts: int
    variant_count: int
    variants_with_evidence: int
    no_association_count: int
    unsupported_count: int
    unavailable_count: int
    invalid_response_count: int


def _safe_text(
    value: object,
    *,
    maximum_length: int = 500,
) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if (
        not text
        or len(text) > maximum_length
        or any(ord(character) < 32 for character in text)
    ):
        return None
    return text


def _as_items(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, Mapping):
        return [value]
    if value is None:
        return []
    return [value]


def _bounded_strings(
    values: Sequence[object],
    *,
    limit: int,
    prefix: str | None = None,
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_text(value, maximum_length=200)
        if text is None:
            continue
        if prefix is not None and text.startswith(prefix):
            text = text[len(prefix) :]
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return sorted(result)[:limit]


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class _ParsedJSONResponse:
    status_code: int
    headers: Mapping[str, object]
    payload: Mapping[str, object]


def _request_json(
    session: requests.Session,
    path: str,
    *,
    params: Mapping[str, object] | None,
    operation: str,
    circuit_state: ProviderCircuitState,
) -> tuple[Mapping[str, object], int, int]:
    url = f"{settings.MYDISEASE_BASE_URL}/{path.lstrip('/')}"
    timeout_seconds = float(settings.MYDISEASE_TIMEOUT)

    def request(
        timeout: tuple[float, float],
        _attempt: int,
    ) -> object:
        response = session.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return response
        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            raise ProviderInvalidResponseError(
                http_status=response.status_code
            ) from error
        if not isinstance(payload, Mapping):
            raise ProviderInvalidResponseError(
                http_status=response.status_code
            )
        headers = getattr(response, "headers", {})
        return _ParsedJSONResponse(
            status_code=response.status_code,
            headers=headers if isinstance(headers, Mapping) else {},
            payload=payload,
        )

    result = call_provider_with_policy(
        provider="mydisease",
        operation_name=operation,
        operation=request,
        timeouts=ProviderTimeouts(
            connect=min(timeout_seconds, 5.0),
            read=timeout_seconds,
        ),
        retry_policy=ProviderRetryPolicy(
            max_attempts=min(settings.MYDISEASE_MAX_RETRIES + 1, 2),
            backoff_base_seconds=0.25,
            backoff_max_seconds=2.0,
            max_retry_after_seconds=2.0,
        ),
        circuit_state=circuit_state,
    )
    if result.status == "success" and isinstance(
        result.value,
        _ParsedJSONResponse,
    ):
        return result.value.payload, result.attempts, result.value.status_code
    kind = {
        "timeout": "timeout",
        "invalid_response": "invalid_response",
        "unavailable": "network" if result.http_status is None else "http_error",
    }.get(result.status, "http_error")
    raise MyDiseaseError(
        "MyDisease request did not complete successfully.",
        attempts=result.attempts,
        kind=kind,
        status_code=result.http_status,
    )


def _normalize_gene(value: object) -> str | None:
    text = _safe_text(value, maximum_length=64)
    if text is None or MYDISEASE_GENE_PATTERN.fullmatch(text) is None:
        return None
    return text


def _hgnc_curie(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return f"HGNC:{value}"
    text = _safe_text(value, maximum_length=100)
    if text is None or MYDISEASE_HGNC_PATTERN.fullmatch(text) is None:
        return None
    return text


def _same_gene(value: object, gene: str) -> bool:
    candidate = _normalize_gene(value)
    return (
        candidate is not None
        and candidate.casefold() == gene.casefold()
    )


def _annotation_hgnc_curie(
    annotation: Mapping[str, object],
    gene: str,
) -> str | None:
    candidates: set[str] = set()
    top_level = _hgnc_curie(annotation.get("gene_id"))
    if top_level is not None:
        candidates.add(top_level)
    sources = annotation.get("sources")
    if isinstance(sources, Mapping):
        genebe = sources.get("genebe")
        if (
            isinstance(genebe, Mapping)
            and genebe.get("status") == "success"
            and _same_gene(genebe.get("gene"), gene)
        ):
            candidate = _hgnc_curie(genebe.get("gene_hgnc_id"))
            if candidate is not None:
                candidates.add(candidate)
        clingen = sources.get("clingen")
        if (
            isinstance(clingen, Mapping)
            and clingen.get("status") == "success"
            and _same_gene(clingen.get("gene"), gene)
        ):
            candidate = _hgnc_curie(clingen.get("gene_id"))
            if candidate is not None:
                candidates.add(candidate)
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _query_for_gene(gene: str) -> str:
    escaped = re.sub(r"([+\-=!(){}\[\]^\"~?:\\/])", r"\\\1", gene)
    return f"mondo.synonym.exact:{escaped}*"


def _query_params(gene: str) -> dict[str, object]:
    query_size = min(
        max(settings.MYDISEASE_MAX_DISEASES_PER_GENE * 5, 10),
        100,
    )
    return {
        "q": _query_for_gene(gene),
        "fields": ",".join(MYDISEASE_QUERY_FIELDS),
        "size": query_size,
        "from": 0,
    }


def _metadata(
    session: requests.Session,
    circuit_state: ProviderCircuitState,
) -> tuple[str | None, int, str | None]:
    try:
        payload, attempts, _status = _request_json(
            session,
            "metadata",
            params=None,
            operation="get_metadata",
            circuit_state=circuit_state,
        )
    except MyDiseaseError as error:
        return (
            None,
            error.attempts,
            "MyDisease build metadata was unavailable.",
        )
    version = _safe_text(payload.get("build_version"), maximum_length=100)
    return (
        version,
        attempts,
        None
        if version is not None
        else "MyDisease build metadata was unavailable.",
    )


def _material_hgnc_ids(mondo: Mapping[str, object]) -> set[str]:
    relation = mondo.get(
        "has_material_basis_in_germline_mutation_in"
    )
    values: list[object] = []
    for item in _as_items(relation):
        if isinstance(item, Mapping):
            for nested in item.values():
                values.extend(_as_items(nested))
        else:
            values.append(item)
    identifiers: set[str] = set()
    for value in values:
        text = _safe_text(value, maximum_length=300)
        if text is None:
            continue
        if MYDISEASE_HGNC_PATTERN.fullmatch(text):
            identifiers.add(text)
            continue
        match = MYDISEASE_HGNC_URL_PATTERN.fullmatch(text)
        if match is not None:
            identifiers.add(f"HGNC:{match.group(1)}")
    return identifiers


def _cross_reference_values(
    *sources: Mapping[str, object],
    keys: Sequence[str],
    strip_prefix: str | None = None,
) -> list[str]:
    values: list[object] = []
    for source in sources:
        for key in keys:
            values.extend(_as_items(source.get(key)))
    return _bounded_strings(
        values,
        limit=MYDISEASE_MAX_CROSS_REFERENCES,
        prefix=strip_prefix,
    )


def _normalize_biocuration(value: object) -> list[dict[str, str | None]]:
    result: list[dict[str, str | None]] = []
    for item in _as_items(value):
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "name": _safe_text(
                    item.get("name"),
                    maximum_length=100,
                ),
                "date": _safe_text(
                    item.get("date"),
                    maximum_length=32,
                ),
            }
        )
    return result[:MYDISEASE_MAX_REFERENCE_ITEMS]


def _normalize_hpo_terms(value: object) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in _as_items(value):
        if not isinstance(item, Mapping):
            continue
        hpo_id = _safe_text(item.get("hpo_id"), maximum_length=20)
        if hpo_id is None or MYDISEASE_HPO_PATTERN.fullmatch(
            hpo_id
        ) is None:
            continue
        numerator = item.get("freq_numerator")
        denominator = item.get("freq_denominator")
        numeric_frequency = item.get("numeric_freq")
        normalized.append(
            {
                "hpo_id": hpo_id,
                "hpo_name": _safe_text(
                    item.get("hpo_name"),
                    maximum_length=200,
                ),
                "evidence_code": _safe_text(
                    item.get("evidence"),
                    maximum_length=50,
                ),
                "numeric_frequency": (
                    float(numeric_frequency)
                    if isinstance(numeric_frequency, (int, float))
                    and not isinstance(numeric_frequency, bool)
                    else None
                ),
                "frequency_numerator": (
                    numerator
                    if isinstance(numerator, int)
                    and not isinstance(numerator, bool)
                    and numerator >= 0
                    else None
                ),
                "frequency_denominator": (
                    denominator
                    if isinstance(denominator, int)
                    and not isinstance(denominator, bool)
                    and denominator >= 0
                    else None
                ),
                "original_disease_id": _safe_text(
                    item.get("original_disease_id"),
                    maximum_length=100,
                ),
                "omim_refs": _bounded_strings(
                    _as_items(item.get("omim_refs")),
                    limit=MYDISEASE_MAX_REFERENCE_ITEMS,
                ),
                "pmid_refs": _bounded_strings(
                    _as_items(item.get("pmid_refs")),
                    limit=MYDISEASE_MAX_REFERENCE_ITEMS,
                ),
                "biocuration": _normalize_biocuration(
                    item.get("biocuration")
                ),
                "upstream_source": "HPO",
            }
        )
    normalized.sort(
        key=lambda item: (
            item["hpo_id"],
            item["evidence_code"] or "",
            item["original_disease_id"] or "",
        )
    )
    unique: dict[str, dict[str, Any]] = {}
    for item in normalized:
        unique.setdefault(item["hpo_id"], item)
    return list(unique.values())[:200]


def _normalize_pathway_context(
    hit: Mapping[str, object],
    gene: str,
    disease_id: str | None,
) -> list[dict[str, Any]]:
    pathways = _mapping(hit.get("ctd")).get(
        "pathway_related_to_disease"
    )
    result: list[dict[str, Any]] = []
    for item in _as_items(pathways):
        if (
            not isinstance(item, Mapping)
            or not _same_gene(
                item.get("inference_gene_symbol"),
                gene,
            )
        ):
            continue
        path_id = (
            _safe_text(
                item.get("kegg_pathway_id"),
                maximum_length=100,
            )
            or _safe_text(
                item.get("react_pathway_id"),
                maximum_length=100,
            )
        )
        result.append(
            {
                "disease_id": disease_id,
                "association_type": "inferred_pathway_context",
                "classification_effect": "context_only",
                "inference_gene": gene,
                "pathway_id": path_id,
                "pathway_name": _safe_text(
                    item.get("pathway_name"),
                    maximum_length=200,
                ),
                "provider": MYDISEASE_PROVIDER_NAME,
                "upstream_source": "CTD",
            }
        )
    result.sort(
        key=lambda item: (
            item["disease_id"] or "",
            item["pathway_id"] or "",
            item["pathway_name"] or "",
        )
    )
    return result[:MYDISEASE_MAX_PATHWAY_CONTEXTS]


def _normalize_direct_disease(
    hit: Mapping[str, object],
    *,
    gene: str,
    gene_id: str,
) -> dict[str, Any] | None:
    mondo = _mapping(hit.get("mondo"))
    matched_ids = _material_hgnc_ids(mondo)
    if gene_id not in matched_ids:
        return None
    mondo_id = _safe_text(mondo.get("mondo"), maximum_length=30)
    hit_id = _safe_text(hit.get("_id"), maximum_length=100)
    if (
        mondo_id is None
        or MYDISEASE_MONDO_PATTERN.fullmatch(mondo_id) is None
    ):
        mondo_id = (
            hit_id
            if hit_id is not None
            and MYDISEASE_MONDO_PATTERN.fullmatch(hit_id)
            else None
        )

    disease_ontology = _mapping(hit.get("disease_ontology"))
    mondo_xrefs = _mapping(mondo.get("xrefs"))
    do_xrefs = _mapping(disease_ontology.get("xrefs"))
    umls = _mapping(hit.get("umls"))
    doids = _cross_reference_values(
        mondo_xrefs,
        disease_ontology,
        keys=("doid",),
    )
    if not doids:
        doids = _cross_reference_values(do_xrefs, keys=("doid",))
    omim = _cross_reference_values(
        mondo_xrefs,
        do_xrefs,
        _mapping(hit.get("hpo")),
        keys=("omim", "mim"),
        strip_prefix="OMIM:",
    )
    medgen = _cross_reference_values(
        mondo_xrefs,
        keys=("medgen",),
    )
    disease_id = (
        mondo_id
        or (doids[0] if doids else None)
        or (f"OMIM:{omim[0]}" if omim else None)
        or (f"MedGen:{medgen[0]}" if medgen else None)
        or hit_id
    )
    if disease_id is None:
        return None

    mondo_synonym = _mapping(mondo.get("synonym"))
    do_synonyms = _mapping(disease_ontology.get("synonyms"))
    synonyms = _bounded_strings(
        [
            *_as_items(mondo_synonym.get("exact")),
            *_as_items(mondo_synonym.get("related")),
            *_as_items(do_synonyms.get("exact")),
            *_as_items(do_synonyms.get("related")),
        ],
        limit=MYDISEASE_MAX_SYNONYMS,
    )
    hpo = _mapping(hit.get("hpo"))
    supporting_hpo = _normalize_hpo_terms(
        hpo.get("phenotype_related_to_disease")
    )
    upstream_sources = ["MONDO"]
    if disease_ontology:
        upstream_sources.append("Disease Ontology")
    if hpo:
        upstream_sources.append("HPO")
    if umls:
        upstream_sources.append("UMLS")

    return {
        "disease_id": disease_id,
        "disease_name": (
            _safe_text(mondo.get("label"), maximum_length=300)
            or _safe_text(
                disease_ontology.get("name"),
                maximum_length=300,
            )
        ),
        "synonyms": synonyms,
        "primary_source": "MONDO" if mondo_id is not None else (
            "Disease Ontology"
            if doids
            else MYDISEASE_PROVIDER_NAME
        ),
        "cross_references": {
            "doid": doids,
            "omim": omim,
            "medgen": medgen,
            "gard": _cross_reference_values(
                mondo_xrefs,
                do_xrefs,
                keys=("gard",),
            ),
            "mesh": _cross_reference_values(
                mondo_xrefs,
                do_xrefs,
                keys=("mesh",),
            ),
            "umls": _cross_reference_values(
                mondo_xrefs,
                do_xrefs,
                umls,
                keys=("umls", "umls_cui"),
            ),
        },
        "gene_disease_relation": {
            "association_type": "direct_gene_disease",
            "relation": "germline_mutation_basis",
            "requested_gene": gene,
            "requested_gene_id": gene_id,
            "matched_gene_id": gene_id,
            "provider": MYDISEASE_PROVIDER_NAME,
            "upstream_source": "MONDO",
            "is_direct": True,
            "classification_effect": "context_only",
        },
        "supporting_hpo_terms": supporting_hpo,
        "inheritance": _normalize_hpo_terms(hpo.get("inheritance")),
        "clinical_course": _normalize_hpo_terms(
            hpo.get("clinical_course")
        ),
        "clinical_modifier": _normalize_hpo_terms(
            hpo.get("clinical_modifier")
        ),
        "upstream_sources": sorted(set(upstream_sources)),
        "warnings": [],
    }


def _lookup_gene(
    session: requests.Session,
    *,
    gene: str,
    gene_id: str,
    circuit_state: ProviderCircuitState,
) -> tuple[dict[str, Any], int]:
    params = _query_params(gene)
    payload, attempts, status_code = _request_json(
        session,
        "query",
        params=params,
        operation="query_gene",
        circuit_state=circuit_state,
    )
    total = payload.get("total")
    hits = payload.get("hits")
    size = params["size"]
    if (
        isinstance(total, bool)
        or not isinstance(total, int)
        or total < 0
        or not isinstance(hits, list)
        or not isinstance(size, int)
        or len(hits) > size
        or total < len(hits)
    ):
        raise MyDiseaseError(
            "MyDisease returned an unexpected query schema.",
            attempts=attempts,
            kind="invalid_response",
            status_code=status_code,
        )

    diseases: list[dict[str, Any]] = []
    pathways: list[dict[str, Any]] = []
    invalid_hit_count = 0
    for hit in hits:
        if not isinstance(hit, Mapping):
            invalid_hit_count += 1
            continue
        hit_id = _safe_text(hit.get("_id"), maximum_length=100)
        pathways.extend(
            _normalize_pathway_context(hit, gene, hit_id)
        )
        disease = _normalize_direct_disease(
            hit,
            gene=gene,
            gene_id=gene_id,
        )
        if disease is not None:
            diseases.append(disease)
    if hits and invalid_hit_count == len(hits):
        raise MyDiseaseError(
            "MyDisease returned unusable disease hits.",
            attempts=attempts,
            kind="invalid_response",
            status_code=status_code,
        )

    unique_diseases: dict[str, dict[str, Any]] = {}
    for disease in sorted(
        diseases,
        key=lambda item: item["disease_id"],
    ):
        unique_diseases.setdefault(disease["disease_id"], disease)
    unique_pathways: dict[tuple[object, ...], dict[str, Any]] = {}
    for pathway in pathways:
        key = (
            pathway["disease_id"],
            pathway["pathway_id"],
            pathway["pathway_name"],
        )
        unique_pathways.setdefault(key, pathway)
    warnings: list[str] = []
    if invalid_hit_count:
        warnings.append(
            "Some MyDisease hits were malformed and were discarded."
        )
    if total > len(hits):
        warnings.append(
            "MyDisease query results were bounded before normalization."
        )
    return (
        {
            "status": (
                "available"
                if unique_diseases
                else "no_association"
            ),
            "query": params["q"],
            "http_status": status_code,
            "provider_total": total,
            "provider_returned_count": len(hits),
            "diseases": list(unique_diseases.values()),
            "inferred_pathway_context": list(
                unique_pathways.values()
            )[:MYDISEASE_MAX_PATHWAY_CONTEXTS],
            "warnings": warnings,
        },
        attempts,
    )


def _cache_key(
    gene: str,
    gene_id: str,
    provider_version: str | None,
) -> tuple[object, ...]:
    return (
        gene.casefold(),
        gene_id,
        settings.MYDISEASE_BASE_URL,
        tuple(MYDISEASE_QUERY_FIELDS),
        settings.MYDISEASE_MAX_DISEASES_PER_GENE,
        settings.MYDISEASE_MAX_HPO_TERMS_PER_DISEASE,
        provider_version,
        MYDISEASE_SCHEMA_VERSION,
    )


def _cache_get(key: tuple[object, ...]) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        value = _CACHE.get(key)
        if value is None:
            return None
        _CACHE.move_to_end(key)
        return deepcopy(value)


def _cache_put(
    key: tuple[object, ...],
    value: Mapping[str, object],
) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = deepcopy(dict(value))
        _CACHE.move_to_end(key)
        while len(_CACHE) > settings.MYDISEASE_CACHE_SIZE:
            _CACHE.popitem(last=False)


def clear_mydisease_cache() -> None:
    """Clear the bounded process-local normalized lookup cache."""

    with _CACHE_LOCK:
        _CACHE.clear()


def _match_disease(
    disease: Mapping[str, object],
    patient_hpo_terms: Sequence[str],
) -> dict[str, Any]:
    result = deepcopy(dict(disease))
    supporting = result.get("supporting_hpo_terms")
    supporting_items = (
        supporting if isinstance(supporting, list) else []
    )
    disease_hpo = {
        item["hpo_id"]
        for item in supporting_items
        if isinstance(item, Mapping)
        and isinstance(item.get("hpo_id"), str)
    }
    patient = list(dict.fromkeys(patient_hpo_terms))
    matched = [term for term in patient if term in disease_hpo]
    unmatched = [term for term in patient if term not in disease_hpo]
    supporting_items = sorted(
        supporting_items,
        key=lambda item: (
            -int(
                isinstance(item, Mapping)
                and item.get("hpo_id") in patient
            ),
            str(item.get("hpo_id", ""))
            if isinstance(item, Mapping)
            else "",
        ),
    )[: settings.MYDISEASE_MAX_HPO_TERMS_PER_DISEASE]
    status: PhenotypeMatchStatus
    if not supporting_items:
        status = "no_disease_hpo_annotations"
    elif patient and len(matched) == len(patient):
        status = "exact_match"
    elif matched:
        status = "partial_match"
    else:
        status = "no_exact_match"
    result.update(
        {
            "supporting_hpo_terms": supporting_items,
            "matched_patient_hpo_terms": matched,
            "unmatched_patient_hpo_terms": unmatched,
            "phenotype_match_count": len(matched),
            "phenotype_match_status": status,
        }
    )
    return result


def _evidence_from_lookup(
    lookup: Mapping[str, object],
    *,
    gene: str,
    gene_id: str,
    patient_hpo_terms: Sequence[str],
    provider_version: str | None,
    metadata_warning: str | None,
    cache_state: Literal["hit", "miss"],
) -> MyDiseaseEvidence:
    raw_diseases = lookup.get("diseases")
    diseases = [
        _match_disease(disease, patient_hpo_terms)
        for disease in (
            raw_diseases if isinstance(raw_diseases, list) else []
        )
        if isinstance(disease, Mapping)
    ]
    diseases.sort(
        key=lambda item: (
            -int(item["phenotype_match_count"] > 0),
            item["disease_id"],
        )
    )
    diseases = diseases[: settings.MYDISEASE_MAX_DISEASES_PER_GENE]
    warnings = [
        warning
        for warning in (
            lookup.get("warnings")
            if isinstance(lookup.get("warnings"), list)
            else []
        )
        if isinstance(warning, str)
    ]
    if metadata_warning is not None:
        warnings.append(metadata_warning)
    upstream_sources = {
        source
        for disease in diseases
        for source in disease.get("upstream_sources", [])
        if isinstance(source, str)
    }
    pathways = lookup.get("inferred_pathway_context")
    pathway_items = (
        deepcopy(pathways) if isinstance(pathways, list) else []
    )
    if pathway_items:
        upstream_sources.add("CTD")
    return {
        "status": lookup["status"],  # type: ignore[typeddict-item]
        "provider": MYDISEASE_PROVIDER_NAME,
        "provider_version": provider_version,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query_gene": gene,
        "query_gene_id": gene_id,
        "query": _safe_text(lookup.get("query"), maximum_length=300),
        "http_status": (
            lookup.get("http_status")
            if isinstance(lookup.get("http_status"), int)
            else None
        ),
        "provider_total": (
            lookup.get("provider_total")
            if isinstance(lookup.get("provider_total"), int)
            else None
        ),
        "provider_returned_count": (
            lookup.get("provider_returned_count")
            if isinstance(
                lookup.get("provider_returned_count"),
                int,
            )
            else 0
        ),
        "disease_count": len(diseases),
        "diseases": diseases,
        "inferred_pathway_context": pathway_items,
        "upstream_sources": sorted(upstream_sources),
        "warnings": warnings,
        "failure_reason": None,
        "cache_state": cache_state,
    }


def _missing_evidence(
    *,
    status: MyDiseaseStatus,
    gene: str | None,
    gene_id: str | None,
    warning: str,
    failure_reason: str | None,
    http_status: int | None = None,
    provider_version: str | None = None,
) -> MyDiseaseEvidence:
    return {
        "status": status,
        "provider": MYDISEASE_PROVIDER_NAME,
        "provider_version": provider_version,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "query_gene": gene,
        "query_gene_id": gene_id,
        "query": None,
        "http_status": http_status,
        "provider_total": None,
        "provider_returned_count": 0,
        "disease_count": 0,
        "diseases": [],
        "inferred_pathway_context": [],
        "upstream_sources": [],
        "warnings": [warning],
        "failure_reason": failure_reason,
        "cache_state": "not_applicable",
    }


def _status_from_error(error: MyDiseaseError) -> MyDiseaseStatus:
    return (
        "invalid_response"
        if error.kind == "invalid_response"
        else "unavailable"
    )


def enrich_with_mydisease(
    variants: Sequence[Mapping[str, object]],
    patient_hpo_terms: Sequence[str],
    *,
    session: requests.Session | None = None,
) -> MyDiseaseEnrichmentResult:
    """Attach independent bounded context without changing variant order."""

    if not isinstance(variants, Sequence) or isinstance(
        variants, (str, bytes)
    ):
        raise MyDiseaseError("MyDisease variants must be a sequence.")
    if not isinstance(patient_hpo_terms, Sequence) or isinstance(
        patient_hpo_terms, (str, bytes)
    ):
        raise MyDiseaseError("MyDisease HPO terms must be a sequence.")

    items = [dict(item) for item in variants]
    genes = [_normalize_gene(item.get("gene")) for item in items]
    patient_hpo = [
        term
        for term in dict.fromkeys(patient_hpo_terms)
        if isinstance(term, str)
        and MYDISEASE_HPO_PATTERN.fullmatch(term)
    ]
    identifiers: dict[str, str] = {}
    for target_gene in dict.fromkeys(gene for gene in genes if gene):
        candidates = {
            candidate
            for item, gene in zip(items, genes)
            if gene is not None
            and gene.casefold() == target_gene.casefold()
            and (
                candidate := _annotation_hgnc_curie(
                    item,
                    target_gene,
                )
            )
            is not None
        }
        if len(candidates) == 1:
            identifiers[target_gene.casefold()] = next(iter(candidates))

    target_gene_by_key: dict[str, str] = {}
    for gene in genes:
        if gene is not None:
            target_gene_by_key.setdefault(gene.casefold(), gene)
    target_genes = list(target_gene_by_key.values())
    owns_session = session is None
    client = session or requests.Session()
    circuit_state = ProviderCircuitState()
    attempts = 0
    provider_version: str | None = None
    metadata_warning: str | None = None
    evidence_by_gene: dict[str, MyDiseaseEvidence] = {}
    try:
        if target_genes:
            (
                provider_version,
                metadata_attempts,
                metadata_warning,
            ) = _metadata(client, circuit_state)
            attempts += metadata_attempts
        for gene in target_genes:
            gene_id = identifiers.get(gene.casefold())
            if gene_id is None:
                evidence_by_gene[gene.casefold()] = _missing_evidence(
                    status="unsupported",
                    gene=gene,
                    gene_id=None,
                    warning=(
                        "MyDisease was not queried because no unambiguous "
                        "HGNC identifier was available."
                    ),
                    failure_reason="unsupported_gene_identifier",
                    provider_version=provider_version,
                )
                continue
            key = _cache_key(gene, gene_id, provider_version)
            lookup = _cache_get(key)
            cache_state: Literal["hit", "miss"] = "hit"
            if lookup is None:
                cache_state = "miss"
                try:
                    lookup, request_attempts = _lookup_gene(
                        client,
                        gene=gene,
                        gene_id=gene_id,
                        circuit_state=circuit_state,
                    )
                    attempts += request_attempts
                except MyDiseaseError as error:
                    attempts += error.attempts
                    evidence_by_gene[
                        gene.casefold()
                    ] = _missing_evidence(
                        status=_status_from_error(error),
                        gene=gene,
                        gene_id=gene_id,
                        warning=(
                            "MyDisease context was unavailable for "
                            "this gene."
                        ),
                        failure_reason=error.kind,
                        http_status=error.status_code,
                        provider_version=provider_version,
                    )
                    continue
                _cache_put(key, lookup)
            evidence_by_gene[gene.casefold()] = _evidence_from_lookup(
                lookup,
                gene=gene,
                gene_id=gene_id,
                patient_hpo_terms=patient_hpo,
                provider_version=provider_version,
                metadata_warning=metadata_warning,
                cache_state=cache_state,
            )
    finally:
        if owns_session:
            client.close()

    enriched_variants: list[dict[str, Any]] = []
    statuses: list[MyDiseaseStatus] = []
    for item, gene in zip(items, genes):
        enriched = deepcopy(item)
        evidence = (
            evidence_by_gene.get(gene.casefold())
            if gene is not None
            else None
        )
        if evidence is None:
            evidence = _missing_evidence(
                status="unsupported",
                gene=gene,
                gene_id=None,
                warning=(
                    "MyDisease was not queried because this variant "
                    "had no usable annotated gene."
                ),
                failure_reason="unsupported_gene",
                provider_version=provider_version,
            )
        enriched["mydisease"] = deepcopy(evidence)
        enriched_variants.append(enriched)
        statuses.append(evidence["status"])

    variant_count = len(enriched_variants)
    variants_with_evidence = statuses.count("available")
    no_association_count = statuses.count("no_association")
    unsupported_count = statuses.count("unsupported")
    unavailable_count = statuses.count("unavailable")
    invalid_response_count = statuses.count("invalid_response")
    if statuses and all(status == "available" for status in statuses):
        overall: MyDiseaseStatus = "available"
    elif statuses and all(
        status == "no_association" for status in statuses
    ):
        overall = "no_association"
    elif statuses and all(status == "unsupported" for status in statuses):
        overall = "unsupported"
    elif statuses and all(status == "unavailable" for status in statuses):
        overall = "unavailable"
    elif statuses and all(
        status == "invalid_response" for status in statuses
    ):
        overall = "invalid_response"
    elif not statuses:
        overall = "unsupported"
    else:
        overall = "partial"

    if overall == "available":
        message = (
            "MyDisease.info returned bounded gene-disease-phenotype "
            f"context for {variants_with_evidence} of {variant_count} "
            "variants."
        )
    elif overall == "no_association":
        message = (
            "MyDisease.info completed successfully; no validated direct "
            "gene-disease association was retained for "
            f"{no_association_count} of {variant_count} variants."
        )
    elif overall == "unsupported":
        message = (
            "MyDisease.info was not called because "
            f"{unsupported_count} of {variant_count} variants lacked a "
            "supported gene identifier."
        )
    elif overall == "unavailable":
        message = (
            f"MyDisease.info was unavailable for {variant_count} of "
            f"{variant_count} variants; existing annotation, HPO, and "
            "Phen2Gene evidence were retained."
        )
    elif overall == "invalid_response":
        message = (
            "MyDisease.info returned an invalid response for "
            f"{variant_count} of {variant_count} variants; existing "
            "annotation, HPO, and Phen2Gene evidence were retained."
        )
    else:
        message = (
            "MyDisease.info returned evidence for "
            f"{variants_with_evidence} of {variant_count} variants; "
            f"{no_association_count} had no validated association, "
            f"{unsupported_count} were unsupported, "
            f"{unavailable_count} were unavailable, and "
            f"{invalid_response_count} had invalid responses."
        )
    return {
        "variants": enriched_variants,
        "status": overall,
        "message": message,
        "request_attempts": attempts,
        "variant_count": variant_count,
        "variants_with_evidence": variants_with_evidence,
        "no_association_count": no_association_count,
        "unsupported_count": unsupported_count,
        "unavailable_count": unavailable_count,
        "invalid_response_count": invalid_response_count,
    }


__all__ = [
    "MYDISEASE_PROVIDER_NAME",
    "MYDISEASE_QUERY_FIELDS",
    "MyDiseaseEnrichmentResult",
    "MyDiseaseError",
    "MyDiseaseEvidence",
    "MyDiseaseStatus",
    "clear_mydisease_cache",
    "enrich_with_mydisease",
]
