"""Bounded, source-separated NCBI MedGen disease/HPO and phenotype-gene context retrieval."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict
from xml.etree import ElementTree

import requests

from backend.provider_resilience import (
    ProviderCircuitState,
    ProviderInvalidResponseError,
    ProviderRetryPolicy,
    ProviderTimeouts,
    call_provider_with_policy,
)
from config import settings


MEDGEN_PROVIDER = "ncbi_medgen"
MEDGEN_PROVIDER_NAME = "NCBI MedGen"
MEDGEN_CONTEXT_SCHEMA_VERSION = "1.1"
MEDGEN_MAX_RECORDS = 10
MEDGEN_GENE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MEDGEN_HPO_PATTERN = re.compile(r"^HP:\d{7}$")
MEDGEN_CONCEPTMETA_MAX_BYTES = 20_000

MedGenStatus = Literal[
    "success", "no_match", "unavailable", "timeout", "forbidden",
    "rate_limited", "server_error", "invalid_response", "not_applicable",
]


class MedGenError(RuntimeError):
    """Raised only for invalid MedGen caller inputs."""


class MedGenEnrichmentResult(TypedDict):
    variants: list[dict[str, Any]]
    status: MedGenStatus
    message: str
    request_attempts: int
    queried_gene_count: int


class MedGenPhenotypeGeneResult(TypedDict):
    """Analysis-level result for Stage 4 phenotype-gene MedGen enrichment."""

    variants: list[dict[str, Any]]
    status: MedGenStatus
    message: str
    request_attempts: int
    queried_tuple_count: int


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: object, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    result = " ".join(value.split())
    return result if result and len(result) <= limit else None


def _normalized_gene(value: object) -> str | None:
    gene = _text(value, limit=64)
    return gene.upper() if gene and MEDGEN_GENE_PATTERN.fullmatch(gene) else None


def _local_name(value: str) -> str:
    """Return an XML local name without trusting namespace prefixes."""
    return value.rsplit("}", 1)[-1]


def _normalized_hpo_terms(value: object) -> list[dict[str, str | None]]:
    """Keep only already-accepted canonical HPO identifiers and labels."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    terms: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            hpo_id, label = item, None
        elif isinstance(item, Mapping):
            hpo_id = item.get("hpo_id", item.get("id"))
            label = item.get("label", item.get("hpo_name"))
        else:
            continue
        if not isinstance(hpo_id, str) or MEDGEN_HPO_PATTERN.fullmatch(hpo_id) is None:
            continue
        if hpo_id in seen:
            continue
        normalized_label = _text(label, limit=200)
        terms.append({"hpo_id": hpo_id, "label": normalized_label})
        seen.add(hpo_id)
    return terms[:10]


def _summary_text(value: object, *, limit: int = 500) -> str | None:
    """Normalize scalar ESummary fields and its documented value wrapper."""
    if isinstance(value, Mapping):
        value = value.get("value")
    return _text(value, limit=limit)


def _conceptmeta(value: object) -> tuple[set[str] | None, list[dict[str, str | None]], dict[str, str | None]]:
    """Parse only deterministic ConceptMeta association/provenance fields.

    MedGen ESummary exposes `conceptmeta` as XML.  A missing or malformed
    value cannot establish a gene association and is therefore deliberately
    returned as unverified rather than guessed from titles or definitions.
    """
    raw = _text(value, limit=MEDGEN_CONCEPTMETA_MAX_BYTES)
    if raw is None:
        return None, [], {}
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        try:
            root = ElementTree.fromstring(f"<ConceptMeta>{raw}</ConceptMeta>")
        except ElementTree.ParseError:
            return None, [], {}

    associated_genes: set[str] = set()
    found_associated_genes = False
    metadata: dict[tuple[str, str | None, str | None, str | None], dict[str, str | None]] = {}
    hpo_terms: dict[str, str | None] = {}
    for element in root.iter():
        name = _local_name(element.tag)
        attributes = {
            _local_name(str(key)).casefold(): _text(item, limit=200)
            for key, item in element.attrib.items()
        }
        database = (
            attributes.get("sab")
            or attributes.get("source")
            or attributes.get("database")
        )
        code = attributes.get("code") or attributes.get("cui")
        scui = attributes.get("scui") or attributes.get("saui")
        if database:
            key = (database, code, scui, attributes.get("tty"))
            metadata[key] = {
                "database": database,
                "code": code,
                "scui": scui,
                "term_type": attributes.get("tty"),
            }

    for section in root.iter():
        section_name = _local_name(section.tag)
        if section_name == "AssociatedGenes":
            found_associated_genes = True
            for element in section.iter():
                attributes = {
                    _local_name(str(key)).casefold(): _text(item, limit=200)
                    for key, item in element.attrib.items()
                }
                for candidate in (
                    attributes.get("symbol"), attributes.get("genesymbol"),
                    attributes.get("gene"),
                ):
                    gene = _normalized_gene(candidate)
                    if gene:
                        associated_genes.add(gene)
                if _local_name(element.tag).casefold() in {"gene", "associatedgene"}:
                    gene = _normalized_gene(_text(element.text, limit=200))
                    if gene:
                        associated_genes.add(gene)
        if section_name not in {"ClinicalFeatures", "PhenotypicAbnormalities"}:
            continue
        for element in section.iter():
            attributes = {
                _local_name(str(key)).casefold(): _text(item, limit=200)
                for key, item in element.attrib.items()
            }
            text = _text(element.text, limit=200)
            hpo_id = next(
                (
                    candidate
                    for candidate in (
                        attributes.get("sdui"), attributes.get("scui"),
                        attributes.get("hpo_id"), attributes.get("hpoid"),
                        attributes.get("id"), attributes.get("code"), text,
                    )
                    if isinstance(candidate, str)
                    and MEDGEN_HPO_PATTERN.fullmatch(candidate) is not None
                ),
                None,
            )
            if hpo_id:
                label = (
                    attributes.get("name")
                    or attributes.get("label")
                    or (text if text != hpo_id else None)
                )
                hpo_terms.setdefault(hpo_id, _text(label, limit=200))

    return (
        associated_genes if found_associated_genes else None,
        sorted(metadata.values(), key=lambda item: (
            item["database"] or "", item["code"] or "", item["scui"] or ""
        ))[:10],
        {identifier: hpo_terms[identifier] for identifier in sorted(hpo_terms)[:10]},
    )


def _primary_state(value: object) -> tuple[str, bool, list[str], list[str]]:
    primary = value if isinstance(value, Mapping) else {}
    status = _text(primary.get("status")) or "not_applicable"
    records = primary.get("diseases")
    has_records = isinstance(records, list) and bool(records)
    if status == "available" and has_records:
        return status, False, ["primary_sufficient"], []
    reasons: list[str] = []
    if status in {"unavailable", "invalid_response"}:
        reasons.append("primary_operational_failure")
    elif status in {"no_association", "not_found"}:
        reasons.append("primary_no_match")
    elif status in {"partial", "available"}:
        reasons.append("primary_required_fields_missing")
    else:
        reasons.append("primary_non_applicable")
    if not has_records and "primary_required_fields_missing" not in reasons:
        reasons.append("primary_required_fields_missing")
    return status, True, reasons, ["diseases"]


def _request_json(
    session: requests.Session,
    path: str,
    *,
    params: Mapping[str, object],
    circuit: ProviderCircuitState,
) -> tuple[MedGenStatus, Mapping[str, object] | None, int, int | None]:
    def operation(timeout: tuple[float, float], _attempt: int) -> object:
        response = session.get(
            f"{settings.MEDGEN_BASE_URL}/{path}",
            params=params,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if not 200 <= response.status_code < 300:
            return response
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidResponseError(http_status=response.status_code) from exc
        if not isinstance(payload, Mapping):
            raise ProviderInvalidResponseError(http_status=response.status_code)
        return (response.status_code, payload)

    outcome = call_provider_with_policy(
        provider=MEDGEN_PROVIDER,
        operation_name=path.replace(".fcgi", "").replace("_", "-"),
        operation=operation,
        timeouts=ProviderTimeouts(
            connect=min(float(settings.MEDGEN_TIMEOUT), 10.0),
            read=float(settings.MEDGEN_TIMEOUT),
        ),
        retry_policy=ProviderRetryPolicy(
            max_attempts=settings.MEDGEN_MAX_RETRIES + 1,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
        ),
        circuit_state=circuit,
    )
    if isinstance(outcome.value, tuple):
        status_code, payload = outcome.value
        return outcome.status, payload, outcome.attempts, status_code
    return outcome.status, None, outcome.attempts, outcome.http_status


def _source_names(value: object) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    names = {_text(item, limit=100) for item in raw}
    return sorted(name for name in names if name is not None)[:10]


def _records(
    payload: Mapping[str, object],
    identifiers: list[str],
    *,
    query_gene: str,
    queried_hpo_terms: list[dict[str, str | None]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    queried_hpo_by_id = {
        term["hpo_id"]: term for term in queried_hpo_terms
    }
    for identifier in identifiers:
        item = result.get(identifier)
        if not isinstance(item, Mapping):
            return None
        uid = _text(item.get("uid"), limit=64) or identifier
        title = _text(item.get("title"))
        concept_id = _text(item.get("conceptid"), limit=128)
        if title is None:
            continue
        associated_genes, concept_metadata, concept_hpo = _conceptmeta(
            item.get("conceptmeta")
        )
        if associated_genes is None:
            association_state = "gene_association_unverified"
            rejection_reason = "conceptmeta_associated_genes_unavailable"
        elif query_gene not in associated_genes:
            association_state = "gene_mismatch"
            rejection_reason = "conceptmeta_associated_genes_no_exact_match"
        else:
            association_state = "exact_gene_association"
            rejection_reason = None
        sources = _source_names(item.get("sources") or item.get("source"))
        sources.extend(
            item["database"]
            for item in concept_metadata
            if item["database"] is not None
        )
        sources = sorted(set(sources))[:10]
        if association_state != "exact_gene_association":
            diagnostics.append(
                {
                    "medgen_uid": uid,
                    "concept_id": concept_id,
                    "title": title,
                    "gene_association_match_state": association_state,
                    "rejection_reason": rejection_reason,
                }
            )
            continue
        matched_hpo_terms = [
            {
                "hpo_id": hpo_id,
                "label": concept_hpo[hpo_id] or queried_hpo_by_id[hpo_id]["label"],
            }
            for hpo_id in queried_hpo_by_id
            if hpo_id in concept_hpo
        ]
        records.append(
            {
                "medgen_uid": uid,
                "concept_id": concept_id,
                "title": title,
                "semantic_type": _summary_text(
                    item.get("semantic_type") or item.get("semantictype"),
                    limit=200,
                ),
                "definition": _summary_text(item.get("definition"), limit=500),
                "gene_association_match_state": association_state,
                "gene_association_basis": "conceptmeta_associated_genes_exact_symbol",
                "matched_hpo_terms": matched_hpo_terms,
                "hpo_match_method": (
                    "exact_hpo_id_conceptmeta" if matched_hpo_terms else "not_available"
                ),
                "source_metadata": concept_metadata,
                "upstream_sources": sources,
            }
        )
        if len(records) == MEDGEN_MAX_RECORDS:
            break
    return records, diagnostics[:MEDGEN_MAX_RECORDS]


def _context(
    *,
    gene: str | None,
    status: MedGenStatus,
    retrieval_state: str,
    triggered: bool,
    reason_codes: list[str],
    primary_state: str,
    missing: list[str],
    attempts: int = 0,
    http_status: int | None = None,
    records: list[dict[str, Any]] | None = None,
    candidate_diagnostics: list[dict[str, Any]] | None = None,
    queried_hpo_terms: list[dict[str, str | None]] | None = None,
) -> dict[str, Any]:
    returned = records or []
    return {
        "schema_version": MEDGEN_CONTEXT_SCHEMA_VERSION,
        "provider": MEDGEN_PROVIDER_NAME,
        "provider_id": MEDGEN_PROVIDER,
        "provider_role": "fallback",
        "fallback_used": triggered,
        "primary_provider": "MyDisease.info",
        "primary_retrieval_state": primary_state,
        "status": status,
        "retrieval_state": retrieval_state,
        "retrieved_at": _timestamp(),
        "query_gene": gene,
        "query_key": f"gene:{gene}" if gene else None,
        "attempts": attempts,
        "http_status": http_status,
        "records": returned,
        "candidate_diagnostics": candidate_diagnostics or [],
        "queried_hpo_terms": queried_hpo_terms or [],
        "upstream_sources": sorted({
            source for record in returned for source in record["upstream_sources"]
        }),
        "enrichment_decision": {
            "triggered": triggered,
            "reason_codes": reason_codes,
            "target_semantic_node": "disease_hpo_context",
            "primary_retrieval_state": primary_state,
            "required_fields_missing": missing,
            "query_key": f"gene:{gene}" if gene else None,
        },
    }


def enrich_with_medgen(
    variants: Sequence[Mapping[str, object]],
    *,
    session: requests.Session | None = None,
    enabled: bool = True,
    accepted_hpo_terms: Sequence[object] = (),
) -> MedGenEnrichmentResult:
    """Attach one bounded MedGen disease/HPO context per unique eligible gene."""
    if not isinstance(variants, Sequence) or isinstance(variants, (str, bytes)):
        raise MedGenError("MedGen variants must be a sequence.")
    items = [dict(item) for item in variants]
    queried_hpo_terms = _normalized_hpo_terms(accepted_hpo_terms)
    queries: dict[str, tuple[str, str, list[str], list[str]]] = {}
    contexts: list[dict[str, Any] | None] = []
    for item in items:
        gene = _normalized_gene(item.get("gene"))
        primary_state, triggered, reasons, missing = _primary_state(item.get("mydisease"))
        if gene is None:
            contexts.append(_context(
                gene=None, status="not_applicable", retrieval_state="unattempted",
                triggered=False, reason_codes=["missing_gene"], primary_state=primary_state,
                missing=["gene"],
            ))
            continue
        if not enabled:
            contexts.append(_context(
                gene=gene, status="not_applicable", retrieval_state="unattempted",
                triggered=False, reason_codes=["provider_disabled"], primary_state=primary_state,
                missing=missing,
            ))
            continue
        if not triggered:
            contexts.append(_context(
                gene=gene, status="not_applicable", retrieval_state="not_needed",
                triggered=False, reason_codes=reasons, primary_state=primary_state, missing=missing,
            ))
            continue
        key = gene.casefold()
        queries.setdefault(key, (gene, primary_state, reasons, missing))
        contexts.append(None)

    context_by_gene: dict[str, dict[str, Any]] = {}
    attempts = 0
    client = session or requests.Session()
    owns_session = session is None
    try:
        circuit = ProviderCircuitState()
        for key, (gene, primary_state, reasons, missing) in queries.items():
            status, search, used, http_status = _request_json(
                client, "esearch.fcgi", params={
                    "db": "medgen", "term": f"{gene}[All Fields]", "retmode": "json",
                    "retmax": MEDGEN_MAX_RECORDS,
                }, circuit=circuit,
            )
            attempts += used
            if status != "success" or search is None:
                context_by_gene[key] = _context(
                    gene=gene, status=status, retrieval_state="operational_failure",
                    triggered=True, reason_codes=reasons, primary_state=primary_state,
                    missing=missing, attempts=used, http_status=http_status,
                )
                continue
            result = search.get("esearchresult")
            ids = result.get("idlist") if isinstance(result, Mapping) else None
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                context_by_gene[key] = _context(
                    gene=gene, status="invalid_response", retrieval_state="invalid_response",
                    triggered=True, reason_codes=reasons, primary_state=primary_state,
                    missing=missing, attempts=used, http_status=http_status,
                )
                continue
            identifiers = list(dict.fromkeys(ids))[:MEDGEN_MAX_RECORDS]
            if not identifiers:
                context_by_gene[key] = _context(
                    gene=gene, status="no_match", retrieval_state="no_match",
                    triggered=True, reason_codes=reasons, primary_state=primary_state,
                    missing=missing, attempts=used, http_status=http_status,
                )
                continue
            summary_status, summary, summary_used, summary_http = _request_json(
                client, "esummary.fcgi", params={
                    "db": "medgen", "id": ",".join(identifiers), "retmode": "json",
                }, circuit=circuit,
            )
            attempts += summary_used
            normalized = _records(
                summary, identifiers, query_gene=gene,
                queried_hpo_terms=queried_hpo_terms,
            ) if summary is not None else None
            if summary_status != "success" or normalized is None:
                context_by_gene[key] = _context(
                    gene=gene,
                    status=(summary_status if summary_status != "success" else "invalid_response"),
                    retrieval_state="operational_failure" if summary_status != "success" else "invalid_response",
                    triggered=True, reason_codes=reasons, primary_state=primary_state,
                    missing=missing, attempts=used + summary_used, http_status=summary_http,
                )
                continue
            accepted_records, diagnostics = normalized
            context_by_gene[key] = _context(
                gene=gene, status="success" if accepted_records else "no_match",
                retrieval_state=(
                    "accepted_records" if accepted_records
                    else "no_verified_gene_association"
                ),
                triggered=True, reason_codes=reasons, primary_state=primary_state,
                missing=missing, attempts=used + summary_used, http_status=summary_http,
                records=accepted_records,
                candidate_diagnostics=diagnostics,
                queried_hpo_terms=queried_hpo_terms,
            )
    finally:
        if owns_session:
            client.close()

    enriched: list[dict[str, Any]] = []
    for item, context in zip(items, contexts):
        gene = _normalized_gene(item.get("gene"))
        attached = context if context is not None else context_by_gene[gene.casefold()]  # type: ignore[union-attr]
        normalized_context = deepcopy(attached)
        normalized_context["queried_hpo_terms"] = deepcopy(queried_hpo_terms)
        enriched.append({**deepcopy(item), "medgen_disease_hpo_context": normalized_context})
    statuses = [item["medgen_disease_hpo_context"]["status"] for item in enriched]
    overall: MedGenStatus = "success" if "success" in statuses else (
        "no_match" if statuses and all(status in {"no_match", "not_applicable"} for status in statuses) else "not_applicable"
    )
    return {
        "variants": enriched,
        "status": overall,
        "message": f"MedGen disease/HPO context queried for {len(queries)} unique genes.",
        "request_attempts": attempts,
        "queried_gene_count": len(queries),
    }


# ---------------------------------------------------------------------------
# Stage 4 — MedGen phenotype-gene supporting evidence
# ---------------------------------------------------------------------------

MEDGEN_PHENOTYPE_GENE_SCHEMA_VERSION = "1.0"


def _is_local_support_sufficient(
    item: Mapping[str, object],
    phen2gene: Mapping[str, object],
) -> bool:
    """Check if semantically valid local phenotype-gene support is already sufficient.

    Preserves the distinction:
    - phenotype_gene_ranking is Phen2Gene only (never fabricated by local support)
    - phenotype_gene_support is populated by local support and/or MedGen

    Returns True when:
    1. phen2gene fallback was used and found a direct HPO-gene match.
    2. Explicit local_phenotype_support / local_support on the variant indicates a match.
    3. matched_patient_hpo_terms or local_phenotype_score on the variant is non-empty / positive.
    """
    if phen2gene.get("fallback_used") is True:
        matched = phen2gene.get("matched_hpos")
        if isinstance(matched, (list, tuple, set)) and len(matched) > 0:
            return True
        if phen2gene.get("status") in {"direct_match", "success"}:
            return True
        score = phen2gene.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score > 0:
            return True

    for key in ("local_phenotype_support", "local_support", "local_hpo_gene_support"):
        val = item.get(key)
        if isinstance(val, Mapping):
            if val.get("status") in {"success", "matched", "available", "direct_match"} or bool(val.get("matched_hpos")):
                return True
        elif isinstance(val, bool) and val is True:
            return True

    matched_hpos = item.get("matched_patient_hpo_terms")
    if isinstance(matched_hpos, (list, tuple, set)) and len(matched_hpos) > 0:
        return True

    local_score = item.get("local_phenotype_score") or item.get("phenotype_score")
    if isinstance(local_score, (int, float)) and not isinstance(local_score, bool) and local_score > 0:
        return True

    return False


def _phen2gene_state(
    phen2gene_value: object,
    item_value: object = None,
) -> tuple[str, bool, list[str], list[str]]:
    """Return (primary_state, triggered, reason_codes, missing) for phenotype_gene_support node.

    Explicitly distinguishes:
    - Phen2Gene usable / primary_sufficient (availability == 'available' and fallback_used is not True)
    - Phen2Gene operational failure (availability == 'unavailable' or primary_failure is not None)
    - Phen2Gene valid no_match (availability in {'no_match', 'not_found', 'no_association'} or status in {'not_found', 'no_match'})
    - Phen2Gene partial / insufficient (availability == 'partial')

    Target-node sufficiency:
    Before triggering MedGen, evaluates whether existing semantically valid local
    phenotype-gene support already sufficiently populates the phenotype_gene_support node.
    If local support is already sufficient -> NOT_NEEDED.
    """
    primary = phen2gene_value if isinstance(phen2gene_value, Mapping) else {}
    item = item_value if isinstance(item_value, Mapping) else {}

    availability_raw = _text(primary.get("availability")) or "not_applicable"
    status_raw = _text(primary.get("status")) or ""
    fallback_used = primary.get("fallback_used") is True

    # 1. Determine Phen2Gene primary state
    if availability_raw == "available" and not fallback_used:
        primary_state = "available"
        return primary_state, False, ["primary_sufficient"], []

    if (
        availability_raw in {"no_match", "not_found", "no_association"}
        or status_raw in {"not_found", "no_match", "no_association", "no_result"}
    ):
        primary_state = "no_match"
        gap_reason = "primary_no_match"
    elif availability_raw == "unavailable" or primary.get("primary_failure") is not None:
        primary_state = "unavailable"
        gap_reason = "primary_operational_failure"
    elif availability_raw == "partial":
        primary_state = "partial"
        gap_reason = "primary_partial"
    else:
        primary_state = "not_applicable"
        gap_reason = "primary_non_applicable"

    # 2. Target-node sufficiency: check if local phenotype-gene support is already sufficient
    if _is_local_support_sufficient(item, primary):
        return primary_state, False, [gap_reason, "local_support_sufficient"], []

    # 3. Support node is insufficient -> trigger MedGen
    return primary_state, True, [gap_reason, "phenotype_gene_support_gap"], ["phenotype_gene_support"]


def _phenotype_gene_context(
    *,
    gene: str | None,
    hpo_id: str,
    hpo_label: str | None,
    status: MedGenStatus,
    retrieval_state: str,
    triggered: bool,
    reason_codes: list[str],
    primary_state: str,
    missing: list[str],
    attempts: int = 0,
    http_status: int | None = None,
    records: list[dict[str, Any]] | None = None,
    candidate_diagnostics: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one source-separated Stage 4 phenotype-gene MedGen context.

    ``target_semantic_node`` is ``phenotype_gene_context`` to distinguish it
    from the Stage 3 ``disease_hpo_context`` node.  Records are disease
    conditions confirmed as associated with both the query gene and the query
    HPO term — they are supporting context, not a causal claim, and must never
    populate Phen2Gene rank/score/weight fields.
    """
    returned = records or []
    normalized_hpo_id = hpo_id if hpo_id and MEDGEN_HPO_PATTERN.fullmatch(hpo_id) else None
    query_key = f"gene:{gene}|hpo:{normalized_hpo_id}" if gene and normalized_hpo_id else (f"gene:{gene}" if gene else None)
    return {
        "schema_version": MEDGEN_PHENOTYPE_GENE_SCHEMA_VERSION,
        "provider": MEDGEN_PROVIDER_NAME,
        "provider_id": MEDGEN_PROVIDER,
        "provider_role": "supporting",
        "primary_provider": "phen2gene",
        "primary_retrieval_state": primary_state,
        "status": status,
        "retrieval_state": retrieval_state,
        "retrieved_at": _timestamp(),
        "query_gene": gene,
        "query_hpo_id": normalized_hpo_id,
        "query_hpo_label": hpo_label,
        "query_key": query_key,
        "attempts": attempts,
        "http_status": http_status,
        "records": returned,
        "candidate_diagnostics": candidate_diagnostics or [],
        "queried_hpo_terms": (
            [{"hpo_id": normalized_hpo_id, "label": hpo_label}]
            if normalized_hpo_id
            else []
        ),
        "upstream_sources": sorted({
            source
            for record in returned
            for source in record["upstream_sources"]
        }),
        "enrichment_decision": {
            "triggered": triggered,
            "reason_codes": reason_codes,
            "target_semantic_node": "phenotype_gene_context",
            "primary_retrieval_state": primary_state,
            "required_fields_missing": missing,
            "query_key": query_key,
        },
    }


def enrich_with_medgen_phenotype_gene(
    variants: Sequence[Mapping[str, object]],
    *,
    session: requests.Session | None = None,
    enabled: bool = True,
    accepted_hpo_terms: Sequence[object] = (),
) -> MedGenPhenotypeGeneResult:
    """Attach MedGen phenotype-gene supporting context per (gene, HPO) tuple.

    Queries MedGen with ``GENE[All Fields] AND "HPO label"[Clinical Features]``
    for each unique (normalized-gene, hpo_id) pair when the Phen2Gene
    phenotype-gene node is insufficient.  Results are source-separated as
    ``medgen_phenotype_gene_context`` and must never populate Phen2Gene
    rank, score, or weight fields.

    Parameters
    ----------
    variants:
        Per-variant dicts that already carry a ``phen2gene`` key with
        the primary phenotype-gene evidence.
    session:
        Optional shared ``requests.Session``; a new one is created when
        ``None`` and closed on exit.
    enabled:
        Master kill-switch; when ``False`` every variant gets
        ``not_applicable`` context without any HTTP calls.
    accepted_hpo_terms:
        Canonical HPO terms (``{"hpo_id": "HP:...", "label": ...}``)
        already accepted for this analysis.  At least one term with a
        non-empty label is required to build a valid search query.
    """
    if not isinstance(variants, Sequence) or isinstance(variants, (str, bytes)):
        raise MedGenError("MedGen variants must be a sequence.")
    items = [dict(item) for item in variants]
    queried_hpo_terms = _normalized_hpo_terms(accepted_hpo_terms)
    # Only HPO terms that have a label can be used in an ESearch phrase query.
    hpo_terms_with_labels = [
        term for term in queried_hpo_terms if term["label"]
    ]

    # -----------------------------------------------------------------------
    # Decide, per variant, whether enrichment should be triggered.
    # Collect unique (gene_key, hpo_id) query tuples for deduplication.
    # -----------------------------------------------------------------------
    # contexts[i] is None when the variant must be looked up by query tuple.
    contexts: list[dict[str, Any] | None] = []
    # Map (gene_key, hpo_id) -> (gene, hpo_id, hpo_label, primary_state, reasons, missing)
    queries: dict[
        tuple[str, str],
        tuple[str, str, str | None, str, list[str], list[str]],
    ] = {}
    # Map variant index -> list of (gene_key, hpo_id) tuples it requires
    variant_query_tuples: list[list[tuple[str, str]]] = []

    for item in items:
        gene = _normalized_gene(item.get("gene"))
        primary_state, triggered, reasons, missing = _phen2gene_state(
            item.get("phen2gene"),
            item,
        )

        if gene is None:
            contexts.append(_phenotype_gene_context(
                gene=None, hpo_id="", hpo_label=None,
                status="not_applicable", retrieval_state="unattempted",
                triggered=False, reason_codes=["missing_gene"],
                primary_state=primary_state, missing=["gene"],
            ))
            variant_query_tuples.append([])
            continue

        if not enabled:
            contexts.append(_phenotype_gene_context(
                gene=gene, hpo_id="", hpo_label=None,
                status="not_applicable", retrieval_state="unattempted",
                triggered=False, reason_codes=["provider_disabled"],
                primary_state=primary_state, missing=missing,
            ))
            variant_query_tuples.append([])
            continue

        if not triggered:
            contexts.append(_phenotype_gene_context(
                gene=gene, hpo_id="", hpo_label=None,
                status="not_applicable", retrieval_state="not_needed",
                triggered=False, reason_codes=reasons,
                primary_state=primary_state, missing=missing,
            ))
            variant_query_tuples.append([])
            continue

        if not hpo_terms_with_labels:
            # Cannot build a valid phenotype ESearch term without a label.
            contexts.append(_phenotype_gene_context(
                gene=gene, hpo_id="", hpo_label=None,
                status="not_applicable", retrieval_state="prerequisite_not_met",
                triggered=False, reason_codes=["no_accepted_hpo_with_label"],
                primary_state=primary_state, missing=missing,
            ))
            variant_query_tuples.append([])
            continue

        gene_key = gene.casefold()
        tuples_for_variant: list[tuple[str, str]] = []
        for term in hpo_terms_with_labels:
            hpo_id = str(term["hpo_id"])
            hpo_label = term["label"]
            tuple_key = (gene_key, hpo_id)
            queries.setdefault(
                tuple_key,
                (gene, hpo_id, hpo_label, primary_state, reasons, missing),
            )
            tuples_for_variant.append(tuple_key)

        contexts.append(None)  # will be filled from context_by_tuple
        variant_query_tuples.append(tuples_for_variant)

    # -----------------------------------------------------------------------
    # Execute deduplicated (gene, HPO) queries.
    # -----------------------------------------------------------------------
    context_by_tuple: dict[tuple[str, str], dict[str, Any]] = {}
    attempts = 0
    client = session or requests.Session()
    owns_session = session is None
    try:
        circuit = ProviderCircuitState()
        for tuple_key, (
            gene, hpo_id, hpo_label, primary_state, reasons, missing
        ) in queries.items():
            search_term = f'{gene}[All Fields] AND "{hpo_label}"[Clinical Features]'
            status, search, used, http_status = _request_json(
                client, "esearch.fcgi", params={
                    "db": "medgen",
                    "term": search_term,
                    "retmode": "json",
                    "retmax": MEDGEN_MAX_RECORDS,
                },
                circuit=circuit,
            )
            attempts += used
            if status != "success" or search is None:
                context_by_tuple[tuple_key] = _phenotype_gene_context(
                    gene=gene, hpo_id=hpo_id, hpo_label=hpo_label,
                    status=status, retrieval_state="operational_failure",
                    triggered=True, reason_codes=reasons,
                    primary_state=primary_state, missing=missing,
                    attempts=used, http_status=http_status,
                )
                continue

            esearch_result = search.get("esearchresult")
            ids = (
                esearch_result.get("idlist")
                if isinstance(esearch_result, Mapping)
                else None
            )
            if not isinstance(ids, list) or any(
                not isinstance(v, str) for v in ids
            ):
                context_by_tuple[tuple_key] = _phenotype_gene_context(
                    gene=gene, hpo_id=hpo_id, hpo_label=hpo_label,
                    status="invalid_response",
                    retrieval_state="invalid_response",
                    triggered=True, reason_codes=reasons,
                    primary_state=primary_state, missing=missing,
                    attempts=used, http_status=http_status,
                )
                continue

            identifiers = list(dict.fromkeys(ids))[:MEDGEN_MAX_RECORDS]
            if not identifiers:
                context_by_tuple[tuple_key] = _phenotype_gene_context(
                    gene=gene, hpo_id=hpo_id, hpo_label=hpo_label,
                    status="no_match", retrieval_state="no_match",
                    triggered=True, reason_codes=reasons,
                    primary_state=primary_state, missing=missing,
                    attempts=used, http_status=http_status,
                )
                continue

            # Single-HPO term list for _records() HPO matching.
            hpo_term_list = [{"hpo_id": hpo_id, "label": hpo_label}]
            summary_status, summary, summary_used, summary_http = _request_json(
                client, "esummary.fcgi", params={
                    "db": "medgen",
                    "id": ",".join(identifiers),
                    "retmode": "json",
                },
                circuit=circuit,
            )
            attempts += summary_used
            normalized = (
                _records(
                    summary,
                    identifiers,
                    query_gene=gene,
                    queried_hpo_terms=hpo_term_list,
                )
                if summary is not None
                else None
            )
            if summary_status != "success" or normalized is None:
                context_by_tuple[tuple_key] = _phenotype_gene_context(
                    gene=gene, hpo_id=hpo_id, hpo_label=hpo_label,
                    status=(
                        summary_status
                        if summary_status != "success"
                        else "invalid_response"
                    ),
                    retrieval_state=(
                        "operational_failure"
                        if summary_status != "success"
                        else "invalid_response"
                    ),
                    triggered=True, reason_codes=reasons,
                    primary_state=primary_state, missing=missing,
                    attempts=used + summary_used, http_status=summary_http,
                )
                continue

            accepted_records, diagnostics = normalized
            context_by_tuple[tuple_key] = _phenotype_gene_context(
                gene=gene, hpo_id=hpo_id, hpo_label=hpo_label,
                status="success" if accepted_records else "no_match",
                retrieval_state=(
                    "accepted_records" if accepted_records
                    else "no_verified_gene_association"
                ),
                triggered=True, reason_codes=reasons,
                primary_state=primary_state, missing=missing,
                attempts=used + summary_used, http_status=summary_http,
                records=accepted_records,
                candidate_diagnostics=diagnostics,
            )
    finally:
        if owns_session:
            client.close()

    # -----------------------------------------------------------------------
    # Fan immutable results back to input-ordered variants.
    # Each variant gets a list of per-HPO context records.
    # When a variant has multiple HPO tuples, all are attached.
    # When a variant was skipped (no tuples), it keeps its single context in a list.
    # -----------------------------------------------------------------------
    enriched: list[dict[str, Any]] = []
    for item, context, tuples in zip(items, contexts, variant_query_tuples):
        if context is not None:
            # Variant was resolved to a single non-null context (skipped case).
            enriched.append({
                **deepcopy(item),
                "medgen_phenotype_gene_context": [deepcopy(context)],
            })
        else:
            # Variant needs per-HPO-tuple contexts.
            per_hpo_contexts = [
                deepcopy(context_by_tuple[t]) for t in tuples
            ]
            enriched.append({
                **deepcopy(item),
                "medgen_phenotype_gene_context": per_hpo_contexts,
            })

    statuses = [
        next(
            (c["status"] for c in ctx if c.get("status") == "success"),
            ctx[0]["status"] if ctx else "not_applicable",
        )
        for ctx in [
            item["medgen_phenotype_gene_context"] for item in enriched
        ]
    ]
    overall: MedGenStatus = "success" if "success" in statuses else (
        "no_match"
        if statuses and all(
            s in {"no_match", "not_applicable"} for s in statuses
        )
        else "not_applicable"
    )
    return {
        "variants": enriched,
        "status": overall,
        "message": (
            f"MedGen phenotype-gene context queried for "
            f"{len(queries)} unique (gene, HPO) tuples."
        ),
        "request_attempts": attempts,
        "queried_tuple_count": len(queries),
    }


__all__ = [
    "MEDGEN_CONTEXT_SCHEMA_VERSION",
    "MEDGEN_PHENOTYPE_GENE_SCHEMA_VERSION",
    "MEDGEN_PROVIDER",
    "MedGenEnrichmentResult",
    "MedGenError",
    "MedGenPhenotypeGeneResult",
    "enrich_with_medgen",
    "enrich_with_medgen_phenotype_gene",
]
