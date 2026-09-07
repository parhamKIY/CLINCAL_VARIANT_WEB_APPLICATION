"""Selected-input outcome contract, isolated from evidence semantics."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from math import isfinite
from typing import Literal, TypedDict, cast

from backend.reference_sequence import (
    fetch_grch38_reference_sequence,
    reference_failure_reason,
    reference_sequence_is_success,
    reference_verification_provenance,
)
from backend.variant_identity import normalize_chromosome, normalize_variant_edit
from backend.vcf_processing import get_primary_chromosome_length, is_manual_gap_allele
from config import settings


INPUT_PREPROCESSING_SCHEMA_VERSION = "1.0"
INPUT_PREPROCESSING_STATUSES = frozenset(
    {"ACCEPTED_DIRECT", "NORMALIZED_AND_ACCEPTED", "IDENTITY_UNRESOLVED"}
)
INPUT_SOURCE_TYPES = frozenset({"vcf", "vcf_gz", "excel", "manual"})
SOURCE_PROVENANCE_FIELDS = frozenset(
    {
        "source_worksheet",
        "source_row",
        "source_chrom",
        "source_start",
        "source_end",
        "source_ref",
        "source_alt",
        "source_filter",
        "source_qual",
        "source_depth",
        "source_ad",
        "source_gq",
        "source_representation",
        "normalization_provenance",
        "reference_verification",
    }
)
CANONICAL_VARIANT_FIELDS = frozenset({"chrom", "pos", "ref", "alt"})

InputPreprocessingStatus = Literal[
    "ACCEPTED_DIRECT", "NORMALIZED_AND_ACCEPTED", "IDENTITY_UNRESOLVED"
]
SourceRepresentation = Literal[
    "STANDARD_ALLELE",
    "ANNOVAR_DELETION",
    "ANNOVAR_INSERTION",
    "UNSUPPORTED_OR_AMBIGUOUS",
]
ReferenceFetcher = Callable[..., Mapping[str, object]]


class SourceVariantProvenance(TypedDict):
    source_worksheet: str | None
    source_row: int | None
    source_chrom: str | None
    source_start: int | None
    source_end: int | None
    source_ref: str | None
    source_alt: str | None
    source_filter: str | None
    source_qual: float | None
    source_depth: float | None
    source_ad: float | None
    source_gq: float | None
    source_representation: str | None
    normalization_provenance: str | None
    reference_verification: str | None


class InputPreprocessingResult(TypedDict):
    schema_version: str
    input_id: str
    source_index: int
    source_type: str
    source_provenance: SourceVariantProvenance
    status: InputPreprocessingStatus
    canonical_variant: dict[str, object] | None
    canonical_variant_index: int | None
    canonical_variant_identity: str | None
    warnings: list[str]
    failure_reason: str | None


INPUT_PREPROCESSING_RESULT_FIELDS = frozenset(
    InputPreprocessingResult.__required_keys__
)


class InputPreprocessingError(ValueError):
    """Raised when a selected-input result violates the bounded contract."""


def _zero_token(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == 0
    ) or (isinstance(value, str) and value.strip() == "0")


def _allele(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    return normalized if normalized and set(normalized) <= {"A", "C", "G", "T"} else None


def classify_source_representation(
    reference: object,
    alternate: object,
) -> SourceRepresentation:
    """Classify source notation without altering it."""

    if _allele(reference) is not None and _allele(alternate) is not None:
        return "STANDARD_ALLELE"
    if _allele(reference) is not None and _zero_token(alternate):
        return "ANNOVAR_DELETION"
    if _zero_token(reference) and _allele(alternate) is not None:
        return "ANNOVAR_INSERTION"
    return "UNSUPPORTED_OR_AMBIGUOUS"


def _source_provenance_from_record(
    record: Mapping[str, object],
    representation: SourceRepresentation,
    *,
    normalization_provenance: str,
    reference_verification: str,
) -> dict[str, object]:
    return {
        "source_worksheet": record.get("worksheet"),
        "source_row": record.get("row"),
        "source_chrom": str(record.get("chrom")).strip() if record.get("chrom") is not None else None,
        "source_start": record.get("start"),
        "source_end": record.get("end"),
        "source_ref": str(record.get("ref")).strip() if record.get("ref") is not None else None,
        "source_alt": str(record.get("alt")).strip() if record.get("alt") is not None else None,
        "source_filter": record.get("filter"),
        "source_qual": record.get("qual"),
        "source_depth": record.get("depth"),
        "source_ad": record.get("ad"),
        "source_gq": record.get("gq"),
        "source_representation": representation,
        "normalization_provenance": normalization_provenance,
        "reference_verification": reference_verification,
    }


def _unresolved_adapter_record(
    record: Mapping[str, object],
    representation: SourceRepresentation,
    reason: str,
    reference_verification: str,
) -> dict[str, object]:
    return {
        "status": "IDENTITY_UNRESOLVED",
        "canonical_variant": None,
        "failure_reason": reason,
        "source_provenance": _source_provenance_from_record(
            record,
            representation,
            normalization_provenance="not_normalized",
            reference_verification=reference_verification,
        ),
    }


def _reference_failure(
    result: Mapping[str, object],
) -> tuple[str, str]:
    return (
        reference_failure_reason(result),
        reference_verification_provenance(result, outcome="unavailable"),
    )


def adapt_annovar_like_record(
    record: Mapping[str, object],
    *,
    reference_fetcher: ReferenceFetcher = fetch_grch38_reference_sequence,
    assembly: str = "GRCh38",
) -> dict[str, object]:
    """Adapt one ANNOVAR-like source record into safe canonical CPRA or hold it."""

    representation = classify_source_representation(record.get("ref"), record.get("alt"))
    chromosome = normalize_chromosome(record.get("chrom"))
    start = record.get("start")
    end = record.get("end")
    if end is None and representation == "STANDARD_ALLELE":
        end = start
    if (
        assembly != "GRCh38"
        or chromosome is None
        or isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 1
        or end < start
    ):
        return _unresolved_adapter_record(
            record, representation, "INVALID_INTERVAL", "not_attempted"
        )
    try:
        if end > get_primary_chromosome_length(chromosome, assembly):
            raise ValueError
    except (ValueError, KeyError):
        return _unresolved_adapter_record(
            record, representation, "INVALID_INTERVAL", "not_attempted"
        )
    if representation == "UNSUPPORTED_OR_AMBIGUOUS":
        return _unresolved_adapter_record(
            record, representation, "UNSUPPORTED_REPRESENTATION", "not_attempted"
        )
    if representation == "STANDARD_ALLELE":
        canonical = {
            "chrom": chromosome,
            "pos": start,
            "ref": _allele(record.get("ref")),
            "alt": _allele(record.get("alt")),
            "qual": record.get("qual"),
            "filter": record.get("filter"),
        }
        if normalize_variant_edit(canonical) is None:
            return _unresolved_adapter_record(
                record, representation, "IDENTITY_NOT_PROVEN", "not_required"
            )
        return {
            "status": "ACCEPTED_DIRECT",
            "canonical_variant": canonical,
            "failure_reason": None,
            "source_provenance": _source_provenance_from_record(
                record,
                representation,
                normalization_provenance="direct_vcf_compatible_allele",
                reference_verification="not_required",
            ),
        }
    if representation == "ANNOVAR_DELETION":
        deleted = _allele(record.get("ref"))
        assert deleted is not None
        if end - start + 1 != len(deleted) or start == 1:
            return _unresolved_adapter_record(
                record, representation, "INVALID_INTERVAL", "not_attempted"
            )
        interval = reference_fetcher(
            assembly=assembly, chrom=chromosome, start=start, end=end
        )
        observed = reference_sequence_is_success(
            interval, assembly=assembly, chrom=chromosome, start=start, end=end
        )
        if observed is None:
            reason, verification = _reference_failure(interval)
            return _unresolved_adapter_record(
                record, representation, reason, verification
            )
        if observed != deleted:
            return _unresolved_adapter_record(
                record,
                representation,
                "REFERENCE_MISMATCH",
                reference_verification_provenance(interval, outcome="mismatch"),
            )
        anchor_result = reference_fetcher(
            assembly=assembly, chrom=chromosome, start=start - 1, end=start - 1
        )
        anchor = reference_sequence_is_success(
            anchor_result,
            assembly=assembly,
            chrom=chromosome,
            start=start - 1,
            end=start - 1,
        )
        if anchor is None:
            reason, verification = _reference_failure(anchor_result)
            return _unresolved_adapter_record(
                record,
                representation,
                reason,
                f"deleted_interval_verified_{verification}",
            )
        canonical = {"chrom": chromosome, "pos": start - 1, "ref": anchor + deleted, "alt": anchor, "qual": record.get("qual"), "filter": record.get("filter")}
        if normalize_variant_edit(canonical) is None:
            return _unresolved_adapter_record(
                record, representation, "NORMALIZATION_FAILED", "verified"
            )
        return {
            "status": "NORMALIZED_AND_ACCEPTED",
            "canonical_variant": canonical,
            "failure_reason": None,
            "source_provenance": _source_provenance_from_record(
                record,
                representation,
                normalization_provenance="annovar_deletion_to_vcf_left_anchor",
                reference_verification=reference_verification_provenance(
                    anchor_result,
                    outcome="verified_source_ref_and_left_anchor",
                ),
            ),
        }
    if end != start:
        return _unresolved_adapter_record(
            record, representation, "INVALID_INTERVAL", "not_attempted"
        )
    inserted = _allele(record.get("alt"))
    assert inserted is not None
    anchor_result = reference_fetcher(
        assembly=assembly, chrom=chromosome, start=start, end=start
    )
    anchor = reference_sequence_is_success(
        anchor_result, assembly=assembly, chrom=chromosome, start=start, end=start
    )
    if anchor is None:
        reason, verification = _reference_failure(anchor_result)
        return _unresolved_adapter_record(
            record, representation, reason, verification
        )
    canonical = {"chrom": chromosome, "pos": start, "ref": anchor, "alt": anchor + inserted, "qual": record.get("qual"), "filter": record.get("filter")}
    if normalize_variant_edit(canonical) is None:
        return _unresolved_adapter_record(
            record, representation, "NORMALIZATION_FAILED", "verified"
        )
    return {
        "status": "NORMALIZED_AND_ACCEPTED",
        "canonical_variant": canonical,
        "failure_reason": None,
        "source_provenance": _source_provenance_from_record(
            record,
            representation,
            normalization_provenance="annovar_insertion_to_vcf_anchor_at_start",
            reference_verification=reference_verification_provenance(
                anchor_result,
                outcome="verified_anchor",
            ),
        ),
    }


def adapt_manual_source_record(
    record: Mapping[str, object],
    *,
    reference_fetcher: ReferenceFetcher = fetch_grch38_reference_sequence,
) -> dict[str, object]:
    """Use the approved reference anchor resolver, preserving manual notation."""
    working = dict(record)
    for field in ("ref", "alt"):
        if is_manual_gap_allele(working.get(field)):
            working[field] = "0"
    result = adapt_annovar_like_record(working, reference_fetcher=reference_fetcher)
    provenance = result["source_provenance"]
    for field in ("ref", "alt"):
        value = record.get(field)
        provenance[f"source_{field}"] = (str(value).strip() or None) if value is not None else None
    provenance["normalization_provenance"] = provenance["normalization_provenance"].replace(
        "annovar_", "manual_gap_"
    )
    provenance["source_representation"] = provenance["source_representation"].replace(
        "ANNOVAR_", "MANUAL_GAP_"
    )
    return result


def _text(value: object, path: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise InputPreprocessingError(f"{path} must be text.")
    normalized = value.strip()
    if not normalized or len(normalized) > 500 or any(
        ord(character) < 32 for character in normalized
    ):
        raise InputPreprocessingError(f"{path} is invalid.")
    return normalized


def _index(value: object, path: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputPreprocessingError(f"{path} is invalid.")
    return value


def _number(value: object, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputPreprocessingError(f"{path} is invalid.")
    converted = float(value)
    if not isfinite(converted):
        raise InputPreprocessingError(f"{path} is invalid.")
    return converted


def _source_provenance(value: object) -> SourceVariantProvenance:
    if not isinstance(value, Mapping) or set(value) != SOURCE_PROVENANCE_FIELDS:
        raise InputPreprocessingError("source_provenance has invalid fields.")
    item = dict(value)
    source_start = _index(item["source_start"], "source_start", optional=True)
    source_end = _index(item["source_end"], "source_end", optional=True)
    if source_start is not None and source_start == 0:
        raise InputPreprocessingError("source_start is invalid.")
    if source_end is not None and source_end == 0:
        raise InputPreprocessingError("source_end is invalid.")
    if (
        source_start is not None
        and source_end is not None
        and source_end < source_start
    ):
        raise InputPreprocessingError("source coordinate interval is invalid.")
    return {
        "source_worksheet": _text(item["source_worksheet"], "source_worksheet", optional=True),
        "source_row": _index(item["source_row"], "source_row", optional=True),
        "source_chrom": _text(item["source_chrom"], "source_chrom", optional=True),
        "source_start": source_start,
        "source_end": source_end,
        "source_ref": _text(item["source_ref"], "source_ref", optional=True),
        "source_alt": _text(item["source_alt"], "source_alt", optional=True),
        "source_filter": _text(item["source_filter"], "source_filter", optional=True),
        "source_qual": _number(item["source_qual"], "source_qual"),
        "source_depth": _number(item["source_depth"], "source_depth"),
        "source_ad": _number(item["source_ad"], "source_ad"),
        "source_gq": _number(item["source_gq"], "source_gq"),
        "source_representation": _text(item["source_representation"], "source_representation", optional=True),
        "normalization_provenance": _text(item["normalization_provenance"], "normalization_provenance", optional=True),
        "reference_verification": _text(item["reference_verification"], "reference_verification", optional=True),
    }


def _canonical_variant(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != CANONICAL_VARIANT_FIELDS:
        raise InputPreprocessingError("canonical_variant has invalid fields.")
    variant = dict(value)
    chrom = _text(variant["chrom"], "canonical_variant.chrom")
    pos = _index(variant["pos"], "canonical_variant.pos")
    if pos == 0:
        raise InputPreprocessingError("canonical_variant.pos is invalid.")
    return {
        "chrom": chrom,
        "pos": pos,
        "ref": _text(variant["ref"], "canonical_variant.ref"),
        "alt": _text(variant["alt"], "canonical_variant.alt"),
    }


def _warnings(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise InputPreprocessingError("input preprocessing warnings are invalid.")
    return [cast(str, _text(item, "input preprocessing warning")) for item in value]


def _default_provenance(
    value: Mapping[str, object],
    source_provenance: Mapping[str, object] | None,
) -> SourceVariantProvenance:
    supplied = dict(source_provenance or {})
    defaults: dict[str, object] = {
        "source_worksheet": None,
        "source_row": None,
        "source_chrom": value.get("chrom"),
        "source_start": value.get("pos"),
        "source_end": value.get("pos"),
        "source_ref": value.get("ref"),
        "source_alt": value.get("alt"),
        "source_filter": value.get("filter"),
        "source_qual": value.get("qual"),
        "source_depth": None,
        "source_ad": None,
        "source_gq": None,
        "source_representation": "vcf_like",
        "normalization_provenance": "direct_input",
        "reference_verification": "input_contract",
    }
    defaults.update(supplied)
    return _source_provenance(defaults)


def build_accepted_input_result(
    *,
    source_index: int,
    source_type: str,
    source_provenance: Mapping[str, object] | None,
    canonical_variant: Mapping[str, object],
    canonical_variant_index: int,
    canonical_variant_identity: str,
    status: InputPreprocessingStatus = "ACCEPTED_DIRECT",
) -> InputPreprocessingResult:
    """Build one accepted selected-input result with a stable canonical link."""

    if status not in {"ACCEPTED_DIRECT", "NORMALIZED_AND_ACCEPTED"}:
        raise InputPreprocessingError("Accepted input status is invalid.")
    canonical = _canonical_variant(
        {
            field: canonical_variant.get(field)
            for field in CANONICAL_VARIANT_FIELDS
        }
    )
    assert canonical is not None
    result: InputPreprocessingResult = {
        "schema_version": INPUT_PREPROCESSING_SCHEMA_VERSION,
        "input_id": f"input-{source_index:03d}",
        "source_index": source_index,
        "source_type": source_type,
        "source_provenance": _default_provenance(
            canonical_variant,
            source_provenance,
        ),
        "status": status,
        "canonical_variant": canonical,
        "canonical_variant_index": canonical_variant_index,
        "canonical_variant_identity": canonical_variant_identity,
        "warnings": [],
        "failure_reason": None,
    }
    return validate_input_preprocessing_result(result)


def build_unresolved_input_result(
    *,
    source_index: int,
    source_type: str,
    source_provenance: Mapping[str, object],
    failure_reason: str,
    warnings: Sequence[str] = (),
) -> InputPreprocessingResult:
    """Build one retained input that has no safe canonical allele identity."""

    result: InputPreprocessingResult = {
        "schema_version": INPUT_PREPROCESSING_SCHEMA_VERSION,
        "input_id": f"input-{source_index:03d}",
        "source_index": source_index,
        "source_type": source_type,
        "source_provenance": _default_provenance(
            source_provenance,
            source_provenance,
        ),
        "status": "IDENTITY_UNRESOLVED",
        "canonical_variant": None,
        "canonical_variant_index": None,
        "canonical_variant_identity": None,
        "warnings": list(warnings),
        "failure_reason": failure_reason,
    }
    return validate_input_preprocessing_result(result)


def validate_input_preprocessing_result(value: object) -> InputPreprocessingResult:
    if not isinstance(value, Mapping) or set(value) != INPUT_PREPROCESSING_RESULT_FIELDS:
        raise InputPreprocessingError("input preprocessing result has invalid fields.")
    item = dict(value)
    if item["schema_version"] != INPUT_PREPROCESSING_SCHEMA_VERSION:
        raise InputPreprocessingError(
            "input preprocessing schema is unsupported."
        )
    source_index = _index(item["source_index"], "source_index")
    assert source_index is not None
    input_id = _text(item["input_id"], "input_id")
    assert input_id is not None
    if input_id != f"input-{source_index:03d}":
        raise InputPreprocessingError("input_id does not match source_index.")
    source_type = _text(item["source_type"], "source_type")
    assert source_type is not None
    if source_type not in INPUT_SOURCE_TYPES:
        raise InputPreprocessingError("source_type is unsupported.")
    status = item["status"]
    if status not in INPUT_PREPROCESSING_STATUSES:
        raise InputPreprocessingError("input preprocessing status is invalid.")
    canonical = _canonical_variant(item["canonical_variant"])
    canonical_index = _index(
        item["canonical_variant_index"], "canonical_variant_index", optional=True
    )
    canonical_identity = _text(
        item["canonical_variant_identity"], "canonical_variant_identity", optional=True
    )
    failure_reason = _text(item["failure_reason"], "failure_reason", optional=True)
    if status == "IDENTITY_UNRESOLVED":
        if canonical is not None or canonical_index is not None or canonical_identity is not None:
            raise InputPreprocessingError("Unresolved input cannot have a canonical variant.")
        if failure_reason is None:
            raise InputPreprocessingError("Unresolved input requires a failure reason.")
    elif canonical is None or canonical_index is None or canonical_identity is None or failure_reason is not None:
        raise InputPreprocessingError("Accepted input requires exactly one canonical variant.")
    return cast(
        InputPreprocessingResult,
        {
            "schema_version": INPUT_PREPROCESSING_SCHEMA_VERSION,
            "input_id": input_id,
            "source_index": source_index,
            "source_type": source_type,
            "source_provenance": _source_provenance(item["source_provenance"]),
            "status": cast(InputPreprocessingStatus, status),
            "canonical_variant": canonical,
            "canonical_variant_index": canonical_index,
            "canonical_variant_identity": canonical_identity,
            "warnings": _warnings(item["warnings"]),
            "failure_reason": failure_reason,
        },
    )


def validate_input_preprocessing_results(
    value: object,
    *,
    variants: Sequence[Mapping[str, object]],
    variant_integrity_records: Sequence[Mapping[str, object]],
) -> list[InputPreprocessingResult]:
    """Require one retained result per selected input and exact accepted links."""

    if not isinstance(value, list) or len(value) > 10:
        raise InputPreprocessingError("input preprocessing results are invalid.")
    results = [validate_input_preprocessing_result(item) for item in value]
    if [item["source_index"] for item in results] != list(range(len(results))):
        raise InputPreprocessingError("input preprocessing results must preserve source order.")
    linked_indexes: set[int] = set()
    for item in results:
        if item["status"] == "IDENTITY_UNRESOLVED":
            continue
        index = item["canonical_variant_index"]
        identity = item["canonical_variant_identity"]
        canonical = item["canonical_variant"]
        assert index is not None and identity is not None and canonical is not None
        if index >= len(variants) or index >= len(variant_integrity_records):
            raise InputPreprocessingError("canonical variant link is invalid.")
        variant = variants[index]
        if any(variant.get(field) != canonical[field] for field in CANONICAL_VARIANT_FIELDS):
            raise InputPreprocessingError("canonical variant link does not match variant identity.")
        if variant_integrity_records[index].get("parser_allele_identity") != identity:
            raise InputPreprocessingError("canonical variant identity link is invalid.")
        if index in linked_indexes:
            raise InputPreprocessingError("canonical variant link is duplicated.")
        linked_indexes.add(index)
    if linked_indexes != set(range(len(variants))):
        raise InputPreprocessingError("every analyzable variant requires one selected-input link.")
    return deepcopy(results)


__all__ = [
    "CANONICAL_VARIANT_FIELDS",
    "INPUT_PREPROCESSING_SCHEMA_VERSION",
    "InputPreprocessingStatus",
    "InputPreprocessingError",
    "InputPreprocessingResult",
    "ReferenceFetcher",
    "SourceRepresentation",
    "SourceVariantProvenance",
    "adapt_annovar_like_record",
    "build_accepted_input_result",
    "build_unresolved_input_result",
    "classify_source_representation",
    "validate_input_preprocessing_result",
    "validate_input_preprocessing_results",
]
