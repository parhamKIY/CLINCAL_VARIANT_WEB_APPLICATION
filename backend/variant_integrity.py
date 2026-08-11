"""Stage 87 variant cardinality and allele-identity integrity gate."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import TypedDict, cast


VARIANT_INTEGRITY_SCHEMA_VERSION = "1.0"


class VariantIntegrityError(ValueError):
    """Raised when variant cardinality, order, or identity drifts."""


class VariantIntegrityRecord(TypedDict):
    """One input-indexed allele lineage across the report workflow."""

    schema_version: str
    input_index: int
    parser_allele_identity: str
    normalized_allele_identity: str
    pipeline_allele_identity: str
    evidence_allele_identity: str | None
    draft_report_id: str | None
    review_record_id: str | None


RECORD_FIELDS = frozenset(VariantIntegrityRecord.__required_keys__)


def stable_allele_identity(
    variant: Mapping[str, object],
    *,
    assembly: object,
) -> str:
    """Return an assembly-qualified allele identity without gene collapse."""

    if not isinstance(assembly, str) or assembly not in {"GRCh37", "GRCh38"}:
        raise VariantIntegrityError("Variant identity requires GRCh37 or GRCh38.")
    chromosome = variant.get("chrom")
    position = variant.get("pos")
    reference = variant.get("ref")
    alternate = variant.get("alt")
    if not isinstance(chromosome, str) or not chromosome.strip():
        raise VariantIntegrityError("Variant chromosome is invalid.")
    if isinstance(position, bool) or not isinstance(position, int) or position <= 0:
        raise VariantIntegrityError("Variant position is invalid.")
    if not isinstance(reference, str) or not reference.strip():
        raise VariantIntegrityError("Variant reference allele is invalid.")
    if not isinstance(alternate, str) or not alternate.strip():
        raise VariantIntegrityError("Variant alternate allele is invalid.")
    normalized_chromosome = chromosome.strip().removeprefix("chr").upper()
    if normalized_chromosome == "M":
        normalized_chromosome = "MT"
    canonical = (
        f"{assembly}:{normalized_chromosome}:{position}:"
        f"{reference.strip().upper()}:{alternate.strip().upper()}"
    )
    return "allele-sha256:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def index_input_variants(
    variants: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Attach the immutable parser-order index to every accepted allele."""

    indexed: list[dict[str, object]] = []
    for input_index, variant in enumerate(variants):
        item = dict(variant)
        existing = item.get("input_index")
        if existing is not None and existing != input_index:
            raise VariantIntegrityError(
                "Input variant index does not match parser order."
            )
        item["input_index"] = input_index
        indexed.append(item)
    return indexed


def _index(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise VariantIntegrityError(f"{path} is invalid.")
    return value


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise VariantIntegrityError(f"{path} is invalid.")
    return value.strip()


def validate_variant_integrity_record(value: object) -> VariantIntegrityRecord:
    """Validate one persisted allele-lineage record."""

    if not isinstance(value, Mapping) or set(value) != RECORD_FIELDS:
        raise VariantIntegrityError("variant_integrity_record has invalid fields.")
    item = dict(value)
    if item["schema_version"] != VARIANT_INTEGRITY_SCHEMA_VERSION:
        raise VariantIntegrityError("Variant integrity schema is unsupported.")
    _index(item["input_index"], "variant_integrity_record.input_index")
    for field in (
        "parser_allele_identity",
        "normalized_allele_identity",
        "pipeline_allele_identity",
    ):
        if not isinstance(item[field], str) or not item[field].strip():
            raise VariantIntegrityError(
                f"variant_integrity_record.{field} is invalid."
            )
    _optional_text(
        item["evidence_allele_identity"],
        "variant_integrity_record.evidence_allele_identity",
    )
    _optional_text(
        item["draft_report_id"],
        "variant_integrity_record.draft_report_id",
    )
    _optional_text(
        item["review_record_id"],
        "variant_integrity_record.review_record_id",
    )
    identities = {
        cast(str, item["parser_allele_identity"]),
        cast(str, item["normalized_allele_identity"]),
        cast(str, item["pipeline_allele_identity"]),
    }
    evidence_identity = item["evidence_allele_identity"]
    if evidence_identity is not None:
        identities.add(cast(str, evidence_identity))
    if len(identities) != 1:
        raise VariantIntegrityError(
            "Variant allele identity changed between pipeline stages."
        )
    return deepcopy(cast(VariantIntegrityRecord, item))


def _ordered_collection(
    values: Sequence[Mapping[str, object]],
    *,
    expected_count: int,
    index_field: str,
    path: str,
) -> None:
    if values and len(values) != expected_count:
        raise VariantIntegrityError(
            f"{path} count does not match accepted input cardinality."
    )
    for expected_index, value in enumerate(values):
        observed_index = _index(
            value.get(index_field),
            f"{path}[{expected_index}].{index_field}",
        )
        if observed_index != expected_index:
            raise VariantIntegrityError(f"{path} does not preserve input order.")


def build_variant_integrity_records(
    parser_variants: Sequence[Mapping[str, object]],
    normalized_variants: Sequence[Mapping[str, object]],
    *,
    assembly: str,
    evidence_objects: Sequence[Mapping[str, object]] = (),
    draft_reports: Sequence[Mapping[str, object]] = (),
    review_records: Sequence[Mapping[str, object]] = (),
) -> list[VariantIntegrityRecord]:
    """Build the strict end-to-end count, order, and identity ledger."""

    if len(parser_variants) != len(normalized_variants):
        raise VariantIntegrityError(
            "Parser and normalized variant counts must be equal."
        )
    count = len(normalized_variants)
    _ordered_collection(
        parser_variants,
        expected_count=count,
        index_field="input_index",
        path="parser_variants",
    )
    _ordered_collection(
        normalized_variants,
        expected_count=count,
        index_field="input_index",
        path="normalized_variants",
    )
    _ordered_collection(
        draft_reports,
        expected_count=count,
        index_field="variant_index",
        path="draft_reports",
    )
    _ordered_collection(
        review_records,
        expected_count=count,
        index_field="variant_index",
        path="review_records",
    )
    if evidence_objects and len(evidence_objects) != count:
        raise VariantIntegrityError(
            "Evidence Object count does not match accepted input cardinality."
        )

    records: list[VariantIntegrityRecord] = []
    for input_index in range(count):
        parser_identity = stable_allele_identity(
            parser_variants[input_index], assembly=assembly
        )
        normalized_identity = stable_allele_identity(
            normalized_variants[input_index], assembly=assembly
        )
        evidence_identity: str | None = None
        if evidence_objects:
            evidence = evidence_objects[input_index]
            evidence_variant = evidence.get("variant")
            evidence_assembly = evidence.get("assembly")
            if not isinstance(evidence_variant, Mapping):
                raise VariantIntegrityError("Evidence allele identity is missing.")
            evidence_identity = stable_allele_identity(
                evidence_variant,
                assembly=evidence_assembly,
            )

        draft_report_id = None
        if draft_reports:
            draft_report_id = _optional_text(
                draft_reports[input_index].get("report_id"),
                f"draft_reports[{input_index}].report_id",
            )
        review_record_id = None
        if review_records:
            review_record_id = _optional_text(
                review_records[input_index].get("report_id"),
                f"review_records[{input_index}].report_id",
            )
        record: VariantIntegrityRecord = {
            "schema_version": VARIANT_INTEGRITY_SCHEMA_VERSION,
            "input_index": input_index,
            "parser_allele_identity": parser_identity,
            "normalized_allele_identity": normalized_identity,
            "pipeline_allele_identity": normalized_identity,
            "evidence_allele_identity": evidence_identity,
            "draft_report_id": draft_report_id,
            "review_record_id": review_record_id,
        }
        records.append(validate_variant_integrity_record(record))
    return records


def cardinality_counts(
    records: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Return acceptance-gate counts from validated lineage records."""

    validated = [validate_variant_integrity_record(record) for record in records]
    return {
        "parser": len(validated),
        "normalized": len(validated),
        "pipeline": len(validated),
        "evidence_package": sum(
            record["evidence_allele_identity"] is not None for record in validated
        ),
        "draft_report": sum(
            record["draft_report_id"] is not None for record in validated
        ),
        "review_record": sum(
            record["review_record_id"] is not None for record in validated
        ),
    }


__all__ = [
    "VARIANT_INTEGRITY_SCHEMA_VERSION",
    "VariantIntegrityError",
    "VariantIntegrityRecord",
    "build_variant_integrity_records",
    "cardinality_counts",
    "index_input_variants",
    "stable_allele_identity",
    "validate_variant_integrity_record",
]
