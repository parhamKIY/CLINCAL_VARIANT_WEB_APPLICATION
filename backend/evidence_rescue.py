"""Bounded provenance for evidence rescue after valid sparse outcomes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Literal, TypedDict, cast


EVIDENCE_RESCUE_SCHEMA_VERSION = "1.0"
MAX_RESCUE_ATTEMPTS = 3
MAX_ALTERNATE_IDENTIFIERS = 5
MAX_RESCUE_TEXT_CHARS = 500
RESCUE_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")

RescueTrigger = Literal[
    "primary_no_match",
    "sparse_evidence",
    "missing_identifier",
    "readiness_deficit",
]
RescueStatus = Literal[
    "success",
    "no_match",
    "unavailable",
    "invalid_response",
    "not_applicable",
]
RescueStopReason = Literal[
    "evidence_recovered",
    "no_secondary_evidence",
    "secondary_unavailable",
    "missing_identifier",
    "attempt_limit_reached",
]

RESCUE_TRIGGERS = frozenset(
    {
        "primary_no_match",
        "sparse_evidence",
        "missing_identifier",
        "readiness_deficit",
    }
)
RESCUE_STATUSES = frozenset(
    {
        "success",
        "no_match",
        "unavailable",
        "invalid_response",
        "not_applicable",
    }
)
RESCUE_STOP_REASONS = frozenset(
    {
        "evidence_recovered",
        "no_secondary_evidence",
        "secondary_unavailable",
        "missing_identifier",
        "attempt_limit_reached",
    }
)


class EvidenceRescueIdentifier(TypedDict):
    """One bounded alternate identifier considered by a rescue route."""

    type: str
    value: str


class EvidenceRescueAttempt(TypedDict):
    """One capability-specific secondary evidence route."""

    sequence: int
    provider: str
    method: str
    identifier_type: str | None
    identifier_value: str | None
    status: RescueStatus
    evidence_recovered: bool
    evidence_path: str | None
    independent_evidence: bool


class EvidenceRescueTrace(TypedDict):
    """Machine-readable rescue eligibility, attempt, and stop record."""

    schema_version: str
    capability: str
    trigger: RescueTrigger
    primary_provider: str
    primary_status: str
    eligible: bool
    attempted: bool
    alternate_identifiers: list[EvidenceRescueIdentifier]
    attempts: list[EvidenceRescueAttempt]
    recovered: bool
    recovered_provider: str | None
    stop_reason: RescueStopReason


IDENTIFIER_FIELDS = frozenset({"type", "value"})
ATTEMPT_FIELDS = frozenset(
    {
        "sequence",
        "provider",
        "method",
        "identifier_type",
        "identifier_value",
        "status",
        "evidence_recovered",
        "evidence_path",
        "independent_evidence",
    }
)
TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "capability",
        "trigger",
        "primary_provider",
        "primary_status",
        "eligible",
        "attempted",
        "alternate_identifiers",
        "attempts",
        "recovered",
        "recovered_provider",
        "stop_reason",
    }
)


class EvidenceRescueContractError(ValueError):
    """Raised when rescue trace provenance violates the contract."""


def _identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not RESCUE_IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise EvidenceRescueContractError(
            f"{field} must be a lowercase identifier."
        )
    return value


def _text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_RESCUE_TEXT_CHARS
    ):
        raise EvidenceRescueContractError(
            f"{field} must be a bounded non-empty string."
        )
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def validate_evidence_rescue_trace(
    value: object,
) -> EvidenceRescueTrace:
    """Validate and detach one strict evidence-rescue trace."""

    if not isinstance(value, Mapping) or set(value) != TRACE_FIELDS:
        raise EvidenceRescueContractError(
            "Evidence rescue trace fields do not match the contract."
        )
    if value["schema_version"] != EVIDENCE_RESCUE_SCHEMA_VERSION:
        raise EvidenceRescueContractError(
            "Unsupported evidence rescue schema version."
        )
    _identifier(value["capability"], "capability")
    _identifier(value["primary_provider"], "primary_provider")
    _identifier(value["primary_status"], "primary_status")
    if value["trigger"] not in RESCUE_TRIGGERS:
        raise EvidenceRescueContractError("Unsupported rescue trigger.")
    if value["stop_reason"] not in RESCUE_STOP_REASONS:
        raise EvidenceRescueContractError("Unsupported rescue stop reason.")
    if value["eligible"] is not True:
        raise EvidenceRescueContractError(
            "Stored evidence rescue traces must be eligible."
        )
    if not isinstance(value["attempted"], bool):
        raise EvidenceRescueContractError("attempted must be a boolean.")
    if not isinstance(value["recovered"], bool):
        raise EvidenceRescueContractError("recovered must be a boolean.")

    raw_identifiers = value["alternate_identifiers"]
    if (
        not isinstance(raw_identifiers, list)
        or len(raw_identifiers) > MAX_ALTERNATE_IDENTIFIERS
    ):
        raise EvidenceRescueContractError(
            "alternate_identifiers must be a bounded list."
        )
    for item in raw_identifiers:
        if not isinstance(item, Mapping) or set(item) != IDENTIFIER_FIELDS:
            raise EvidenceRescueContractError(
                "Alternate identifier fields do not match the contract."
            )
        _identifier(item["type"], "alternate identifier type")
        _text(item["value"], "alternate identifier value")
    identifier_pairs = {
        (item["type"], item["value"])
        for item in raw_identifiers
    }
    if len(identifier_pairs) != len(raw_identifiers):
        raise EvidenceRescueContractError(
            "alternate_identifiers must not contain duplicates."
        )

    attempts = value["attempts"]
    if not isinstance(attempts, list) or len(attempts) > MAX_RESCUE_ATTEMPTS:
        raise EvidenceRescueContractError(
            "attempts must be a bounded list."
        )
    recovered_providers: list[str] = []
    for index, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, Mapping) or set(attempt) != ATTEMPT_FIELDS:
            raise EvidenceRescueContractError(
                "Evidence rescue attempt fields do not match the contract."
            )
        if attempt["sequence"] != index:
            raise EvidenceRescueContractError(
                "Evidence rescue attempt sequence must be contiguous."
            )
        provider = _identifier(attempt["provider"], "attempt provider")
        _identifier(attempt["method"], "attempt method")
        identifier_type = attempt["identifier_type"]
        identifier_value = attempt["identifier_value"]
        if (identifier_type is None) != (identifier_value is None):
            raise EvidenceRescueContractError(
                "Attempt identifier type and value must appear together."
            )
        if identifier_type is not None:
            _identifier(identifier_type, "attempt identifier type")
            _text(identifier_value, "attempt identifier value")
            if (identifier_type, identifier_value) not in identifier_pairs:
                raise EvidenceRescueContractError(
                    "Attempt identifier must be declared as alternate."
                )
        if attempt["status"] not in RESCUE_STATUSES:
            raise EvidenceRescueContractError(
                "Unsupported evidence rescue attempt status."
            )
        if not isinstance(attempt["evidence_recovered"], bool):
            raise EvidenceRescueContractError(
                "evidence_recovered must be a boolean."
            )
        if not isinstance(attempt["independent_evidence"], bool):
            raise EvidenceRescueContractError(
                "independent_evidence must be a boolean."
            )
        evidence_path = _optional_text(
            attempt["evidence_path"], "attempt evidence_path"
        )
        if attempt["evidence_recovered"]:
            if attempt["status"] != "success" or evidence_path is None:
                raise EvidenceRescueContractError(
                    "Recovered evidence requires success and an evidence path."
                )
            recovered_providers.append(provider)
        elif evidence_path is not None:
            raise EvidenceRescueContractError(
                "Unrecovered attempts cannot name an evidence path."
            )

    if value["attempted"] != bool(attempts):
        raise EvidenceRescueContractError(
            "attempted must match the presence of rescue attempts."
        )
    if value["recovered"] != bool(recovered_providers):
        raise EvidenceRescueContractError(
            "recovered must match successful rescue attempts."
        )
    recovered_provider = value["recovered_provider"]
    if value["recovered"]:
        if (
            not isinstance(recovered_provider, str)
            or recovered_provider != recovered_providers[-1]
            or value["stop_reason"] != "evidence_recovered"
        ):
            raise EvidenceRescueContractError(
                "Recovered rescue provenance is inconsistent."
            )
    elif recovered_provider is not None:
        raise EvidenceRescueContractError(
            "Unrecovered rescue cannot name a recovered provider."
        )
    elif value["stop_reason"] == "evidence_recovered":
        raise EvidenceRescueContractError(
            "Unrecovered rescue cannot use the recovered stop reason."
        )

    return cast(EvidenceRescueTrace, deepcopy(dict(value)))


def build_evidence_rescue_trace(
    *,
    capability: str,
    trigger: RescueTrigger,
    primary_provider: str,
    primary_status: str,
    alternate_identifiers: list[EvidenceRescueIdentifier],
    attempts: list[EvidenceRescueAttempt],
    stop_reason: RescueStopReason,
) -> EvidenceRescueTrace:
    """Build one validated trace from a completed bounded rescue route."""

    recovered_attempts = [
        attempt for attempt in attempts if attempt["evidence_recovered"]
    ]
    return validate_evidence_rescue_trace(
        {
            "schema_version": EVIDENCE_RESCUE_SCHEMA_VERSION,
            "capability": capability,
            "trigger": trigger,
            "primary_provider": primary_provider,
            "primary_status": primary_status,
            "eligible": True,
            "attempted": bool(attempts),
            "alternate_identifiers": alternate_identifiers,
            "attempts": attempts,
            "recovered": bool(recovered_attempts),
            "recovered_provider": (
                recovered_attempts[-1]["provider"]
                if recovered_attempts
                else None
            ),
            "stop_reason": stop_reason,
        }
    )
