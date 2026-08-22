"""Stage 6A observational annotation-composition diagnostics.

This module deliberately produces candidates only.  It never promotes a
candidate into canonical annotation, readiness, or LLM-bound evidence.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from typing import Any, Literal

from backend.privacy import (
    VARIANT_INTERPRETATION_COMPACTION_FIELDS,
    VARIANT_INTERPRETATION_PROHIBITED_EVIDENCE_FIELDS,
)


SHADOW_COMPOSITION_SCHEMA_VERSION = "1.0"
FIELD_NAMES = (
    "gene",
    "transcript",
    "hgvs_c",
    "hgvs_p",
    "consequence",
    "impact",
    "mane",
    "canonical",
)
GAP_FIELDS = FIELD_NAMES[:5]
VEP_STATES = frozenset(
    {
        "usable",
        "operational_failure",
        "valid_no_match",
        "partial",
        "insufficient",
    }
)
COMPOSITION_DECISIONS = frozenset(
    {
        "COMPOSABLE",
        "UNAVAILABLE",
        "CONFLICT",
        "REJECTED_UNSAFE",
        "NOT_COMPOSED",
    }
)
IDENTITY_DECISIONS = frozenset({"EXACT", "MISMATCH", "UNPROVEN"})
EVALUATION_MODES = frozenset({"gap_driven", "counterfactual"})

# A conservative, deterministic subset of the Sequence Ontology terms emitted
# by the existing transcript-consequence adapter.  Unknown or ambiguous terms
# stay observationally rejected until a separately reviewed semantic contract
# expands this set.
RECOGNIZED_SO_CONSEQUENCES = frozenset(
    {
        "3_prime_UTR_variant",
        "5_prime_UTR_variant",
        "coding_sequence_variant",
        "downstream_gene_variant",
        "frameshift_variant",
        "inframe_deletion",
        "inframe_insertion",
        "intron_variant",
        "missense_variant",
        "non_coding_transcript_variant",
        "protein_altering_variant",
        "regulatory_region_variant",
        "splice_acceptor_variant",
        "splice_donor_variant",
        "start_lost",
        "stop_gained",
        "stop_lost",
        "synonymous_variant",
        "transcript_ablation",
        "transcript_amplification",
        "upstream_gene_variant",
    }
)

Identity = dict[str, str | int]
ShadowComposition = dict[str, Any]


class ShadowCompositionError(ValueError):
    """Raised when a Stage 6A diagnostic violates its bounded contract."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identity(value: object, *, required: bool) -> Identity | None:
    item = _mapping(value)
    assembly = _text(item.get("assembly"))
    chrom = _text(item.get("chrom"))
    reference = _text(item.get("ref"))
    alternate = _text(item.get("alt"))
    position = item.get("pos")
    if chrom is not None:
        chrom = chrom.removeprefix("chr").upper()
    if reference is not None:
        reference = reference.upper()
    if alternate is not None:
        alternate = alternate.upper()
    if (
        assembly not in {"GRCh37", "GRCh38"}
        or chrom is None
        or reference is None
        or alternate is None
        or isinstance(position, bool)
        or not isinstance(position, int)
        or position <= 0
    ):
        if required:
            raise ShadowCompositionError("Identity context is invalid.")
        return None
    return {
        "assembly": assembly,
        "chrom": chrom,
        "pos": position,
        "ref": reference,
        "alt": alternate,
    }


def _identity_proof(expected: Identity, observed: object) -> dict[str, object]:
    normalized = _identity(observed, required=False)
    return {
        "decision": (
            "UNPROVEN"
            if normalized is None
            else "EXACT"
            if normalized == expected
            else "MISMATCH"
        ),
        "expected": deepcopy(expected),
        "observed": deepcopy(normalized),
    }


def _field(
    *,
    candidate: str | bool | None = None,
    source: str | None = None,
    identity_proof: Mapping[str, object] | None = None,
    transcript_context: str | None = None,
    semantic_validation: str | None = None,
    decision: str,
    rejection_reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "candidate": candidate,
        "source": source,
        "identity_proof": deepcopy(identity_proof)
        if identity_proof is not None
        else None,
        "transcript_context": transcript_context,
        "semantic_validation": semantic_validation,
        "decision": decision,
        "rejection_reasons": list(rejection_reasons or []),
    }


def _unavailable_or_unsafe(
    *,
    source: str | None,
    proof: Mapping[str, object] | None,
    reason: str,
) -> dict[str, object]:
    proof_decision = proof.get("decision") if proof else None
    return _field(
        source=source,
        identity_proof=proof,
        decision=(
            "REJECTED_UNSAFE"
            if proof_decision in {"MISMATCH", "UNPROVEN"}
            else "UNAVAILABLE"
        ),
        rejection_reasons=[reason],
    )


def _transcript_from_hgvs(value: object) -> str | None:
    text = _text(value)
    if text is None or ":" not in text:
        return None
    return _text(text.split(":", 1)[0])


def _record_for_transcript(
    records: list[Mapping[str, object]],
    transcript: str | None,
) -> Mapping[str, object] | None:
    if transcript is None:
        return None
    return next(
        (
            record
            for record in records
            if _text(record.get("transcript")) == transcript
        ),
        None,
    )


def compose_shadow_annotation(
    *,
    identity_context: Mapping[str, object],
    vep_state: str,
    field_gaps: Mapping[str, object],
    fallback_evidence: Mapping[str, object],
    evaluation_mode: Literal["gap_driven", "counterfactual"],
) -> ShadowComposition:
    """Compose a pure, never-promoted field-level diagnostic.

    The composer receives normalized fallback evidence and explicit VEP state/
    gaps.  It does not accept or inspect VEP field values.
    """

    expected = _identity(identity_context, required=True)
    if vep_state not in VEP_STATES:
        raise ShadowCompositionError("VEP state is invalid.")
    if evaluation_mode not in EVALUATION_MODES:
        raise ShadowCompositionError("Shadow evaluation mode is invalid.")
    if set(field_gaps) != set(GAP_FIELDS) or any(
        not isinstance(field_gaps[field], bool) for field in GAP_FIELDS
    ):
        raise ShadowCompositionError("Field gaps must be complete booleans.")

    fallback = _mapping(fallback_evidence)
    vv = _mapping(fallback.get("variantvalidator"))
    gb = _mapping(fallback.get("genebe"))
    vv_proof = _identity_proof(expected, vv.get("identity"))
    gb_proof = _identity_proof(expected, gb.get("identity"))
    vv_exact = vv_proof["decision"] == "EXACT"
    gb_exact = gb_proof["decision"] == "EXACT"
    vv_transcript = _text(vv.get("transcript"))
    records = [
        record
        for item in gb.get("records", [])
        if isinstance(item, Mapping)
        for record in [item]
    ]
    gb_transcripts = [
        transcript
        for record in records
        if (transcript := _text(record.get("transcript"))) is not None
    ]

    owner_source: str | None = None
    owner_transcript: str | None = None
    owner_record: Mapping[str, object] | None = None
    transcript_conflict = False
    if vv_exact and vv_transcript is not None:
        owner_source = "VariantValidator"
        owner_transcript = vv_transcript
        owner_record = _record_for_transcript(records, vv_transcript)
        transcript_conflict = bool(gb_transcripts) and owner_record is None
    elif gb_exact and gb_transcripts:
        owner_source = "GeneBe"
        owner_transcript = gb_transcripts[0]
        owner_record = _record_for_transcript(records, owner_transcript)

    fields: dict[str, dict[str, object]] = {}
    for field in ("impact", "mane", "canonical"):
        fields[field] = _field(
            decision="NOT_COMPOSED",
            rejection_reasons=["stage6a_structural_block"],
        )

    def is_gap(field: str) -> bool:
        return bool(field_gaps[field])

    def not_gap() -> dict[str, object]:
        return _field(
            decision="NOT_COMPOSED",
            rejection_reasons=["vep_field_not_insufficient"],
        )

    for name in ("gene", "transcript", "hgvs_c", "hgvs_p"):
        if not is_gap(name):
            fields[name] = not_gap()
            continue
        if owner_source == "VariantValidator":
            field_name = {"hgvs_c": "hgvs_c", "hgvs_p": "hgvs_p"}.get(
                name, name
            )
            candidate = _text(vv.get(field_name))
            if candidate is None:
                fields[name] = _unavailable_or_unsafe(
                    source="VariantValidator",
                    proof=vv_proof,
                    reason="validated_variantvalidator_field_unavailable",
                )
            else:
                fields[name] = _field(
                    candidate=candidate,
                    source="VariantValidator",
                    identity_proof=vv_proof,
                    transcript_context=owner_transcript,
                    semantic_validation="validated_mapping_record",
                    decision="COMPOSABLE",
                )
            continue
        if owner_source == "GeneBe" and owner_record is not None:
            candidate = _text(
                owner_record.get(
                    {"hgvs_c": "hgvs_c", "hgvs_p": "hgvs_p"}.get(
                        name, name
                    )
                )
            )
            if candidate is None:
                fields[name] = _unavailable_or_unsafe(
                    source="GeneBe",
                    proof=gb_proof,
                    reason="transcript_bound_genebe_field_unavailable",
                )
            else:
                fields[name] = _field(
                    candidate=candidate,
                    source="GeneBe",
                    identity_proof=gb_proof,
                    transcript_context=owner_transcript,
                    semantic_validation="transcript_bound_consequence_record",
                    decision="COMPOSABLE",
                )
            continue
        source, proof = (
            ("VariantValidator", vv_proof)
            if vv
            else ("GeneBe", gb_proof)
            if gb
            else (None, None)
        )
        fields[name] = _unavailable_or_unsafe(
            source=source,
            proof=proof,
            reason=(
                "identity_proof_unavailable"
                if proof is not None and proof.get("decision") == "UNPROVEN"
                else "proven_transcript_context_unavailable"
            ),
        )

    if not is_gap("consequence"):
        fields["consequence"] = not_gap()
    elif not gb:
        fields["consequence"] = _field(
            decision="UNAVAILABLE",
            rejection_reasons=["genebe_evidence_unavailable"],
        )
    elif not gb_exact:
        fields["consequence"] = _unavailable_or_unsafe(
            source="GeneBe",
            proof=gb_proof,
            reason="genebe_identity_not_exact",
        )
    elif transcript_conflict:
        fields["consequence"] = _field(
            source="GeneBe",
            identity_proof=gb_proof,
            transcript_context=owner_transcript,
            decision="CONFLICT",
            rejection_reasons=["versioned_transcript_mismatch"],
        )
    elif owner_record is None:
        fields["consequence"] = _field(
            source="GeneBe",
            identity_proof=gb_proof,
            decision="REJECTED_UNSAFE",
            rejection_reasons=["transcript_bound_consequence_unavailable"],
        )
    else:
        terms = [
            term
            for value in owner_record.get("consequence_terms", [])
            if (term := _text(value)) is not None
        ]
        if len(terms) != 1:
            fields["consequence"] = _field(
                source="GeneBe",
                identity_proof=gb_proof,
                transcript_context=owner_transcript,
                decision="REJECTED_UNSAFE",
                rejection_reasons=["ambiguous_consequence_term"],
            )
        elif terms[0] not in RECOGNIZED_SO_CONSEQUENCES:
            fields["consequence"] = _field(
                source="GeneBe",
                identity_proof=gb_proof,
                transcript_context=owner_transcript,
                decision="REJECTED_UNSAFE",
                rejection_reasons=["unknown_consequence_term"],
            )
        else:
            fields["consequence"] = _field(
                candidate=terms[0],
                source="GeneBe",
                identity_proof=gb_proof,
                transcript_context=owner_transcript,
                semantic_validation="recognized_transcript_bound_so_term",
                decision="COMPOSABLE",
            )

    result: ShadowComposition = {
        "schema_version": SHADOW_COMPOSITION_SCHEMA_VERSION,
        "vep_state": vep_state,
        "evaluation_mode": evaluation_mode,
        "identity_context": deepcopy(expected),
        "field_gaps": {field: bool(field_gaps[field]) for field in GAP_FIELDS},
        "fields": {field: fields[field] for field in FIELD_NAMES},
    }
    return validate_shadow_composition(result)


def _vep_state_and_gaps(candidate: Mapping[str, object]) -> tuple[str, dict[str, bool]]:
    sources = _mapping(candidate.get("sources"))
    vep = _mapping(sources.get("vep"))
    status = _text(vep.get("status"))
    primary_failure = _text(vep.get("primary_failure"))
    operational_failures = {
        "error",
        "unavailable",
        "timeout",
        "forbidden",
        "rate_limited",
        "server_error",
        "invalid_response",
        "configuration_error",
    }
    direct_vep = _text(vep.get("provider")) in {None, "Ensembl VEP"}
    present = {
        "gene": _text(candidate.get("gene")) is not None,
        "transcript": _text(candidate.get("transcript")) is not None,
        "hgvs_c": _text(candidate.get("hgvsc")) is not None,
        "hgvs_p": _text(candidate.get("hgvsp")) is not None
        or _text(candidate.get("protein_change")) is not None,
        "consequence": _text(candidate.get("consequence")) is not None,
    }
    if status in operational_failures or primary_failure in operational_failures:
        return "operational_failure", {field: True for field in GAP_FIELDS}
    if status in {"not_found", "no_match"}:
        return "valid_no_match", {field: True for field in GAP_FIELDS}
    if direct_vep and status == "success" and all(present.values()):
        return "usable", {field: False for field in GAP_FIELDS}
    if direct_vep and any(present.values()):
        return "partial", {field: not present[field] for field in GAP_FIELDS}
    return "insufficient", {field: True for field in GAP_FIELDS}


def _fallback_evidence_from_candidate(
    candidate: Mapping[str, object],
    expected: Identity,
) -> dict[str, object]:
    sources = _mapping(candidate.get("sources"))
    vep = _mapping(sources.get("vep"))
    genebe = _mapping(sources.get("genebe"))
    vv_identity = _mapping(vep.get("normalized_variant"))
    vv_input: dict[str, object] = {}
    if vv_identity:
        vv_input = {
            "identity": {
                "assembly": vv_identity.get("assembly"),
                "chrom": vv_identity.get("chrom"),
                "pos": vv_identity.get("pos"),
                "ref": vv_identity.get("ref"),
                "alt": vv_identity.get("alt"),
            },
            "gene": vep.get("validated_gene"),
            "transcript": (
                vep.get("validated_transcript")
                or _transcript_from_hgvs(vep.get("validated_transcript_hgvs"))
            ),
            "hgvs_c": vep.get("validated_transcript_hgvs"),
            "hgvs_p": vep.get("validated_protein_hgvs"),
        }
    gb_identity: dict[str, object] | None = None
    returned = _mapping(genebe.get("returned_variant"))
    if genebe.get("status") == "success" and returned:
        gb_identity = {
            "assembly": genebe.get("request_assembly"),
            "chrom": returned.get("chrom"),
            "pos": returned.get("pos"),
            "ref": returned.get("ref"),
            "alt": returned.get("alt"),
        }
    records = [
        {
            "gene": record.get("gene"),
            "transcript": record.get("transcript"),
            "hgvs_c": record.get("hgvs_c"),
            "hgvs_p": record.get("hgvs_p"),
            "consequence_terms": deepcopy(record.get("consequence_terms", [])),
        }
        for record in genebe.get("consequences", [])
        if genebe.get("status") == "success" and isinstance(record, Mapping)
    ]
    return {
        "variantvalidator": vv_input,
        "genebe": {"identity": gb_identity, "records": records},
        "myvariant": {},
        "identity_context": deepcopy(expected),
    }


def build_shadow_composition(candidate: Mapping[str, object]) -> ShadowComposition | None:
    """Build one production gap-driven diagnostic from existing evidence only."""

    sources = _mapping(candidate.get("sources"))
    vep = _mapping(sources.get("vep"))
    # Legacy/minimal test candidates without normalized VEP provenance are not
    # production annotation records and remain byte-for-byte compatible.
    if _text(vep.get("provider")) is None:
        return None
    expected = _identity(
        {
            "assembly": candidate.get("assembly"),
            **_mapping(candidate.get("variant")),
        },
        required=True,
    )
    vep_state, gaps = _vep_state_and_gaps(candidate)
    if vep_state == "usable":
        return None
    return compose_shadow_annotation(
        identity_context=expected,
        vep_state=vep_state,
        field_gaps=gaps,
        fallback_evidence=_fallback_evidence_from_candidate(candidate, expected),
        evaluation_mode="gap_driven",
    )


def shadow_free_evidence_for_llm(evidence: Mapping[str, object]) -> dict[str, object]:
    """Return semantic evidence without non-evidentiary operational data."""

    def project(value: object, *, field_name: str | None = None) -> object:
        if isinstance(value, Mapping):
            if field_name == "compaction":
                return {
                    field: project(value[field], field_name=field)
                    for field in sorted(
                        VARIANT_INTERPRETATION_COMPACTION_FIELDS
                    )
                    if field in value
                }
            return {
                key: project(item, field_name=key)
                for key, item in value.items()
                if key
                not in VARIANT_INTERPRETATION_PROHIBITED_EVIDENCE_FIELDS
            }
        if isinstance(value, list):
            return [project(item, field_name=field_name) for item in value]
        return deepcopy(value)

    result = project(evidence)
    assert isinstance(result, dict)
    result.pop("shadow_composition", None)
    result.pop("annotation_promotion", None)
    return result


def compare_shadow_to_vep(
    shadow: Mapping[str, object],
    vep_comparator: Mapping[str, object],
) -> list[dict[str, object]]:
    """Compare a frozen shadow candidate with VEP only after composition.

    No heuristic equivalence is implemented in Stage 6A.  Exact equality is
    `MATCH`; all unresolved non-identical assertions remain `CONFLICT`.
    """

    validated = validate_shadow_composition(shadow)
    rows: list[dict[str, object]] = []
    for field in FIELD_NAMES:
        result = validated["fields"][field]
        candidate = result["candidate"]
        comparator = vep_comparator.get(field)
        decision = result["decision"]
        if decision == "COMPOSABLE":
            classification = "MATCH" if candidate == comparator else "CONFLICT"
            reason = (
                "exact_value_match"
                if classification == "MATCH"
                else "no_preexisting_equivalence_rule"
            )
        elif decision in {"CONFLICT", "REJECTED_UNSAFE", "NOT_COMPOSED"}:
            classification = decision
            reason = (
                result["rejection_reasons"][0]
                if result["rejection_reasons"]
                else "shadow_field_not_composable"
            )
        else:
            classification = "UNAVAILABLE"
            reason = "shadow_candidate_unavailable"
        rows.append(
            {
                "field": field,
                "shadow_candidate": candidate,
                "vep_comparator": deepcopy(comparator),
                "classification": classification,
                "reason": reason,
            }
        )
    return rows


def validate_shadow_composition(value: object) -> ShadowComposition:
    """Validate the bounded, additive Stage 6A diagnostic contract."""

    item = _mapping(value)
    required = {
        "schema_version",
        "vep_state",
        "evaluation_mode",
        "identity_context",
        "field_gaps",
        "fields",
    }
    if set(item) != required:
        raise ShadowCompositionError("Shadow composition fields are invalid.")
    if item.get("schema_version") != SHADOW_COMPOSITION_SCHEMA_VERSION:
        raise ShadowCompositionError("Shadow composition version is unsupported.")
    if item.get("vep_state") not in VEP_STATES:
        raise ShadowCompositionError("Shadow composition VEP state is invalid.")
    if item.get("evaluation_mode") not in EVALUATION_MODES:
        raise ShadowCompositionError("Shadow composition mode is invalid.")
    expected = _identity(item.get("identity_context"), required=True)
    gaps = _mapping(item.get("field_gaps"))
    fields = _mapping(item.get("fields"))
    if set(gaps) != set(GAP_FIELDS) or any(
        not isinstance(gaps.get(field), bool) for field in GAP_FIELDS
    ):
        raise ShadowCompositionError("Shadow composition field gaps are invalid.")
    if set(fields) != set(FIELD_NAMES):
        raise ShadowCompositionError("Shadow composition field results are invalid.")
    clean_fields: dict[str, dict[str, object]] = {}
    for name in FIELD_NAMES:
        field = _mapping(fields[name])
        required_field = {
            "candidate",
            "source",
            "identity_proof",
            "transcript_context",
            "semantic_validation",
            "decision",
            "rejection_reasons",
        }
        if set(field) != required_field or field.get("decision") not in COMPOSITION_DECISIONS:
            raise ShadowCompositionError("Shadow composition field decision is invalid.")
        candidate = field.get("candidate")
        if candidate is not None and not isinstance(candidate, (str, bool)):
            raise ShadowCompositionError("Shadow composition candidate is invalid.")
        for text_field in ("source", "transcript_context", "semantic_validation"):
            if field.get(text_field) is not None and _text(field.get(text_field)) is None:
                raise ShadowCompositionError("Shadow composition text is invalid.")
        reasons = field.get("rejection_reasons")
        if (
            not isinstance(reasons, list)
            or len(reasons) > 20
            or any(_text(reason) is None for reason in reasons)
            or len(set(reasons)) != len(reasons)
        ):
            raise ShadowCompositionError("Shadow composition reasons are invalid.")
        proof = field.get("identity_proof")
        if proof is not None:
            proof_map = _mapping(proof)
            if set(proof_map) != {"decision", "expected", "observed"} or proof_map.get("decision") not in IDENTITY_DECISIONS:
                raise ShadowCompositionError("Shadow composition identity proof is invalid.")
            if _identity(proof_map.get("expected"), required=True) != expected:
                raise ShadowCompositionError("Shadow composition identity proof changed input identity.")
            observed = proof_map.get("observed")
            if observed is not None:
                _identity(observed, required=True)
        clean_fields[name] = deepcopy(dict(field))
    return {
        "schema_version": SHADOW_COMPOSITION_SCHEMA_VERSION,
        "vep_state": item["vep_state"],
        "evaluation_mode": item["evaluation_mode"],
        "identity_context": deepcopy(expected),
        "field_gaps": {field: bool(gaps[field]) for field in GAP_FIELDS},
        "fields": clean_fields,
    }


__all__ = [
    "COMPOSITION_DECISIONS",
    "FIELD_NAMES",
    "GAP_FIELDS",
    "RECOGNIZED_SO_CONSEQUENCES",
    "SHADOW_COMPOSITION_SCHEMA_VERSION",
    "ShadowCompositionError",
    "build_shadow_composition",
    "compare_shadow_to_vep",
    "compose_shadow_annotation",
    "shadow_free_evidence_for_llm",
    "validate_shadow_composition",
]
