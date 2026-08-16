"""Bounded, source-separated NCBI MedGen disease/HPO context retrieval."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
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


MEDGEN_PROVIDER = "ncbi_medgen"
MEDGEN_PROVIDER_NAME = "NCBI MedGen"
MEDGEN_CONTEXT_SCHEMA_VERSION = "1.0"
MEDGEN_MAX_RECORDS = 10
MEDGEN_GENE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

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


def _records(payload: Mapping[str, object], identifiers: list[str]) -> list[dict[str, Any]] | None:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return None
    records: list[dict[str, Any]] = []
    for identifier in identifiers:
        item = result.get(identifier)
        if not isinstance(item, Mapping):
            return None
        uid = _text(item.get("uid"), limit=64) or identifier
        title = _text(item.get("title"))
        concept_id = _text(item.get("conceptid"), limit=128)
        if title is None:
            continue
        sources = _source_names(item.get("sources") or item.get("source"))
        records.append(
            {
                "medgen_uid": uid,
                "concept_id": concept_id,
                "title": title,
                "semantic_type": _text(item.get("semantic_type"), limit=200),
                "definition": _text(item.get("definition"), limit=500),
                "source_metadata": sources,
                "upstream_sources": sources,
            }
        )
        if len(records) == MEDGEN_MAX_RECORDS:
            break
    return records


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
) -> MedGenEnrichmentResult:
    """Attach one bounded MedGen disease/HPO context per unique eligible gene."""
    if not isinstance(variants, Sequence) or isinstance(variants, (str, bytes)):
        raise MedGenError("MedGen variants must be a sequence.")
    items = [dict(item) for item in variants]
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
            normalized = _records(summary, identifiers) if summary is not None else None
            if summary_status != "success" or normalized is None:
                context_by_gene[key] = _context(
                    gene=gene,
                    status=(summary_status if summary_status != "success" else "invalid_response"),
                    retrieval_state="operational_failure" if summary_status != "success" else "invalid_response",
                    triggered=True, reason_codes=reasons, primary_state=primary_state,
                    missing=missing, attempts=used + summary_used, http_status=summary_http,
                )
                continue
            context_by_gene[key] = _context(
                gene=gene, status="success" if normalized else "no_match",
                retrieval_state="accepted_records" if normalized else "no_match",
                triggered=True, reason_codes=reasons, primary_state=primary_state,
                missing=missing, attempts=used + summary_used, http_status=summary_http,
                records=normalized,
            )
    finally:
        if owns_session:
            client.close()

    enriched: list[dict[str, Any]] = []
    for item, context in zip(items, contexts):
        gene = _normalized_gene(item.get("gene"))
        attached = context if context is not None else context_by_gene[gene.casefold()]  # type: ignore[union-attr]
        enriched.append({**deepcopy(item), "medgen_disease_hpo_context": deepcopy(attached)})
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


__all__ = ["MEDGEN_CONTEXT_SCHEMA_VERSION", "MEDGEN_PROVIDER", "MedGenEnrichmentResult", "MedGenError", "enrich_with_medgen"]
