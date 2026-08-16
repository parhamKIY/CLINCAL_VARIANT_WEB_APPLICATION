"""Bounded exact-identity ClinGen ERepo retrieval.

This module retains compact, source-attributed expert-curated context only.
It never converts ERepo records into ACMG criteria or an autonomous result.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, NamedTuple

import requests

from backend.provider_resilience import (
    ProviderCircuitState,
    ProviderInvalidResponseError,
    ProviderRetryPolicy,
    ProviderTimeouts,
    build_capability_result,
    call_provider_with_policy,
)
from backend.retrieval_intelligence import VariantIdentifierBundle
from config import settings


EREPO_PROVIDER = "clingen_erepo"
EREPO_PROVIDER_NAME = "ClinGen ERepo"
EREPO_CONTEXT_SCHEMA_VERSION = "1.0"
SUMMARY_PATH = "/evrepo/api/summary/classifications"
DETAIL_PATH = "/evrepo/api/summary/classification/{uuid}/doc/sepio/version/{version}"
MAX_STRATEGIES = 7
MAX_CANDIDATES_PER_STRATEGY = 10
MAX_REJECTIONS_PER_STRATEGY = 20
MAX_ACCEPTED_RECORDS = 10
_STRUCTURED_NO_MATCH = {
    "code": 404,
    "name": "Not Found",
    "msg": "No records were found for given query",
}


class _DecodedResponse(NamedTuple):
    payload: dict[str, Any]
    http_status: int


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: object, *, limit: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def _strings(value: object, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text is not None and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _provenance_for(
    bundle: VariantIdentifierBundle,
    value: str,
    *,
    scope: str,
    validations: set[str],
) -> list[dict[str, str]]:
    return [
        {
            "source": source,
            "scope": record_scope,
            "validation": validation,
        }
        for record in bundle.get("provenance", [])
        if isinstance(record, Mapping)
        and _text(record.get("value")) == value
        and (record_scope := _text(record.get("scope"))) == scope
        and (validation := _text(record.get("validation"))) in validations
        and (source := _text(record.get("source"))) is not None
    ]


def _eligible_strategies(
    bundle: VariantIdentifierBundle,
) -> tuple[str | None, bool, list[dict[str, Any]]]:
    """Return bounded approved discovery strategies and expected allele HGVS."""

    selected_genomic: str | None = None
    selected_provenance: list[dict[str, str]] = []
    for value in bundle.get("genomic_hgvs", []):
        text = _text(value)
        if text is None or not text.startswith("NC_") or ":g." not in text:
            continue
        provenance = _provenance_for(
            bundle,
            text,
            scope="allele",
            validations={"deterministic_normalization", "provider_exact_allele"},
        )
        if provenance:
            selected_genomic, selected_provenance = text, provenance
            break
    if selected_genomic is None:
        return None, False, []

    strategies: list[dict[str, Any]] = [
        {
            "strategy_id": "exact_genomic_hgvs",
            "query_column": "hgvs",
            "query_identifier": selected_genomic,
            "identifier_provenance": selected_provenance,
        }
    ]
    used = {selected_genomic.casefold()}
    for field, scope, validations, strategy_id, column, limit in (
        (
            "clinvar_variation_ids",
            "allele",
            {"provider_exact_allele"},
            "exact_clinvar_variation_id",
            "cvId",
            3,
        ),
        (
            "transcript_hgvs",
            "transcript",
            {"provider_transcript_annotation"},
            "exact_transcript_hgvs",
            "hgvs",
            3,
        ),
    ):
        count = 0
        for value in bundle.get(field, []):
            text = _text(value)
            if text is None or text.casefold() in used:
                continue
            if strategy_id == "exact_transcript_hgvs" and not (
                text.startswith(("NM_", "NR_", "ENST"))
                and "." in text.split(":", 1)[0]
            ):
                continue
            provenance = _provenance_for(
                bundle, text, scope=scope, validations=validations
            )
            if not provenance:
                continue
            strategies.append(
                {
                    "strategy_id": strategy_id,
                    "query_column": column,
                    "query_identifier": text,
                    "identifier_provenance": provenance,
                }
            )
            used.add(text.casefold())
            count += 1
            if count == limit:
                break
    equivalent_representation_allowed = (
        bundle.get("minimal_representation_status") == "applied"
        and any(
            record["validation"] == "deterministic_normalization"
            for record in selected_provenance
        )
    )
    return (
        selected_genomic,
        equivalent_representation_allowed,
        strategies[:MAX_STRATEGIES],
    )


def _request_json(
    *,
    session: requests.Session,
    url: str,
    params: Mapping[str, object] | None,
    circuit_state: ProviderCircuitState | None,
    operation_name: str,
    timeout: int,
    max_retries: int,
) -> tuple[str, dict[str, Any] | None, int, int | None]:
    """Call one endpoint through the shared bounded provider policy."""

    def operation(request_timeout: tuple[float, float], _attempt: int) -> object:
        response = session.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=request_timeout,
        )
        if response.status_code == 404:
            return response
        if 200 <= response.status_code < 300:
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise ProviderInvalidResponseError(
                    "ERepo returned malformed JSON.",
                    http_status=response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise ProviderInvalidResponseError(
                    "ERepo response must be an object.",
                    http_status=response.status_code,
                )
            return _DecodedResponse(payload, response.status_code)
        return response

    outcome = call_provider_with_policy(
        provider=EREPO_PROVIDER,
        operation_name=operation_name,
        operation=operation,
        timeouts=ProviderTimeouts(connect=min(timeout, 10), read=timeout),
        retry_policy=ProviderRetryPolicy(
            max_attempts=max_retries + 1,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
        ),
        circuit_state=circuit_state,
        no_match_http_statuses=frozenset({404}),
    )
    value = outcome.value
    if isinstance(value, _DecodedResponse):
        return outcome.status, value.payload, outcome.attempts, value.http_status
    if outcome.status == "no_match" and hasattr(value, "json"):
        try:
            payload = value.json()
        except (TypeError, ValueError):
            return "invalid_response", None, outcome.attempts, 404
        if not isinstance(payload, dict):
            return "invalid_response", None, outcome.attempts, 404
        return outcome.status, payload, outcome.attempts, 404
    return outcome.status, None, outcome.attempts, outcome.http_status


def _valid_envelope(
    payload: object,
    *,
    data_kind: type[list[Any]] | type[dict[str, Any]],
) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("metadata"), dict):
        return False
    status = payload.get("status")
    return (
        isinstance(status, dict)
        and status.get("code") == 200
        and status.get("name") == "OK"
        and isinstance(payload.get("data"), data_kind)
    )


def _is_structured_no_match(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("status"), dict)
        and all(payload["status"].get(key) == value for key, value in _STRUCTURED_NO_MATCH.items())
    )


def _candidate_rejection(
    candidate: object,
    expected_hgvs: str,
    *,
    equivalent_representation_allowed: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(candidate, dict):
        return "malformed_field_type", None
    uuid = _text(candidate.get("uuid"), limit=128)
    ca_id = _text(candidate.get("caId"), limit=128)
    doc_version = _text(candidate.get("docVersion"), limit=128)
    hgvs = candidate.get("hgvs")
    if not uuid or not ca_id or not doc_version:
        return "missing_identity_field", None
    if not isinstance(candidate.get("retracted"), bool) or not isinstance(hgvs, list):
        return "malformed_field_type", None
    candidate_hgvs = _strings(hgvs, limit=30)
    if len(candidate_hgvs) != len(hgvs):
        return "malformed_field_type", None
    if candidate["retracted"]:
        return "retracted_record", None
    normalized_expected = expected_hgvs.strip().upper()
    if normalized_expected not in {item.strip().upper() for item in candidate_hgvs}:
        expected_accession = expected_hgvs.split(":", 1)[0].split(".", 1)[0]
        if any(
            item.split(":", 1)[0].split(".", 1)[0] == expected_accession
            for item in candidate_hgvs
            if item.startswith("NC_")
        ):
            return "assembly_mismatch", None
        return "hgvs_mismatch", None
    acceptance_state = (
        "EXACT_MATCH_EQUIVALENT_REPRESENTATION"
        if equivalent_representation_allowed
        and any(token in expected_hgvs for token in ("del", "ins"))
        else "EXACT_MATCH"
    )
    return None, {
        "uuid": uuid,
        "ca_id": ca_id,
        "doc_version": doc_version,
        "acceptance_state": acceptance_state,
        "classification": _text(candidate.get("classification")),
        "condition": _text(candidate.get("condition")),
        "mondo_id": _text(candidate.get("mondoId"), limit=128),
        "mode_of_inheritance": _text(candidate.get("moi")),
        "expert_panel": _text(candidate.get("ep")),
        "approved_date": _text(candidate.get("approvedDate"), limit=64),
        "published_date": _text(candidate.get("publishedDate"), limit=64),
        "met_codes": _strings(candidate.get("metCodes")),
        "unmet_codes": _strings(candidate.get("unMetCodes")),
        "clinvar_variation_id": _text(candidate.get("cvId"), limit=128),
        "preferred_variant_title": _text(candidate.get("preferredVarTitle")),
        "summary_description": _text(candidate.get("summaryDesc")),
    }


def _detail_record(
    summary: dict[str, Any],
    payload: object,
) -> tuple[str | None, dict[str, Any] | None]:
    if not _valid_envelope(payload, data_kind=dict):
        return "malformed_detail_document", None
    data = payload["data"]
    assert isinstance(data, dict)
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return "malformed_detail_document", None
    if _text(data.get("uuid"), limit=128) != summary["uuid"]:
        return "detail_uuid_mismatch", None
    version = _text(metadata.get("version"), limit=128)
    if version != summary["doc_version"]:
        return "detail_version_mismatch", None
    identifier = _text(data.get("@id"), limit=2048)
    if identifier is None or not identifier.endswith(f"/doc/sepio/version/{version}"):
        return "detail_version_mismatch", None
    for field in ("variant", "condition", "statementOutcome", "assertionMethod"):
        if not isinstance(data.get(field), dict) or not data[field]:
            return "malformed_detail_document", None
    variant = data["variant"]
    if not _text(variant.get("@id"), limit=2048) and not _text(
        variant.get("id"), limit=500
    ):
        return "malformed_detail_document", None
    statement = data["statementOutcome"]
    method = data["assertionMethod"]
    return None, {
        **summary,
        "statement_outcome": _text(statement.get("label")) or summary["classification"],
        "assertion_method": _text(method.get("label")),
    }


def _context(
    *,
    status: str,
    retrieval_state: str,
    records: list[dict[str, Any]],
    strategy_results: list[dict[str, Any]],
) -> dict[str, Any]:
    capability_status = status if status in {
        "success", "no_match", "unavailable", "timeout", "forbidden",
        "rate_limited", "server_error", "invalid_response", "configuration_error",
        "not_applicable",
    } else "unavailable"
    return {
        "status": status,
        "provider": EREPO_PROVIDER_NAME,
        "provider_id": EREPO_PROVIDER,
        "retrieved_at": _timestamp(),
        "expert_curated_variant_context": {
            "schema_version": EREPO_CONTEXT_SCHEMA_VERSION,
            "provider": EREPO_PROVIDER,
            "status": status,
            "retrieval_state": retrieval_state,
            "context_available": bool(records),
            "records": records[:MAX_ACCEPTED_RECORDS],
            "strategy_results": strategy_results[:MAX_STRATEGIES],
            "capability_result": build_capability_result(
                capability="expert_curated_variant_context",
                status=capability_status,
                provider=EREPO_PROVIDER,
                method="exact_erepo_identity_lookup",
                data={
                    "context_available": bool(records),
                    "record_count": len(records),
                    "retrieval_state": retrieval_state,
                },
                provenance={"strategy_count": len(strategy_results)},
            ),
        },
    }


def retrieve_expert_curated_context(
    bundle: VariantIdentifierBundle,
    *,
    session: requests.Session,
    circuit_state: ProviderCircuitState | None = None,
    enabled: bool = True,
    base_url: str | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """Retrieve only detail-verified, exact ERepo variant context."""

    if not enabled:
        return _context(
            status="not_applicable", retrieval_state="unattempted", records=[], strategy_results=[]
        )
    expected_hgvs, equivalent_representation_allowed, strategies = _eligible_strategies(bundle)
    if expected_hgvs is None:
        return _context(
            status="not_applicable", retrieval_state="unattempted", records=[], strategy_results=[]
        )

    base = (base_url or settings.EREPO_BASE_URL).rstrip("/")
    resolved_timeout = timeout if timeout is not None else settings.EREPO_TIMEOUT
    resolved_retries = max_retries if max_retries is not None else settings.EREPO_MAX_RETRIES
    strategy_results: list[dict[str, Any]] = []
    accepted_records: list[dict[str, Any]] = []
    operational_status: str | None = None
    identity_rejected = False

    for strategy in strategies:
        query_result = {
            **strategy,
            "endpoint": SUMMARY_PATH,
            "retrieved_at": _timestamp(),
            "candidate_count": 0,
            "candidate_rejections": [],
            "accepted_uuids": [],
        }
        status, payload, attempts, http_status = _request_json(
            session=session,
            url=f"{base}{SUMMARY_PATH}",
            params={
                "columns": strategy["query_column"],
                "values": strategy["query_identifier"],
                "matchTypes": "exact",
                "matchMode": "and",
                "pg": 1,
                "pgSize": MAX_CANDIDATES_PER_STRATEGY,
            },
            circuit_state=circuit_state,
            operation_name="summary_lookup",
            timeout=resolved_timeout,
            max_retries=resolved_retries,
        )
        query_result.update({"status": status, "attempts": attempts, "http_status": http_status})
        if status == "no_match":
            if _is_structured_no_match(payload):
                strategy_results.append(query_result)
                continue
            query_result["status"] = "invalid_response"
            strategy_results.append(query_result)
            operational_status = operational_status or "invalid_response"
            continue
        if status != "success" or not _valid_envelope(payload, data_kind=list):
            query_result["status"] = "invalid_response" if status == "success" else status
            strategy_results.append(query_result)
            operational_status = operational_status or query_result["status"]
            continue
        candidates = payload["data"]
        assert isinstance(candidates, list)
        if not candidates:
            query_result["status"] = "invalid_response"
            strategy_results.append(query_result)
            operational_status = operational_status or "invalid_response"
            continue
        query_result["candidate_count"] = min(len(candidates), MAX_CANDIDATES_PER_STRATEGY)
        for candidate in candidates[:MAX_CANDIDATES_PER_STRATEGY]:
            rejection, summary = _candidate_rejection(
                candidate,
                expected_hgvs,
                equivalent_representation_allowed=equivalent_representation_allowed,
            )
            if rejection is not None or summary is None:
                query_result["candidate_rejections"].append(rejection or "malformed_field_type")
                identity_rejected = True
                continue
            detail_status, detail_payload, detail_attempts, detail_http = _request_json(
                session=session,
                url=(
                    f"{base}{DETAIL_PATH.format(uuid=summary['uuid'], version=summary['doc_version'])}"
                ),
                params=None,
                circuit_state=circuit_state,
                operation_name="detail_lookup",
                timeout=resolved_timeout,
                max_retries=resolved_retries,
            )
            if detail_status != "success":
                query_result["candidate_rejections"].append(
                    "malformed_detail_document" if detail_status == "no_match" else detail_status
                )
                operational_status = operational_status or (
                    "invalid_response"
                    if detail_status == "no_match"
                    else detail_status
                )
                continue
            rejection, record = _detail_record(summary, detail_payload)
            if rejection is not None or record is None:
                query_result["candidate_rejections"].append(rejection or "malformed_detail_document")
                identity_rejected = True
                continue
            record["query_strategy"] = strategy["strategy_id"]
            record["query_identifier"] = strategy["query_identifier"]
            record["detail_attempts"] = detail_attempts
            record["detail_http_status"] = detail_http
            accepted_records.append(record)
            query_result["accepted_uuids"].append(record["uuid"])
        query_result["candidate_rejections"] = query_result["candidate_rejections"][:MAX_REJECTIONS_PER_STRATEGY]
        strategy_results.append(query_result)
        if accepted_records:
            break

    if accepted_records:
        return _context(status="success", retrieval_state="accepted_records", records=accepted_records, strategy_results=strategy_results)
    if operational_status is not None:
        return _context(status=operational_status, retrieval_state="operational_failure", records=[], strategy_results=strategy_results)
    if strategy_results and all(item["status"] == "no_match" for item in strategy_results):
        return _context(status="no_match", retrieval_state="no_match", records=[], strategy_results=strategy_results)
    return _context(
        status="unavailable",
        retrieval_state="identity_mismatch" if identity_rejected else "operational_failure",
        records=[],
        strategy_results=strategy_results,
    )


__all__ = [
    "EREPO_CONTEXT_SCHEMA_VERSION",
    "EREPO_PROVIDER",
    "EREPO_PROVIDER_NAME",
    "retrieve_expert_curated_context",
]
