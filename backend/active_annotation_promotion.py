"""Stage 6B bounded promotion of proven shadow annotation candidates."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from typing import Any

from backend.shadow_composition import (
    ShadowCompositionError,
    validate_shadow_composition,
)


ACTIVE_ANNOTATION_PROMOTION_SCHEMA_VERSION = "1.0"
PROMOTABLE_FIELDS = ("gene", "transcript", "hgvs_c", "hgvs_p")
PROMOTION_SOURCES = frozenset({"VariantValidator", "GeneBe"})
_VERSIONED_TRANSCRIPT = re.compile(
    r"^(?:[A-Z]{2,}_[A-Z0-9]+|ENST[0-9]+)\.[0-9]+$"
)


class ActiveAnnotationPromotionError(ValueError):
    """Raised when active promotion provenance is malformed or unsafe."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _exact_identity_proof(value: object) -> dict[str, object] | None:
    proof = _mapping(value)
    if (
        set(proof) != {"decision", "expected", "observed"}
        or proof.get("decision") != "EXACT"
        or not isinstance(proof.get("expected"), Mapping)
        or not isinstance(proof.get("observed"), Mapping)
    ):
        return None
    return deepcopy(dict(proof))


def _composable_field(
    shadow: Mapping[str, object],
    field_name: str,
) -> dict[str, object] | None:
    gaps = _mapping(shadow.get("field_gaps"))
    fields = _mapping(shadow.get("fields"))
    field = _mapping(fields.get(field_name))
    candidate = _text(field.get("candidate"))
    source = _text(field.get("source"))
    proof = _exact_identity_proof(field.get("identity_proof"))
    if (
        gaps.get(field_name) is not True
        or field.get("decision") != "COMPOSABLE"
        or candidate is None
        or source not in PROMOTION_SOURCES
        or proof is None
    ):
        return None
    return {
        "candidate": candidate,
        "source": source,
        "identity_proof": proof,
        "transcript_context": _text(field.get("transcript_context")),
        "composition_state": "COMPOSABLE",
    }


def controlled_active_annotation_candidate(
    candidate: Mapping[str, object],
) -> dict[str, Any]:
    """Hide legacy VariantValidator fallback fields pending Stage 6B proof.

    The normalized fallback source remains intact for shadow composition and
    provenance.  Only the canonical Evidence Object projection is cleared,
    so every active fallback value must pass the Stage 6B field gate below.
    """

    active = deepcopy(dict(candidate))
    sources = _mapping(active.get("sources"))
    vep = _mapping(sources.get("vep"))
    if (
        vep.get("provider") == "VariantValidator"
        and vep.get("provider_role") == "fallback"
        and vep.get("fallback_for") == "ensembl_vep"
        and _text(vep.get("primary_failure")) is not None
    ):
        for field in (
            "gene",
            "gene_id",
            "transcript",
            "hgvsc",
            "hgvsp",
            "protein_change",
            "consequence",
            "impact",
        ):
            active[field] = None
    return active


def _source_payload(candidate: Mapping[str, object], source: str) -> Mapping[str, object]:
    sources = _mapping(candidate.get("sources"))
    if source != "VariantValidator":
        return _mapping(sources.get("genebe"))
    vep = _mapping(sources.get("vep"))
    verification = _mapping(vep.get("gene_identity_verification"))
    return verification if verification.get("status") == "success" else vep


def _variantvalidator_field_is_retained(
    payload: Mapping[str, object],
    field_name: str,
    candidate: str,
) -> bool:
    expected_key = {
        "gene": "validated_gene",
        "transcript": "validated_transcript",
        "hgvs_c": "validated_transcript_hgvs",
        "hgvs_p": "validated_protein_hgvs",
    }[field_name]
    legacy_fallback = (
        payload.get("provider") == "VariantValidator"
        and payload.get("provider_role") == "fallback"
        and payload.get("fallback_for") == "ensembl_vep"
        and payload.get("source_type") == "validation_mapping_fallback"
    )
    gene_verification = (
        payload.get("provider") == "VariantValidator"
        and payload.get("provider_role") == "verification"
        and payload.get("trigger") == "vep_gene_context_unresolved"
        and payload.get("status") == "success"
    )
    return (
        (legacy_fallback or gene_verification)
        and _text(payload.get(expected_key)) == candidate
    )


def _genebe_records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    records = payload.get("consequences")
    return [record for record in records if isinstance(record, Mapping)] if isinstance(records, list) else []


def _genebe_field_is_retained(
    payload: Mapping[str, object],
    field_name: str,
    candidate: str,
) -> bool:
    if payload.get("status") != "success" or payload.get("provider") != "GeneBe":
        return False
    record_key = {
        "gene": "gene",
        "transcript": "transcript",
        "hgvs_c": "hgvs_c",
        "hgvs_p": "hgvs_p",
    }[field_name]
    return any(_text(record.get(record_key)) == candidate for record in _genebe_records(payload))


def _accepted_source_field(
    candidate: Mapping[str, object],
    source: str,
    field_name: str,
    value: str,
) -> bool:
    payload = _source_payload(candidate, source)
    if source == "VariantValidator":
        return _variantvalidator_field_is_retained(payload, field_name, value)
    return _genebe_field_is_retained(payload, field_name, value)


def _gene_is_unconflicted(
    candidate: Mapping[str, object],
    selected_gene: str,
) -> bool:
    observed = {selected_gene}
    vep = _source_payload(candidate, "VariantValidator")
    genebe = _source_payload(candidate, "GeneBe")
    for value in (vep.get("validated_gene"), genebe.get("gene")):
        if (gene := _text(value)) is not None:
            observed.add(gene)
    for record in _genebe_records(genebe):
        if (gene := _text(record.get("gene"))) is not None:
            observed.add(gene)
    return len(observed) == 1


def _has_transcript_conflict(shadow: Mapping[str, object]) -> bool:
    for value in _mapping(shadow.get("fields")).values():
        field = _mapping(value)
        reasons = field.get("rejection_reasons")
        if (
            field.get("decision") == "CONFLICT"
            and isinstance(reasons, list)
            and "versioned_transcript_mismatch" in reasons
        ):
            return True
    return False


def _has_competing_transcript_context(
    candidate: Mapping[str, object],
    selected_transcript: str,
) -> bool:
    vep = _source_payload(candidate, "VariantValidator")
    transcripts = {
        transcript
        for transcript in [
            _text(vep.get("validated_transcript")),
            *[
                _text(record.get("transcript"))
                for record in _genebe_records(
                    _source_payload(candidate, "GeneBe")
                )
            ],
        ]
        if transcript is not None
    }
    return any(transcript != selected_transcript for transcript in transcripts)


def _source_record_is_transcript_coherent(
    candidate: Mapping[str, object],
    source: str,
    transcript: str,
    hgvs_c: str,
    hgvs_p: str,
) -> bool:
    payload = _source_payload(candidate, source)
    if source == "VariantValidator":
        return (
            _variantvalidator_field_is_retained(payload, "transcript", transcript)
            and _variantvalidator_field_is_retained(payload, "hgvs_c", hgvs_c)
            and _variantvalidator_field_is_retained(payload, "hgvs_p", hgvs_p)
            and _text(payload.get("selected_record")) is not None
        )
    matches = [
        record
        for record in _genebe_records(payload)
        if _text(record.get("transcript")) == transcript
        and _text(record.get("hgvs_c")) == hgvs_c
        and _text(record.get("hgvs_p")) == hgvs_p
    ]
    return (
        payload.get("status") == "success"
        and payload.get("provider") == "GeneBe"
        and len(matches) == 1
    )


def _versioned_bundle(
    candidate: Mapping[str, object],
    shadow: Mapping[str, object],
) -> dict[str, dict[str, object]] | None:
    if _has_transcript_conflict(shadow):
        return None
    fields = {
        name: _composable_field(shadow, name)
        for name in ("transcript", "hgvs_c", "hgvs_p")
    }
    if any(value is None for value in fields.values()):
        return None
    transcript_field = fields["transcript"]
    hgvs_c_field = fields["hgvs_c"]
    hgvs_p_field = fields["hgvs_p"]
    assert transcript_field is not None
    assert hgvs_c_field is not None
    assert hgvs_p_field is not None
    transcript = str(transcript_field["candidate"])
    hgvs_c = str(hgvs_c_field["candidate"])
    hgvs_p = str(hgvs_p_field["candidate"])
    source = transcript_field["source"]
    if (
        _VERSIONED_TRANSCRIPT.fullmatch(transcript) is None
        or _has_competing_transcript_context(candidate, transcript)
        or hgvs_c_field["source"] != source
        or hgvs_p_field["source"] != source
        or transcript_field["transcript_context"] != transcript
        or hgvs_c_field["transcript_context"] != transcript
        or hgvs_p_field["transcript_context"] != transcript
        or not hgvs_c.startswith(f"{transcript}:")
        or not _source_record_is_transcript_coherent(
            candidate,
            str(source),
            transcript,
            hgvs_c,
            hgvs_p,
        )
    ):
        return None
    return {
        "transcript": transcript_field,
        "hgvs_c": hgvs_c_field,
        "hgvs_p": hgvs_p_field,
    }


def _promotion_record(
    field: Mapping[str, object],
    *,
    selection_policy: str,
) -> dict[str, object]:
    transcript_context = field["transcript_context"]
    return {
        "active_value": field["candidate"],
        "source": field["source"],
        "identity_proof": deepcopy(field["identity_proof"]),
        "transcript_context": transcript_context,
        "composition_state": field["composition_state"],
        "promotion_state": "PROMOTED",
        "selection_policy": selection_policy,
        "limitations": ["vep_field_insufficient"],
    }


def _set_active_field(
    evidence: dict[str, Any],
    field_name: str,
    value: str,
) -> None:
    variant_context = evidence["variant_context"]
    if field_name == "gene":
        evidence["gene"] = value
        variant_context["gene"] = value
    elif field_name == "transcript":
        evidence["transcript"] = value
        variant_context["transcript"] = value
    elif field_name == "hgvs_c":
        variant_context["hgvs_c"] = value
    elif field_name == "hgvs_p":
        evidence["protein_change"] = value
        variant_context["hgvs_p"] = value


def promote_active_annotation_fields(
    evidence: dict[str, Any],
    *,
    candidate: Mapping[str, object],
    shadow: Mapping[str, object],
) -> dict[str, Any] | None:
    """Apply only Stage 6B-authorized fields to an Evidence Object in place."""

    try:
        validated_shadow = validate_shadow_composition(shadow)
    except ShadowCompositionError as exc:
        raise ActiveAnnotationPromotionError("Shadow composition is invalid.") from exc

    records: dict[str, dict[str, object]] = {}
    gene = _composable_field(validated_shadow, "gene")
    if (
        gene is not None
        and _accepted_source_field(
            candidate, str(gene["source"]), "gene", str(gene["candidate"])
        )
        and _gene_is_unconflicted(candidate, str(gene["candidate"]))
    ):
        _set_active_field(evidence, "gene", str(gene["candidate"]))
        records["gene"] = _promotion_record(
            gene,
            selection_policy="stage6b_exact_identity_gene",
        )

    bundle = _versioned_bundle(candidate, validated_shadow)
    if bundle is not None:
        for field_name, field in bundle.items():
            _set_active_field(evidence, field_name, str(field["candidate"]))
            records[field_name] = _promotion_record(
                field,
                selection_policy="stage6b_exact_versioned_transcript_bundle",
            )

    if not records:
        return None
    return validate_active_annotation_promotion(
        {
            "schema_version": ACTIVE_ANNOTATION_PROMOTION_SCHEMA_VERSION,
            "policy": "stage6b_controlled_active_promotion",
            "vep_state": validated_shadow["vep_state"],
            "fields": records,
        }
    )


def validate_active_annotation_promotion(value: object) -> dict[str, Any]:
    """Validate compact, source-attributed active-promotion provenance."""

    item = _mapping(value)
    if set(item) != {"schema_version", "policy", "vep_state", "fields"}:
        raise ActiveAnnotationPromotionError("Promotion fields are invalid.")
    if item.get("schema_version") != ACTIVE_ANNOTATION_PROMOTION_SCHEMA_VERSION:
        raise ActiveAnnotationPromotionError("Promotion version is unsupported.")
    if item.get("policy") != "stage6b_controlled_active_promotion":
        raise ActiveAnnotationPromotionError("Promotion policy is invalid.")
    if item.get("vep_state") not in {
        "operational_failure",
        "valid_no_match",
        "partial",
        "insufficient",
    }:
        raise ActiveAnnotationPromotionError("Promotion VEP state is invalid.")
    fields = _mapping(item.get("fields"))
    if not fields or not set(fields).issubset(PROMOTABLE_FIELDS):
        raise ActiveAnnotationPromotionError("Promotion field allowlist is invalid.")
    clean_fields: dict[str, dict[str, object]] = {}
    for field_name, raw_field in fields.items():
        field = _mapping(raw_field)
        if set(field) != {
            "active_value",
            "source",
            "identity_proof",
            "transcript_context",
            "composition_state",
            "promotion_state",
            "selection_policy",
            "limitations",
        }:
            raise ActiveAnnotationPromotionError("Promotion field record is invalid.")
        active_value = _text(field.get("active_value"))
        source = _text(field.get("source"))
        proof = _exact_identity_proof(field.get("identity_proof"))
        transcript_context = _text(field.get("transcript_context"))
        limitations = field.get("limitations")
        if (
            active_value is None
            or source not in PROMOTION_SOURCES
            or proof is None
            or field.get("composition_state") != "COMPOSABLE"
            or field.get("promotion_state") != "PROMOTED"
            or field.get("selection_policy") not in {
                "stage6b_exact_identity_gene",
                "stage6b_exact_versioned_transcript_bundle",
            }
            or not isinstance(limitations, list)
            or any(_text(item) is None for item in limitations)
            or len(set(limitations)) != len(limitations)
        ):
            raise ActiveAnnotationPromotionError("Promotion field state is invalid.")
        if field_name == "gene":
            if field.get("selection_policy") != "stage6b_exact_identity_gene":
                raise ActiveAnnotationPromotionError("Gene promotion provenance is invalid.")
        elif (
            transcript_context is None
            or _VERSIONED_TRANSCRIPT.fullmatch(transcript_context) is None
            or field.get("selection_policy") != "stage6b_exact_versioned_transcript_bundle"
        ):
            raise ActiveAnnotationPromotionError("Transcript promotion provenance is invalid.")
        clean_fields[field_name] = {
            "active_value": active_value,
            "source": source,
            "identity_proof": proof,
            "transcript_context": transcript_context,
            "composition_state": "COMPOSABLE",
            "promotion_state": "PROMOTED",
            "selection_policy": field["selection_policy"],
            "limitations": list(limitations),
        }
    if any(name in fields for name in ("transcript", "hgvs_c", "hgvs_p")) and set(fields) - {"gene"} != {
        "transcript",
        "hgvs_c",
        "hgvs_p",
    }:
        raise ActiveAnnotationPromotionError("Transcript promotion must be a complete bundle.")
    return {
        "schema_version": ACTIVE_ANNOTATION_PROMOTION_SCHEMA_VERSION,
        "policy": "stage6b_controlled_active_promotion",
        "vep_state": item["vep_state"],
        "fields": clean_fields,
    }


__all__ = [
    "ACTIVE_ANNOTATION_PROMOTION_SCHEMA_VERSION",
    "ActiveAnnotationPromotionError",
    "PROMOTABLE_FIELDS",
    "controlled_active_annotation_candidate",
    "promote_active_annotation_fields",
    "validate_active_annotation_promotion",
]
