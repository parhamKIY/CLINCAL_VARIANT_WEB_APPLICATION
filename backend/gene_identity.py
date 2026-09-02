"""Deterministic, provenance-backed gene identity resolution.

This module selects only from already normalized provider outputs.  It does
not call providers and it never treats cross-check sources as authoritative.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from typing import Any

from backend.variant_identity import normalize_chromosome


GENE_IDENTITY_RESOLUTION_SCHEMA_VERSION = "1.0"
GENE_IDENTITY_UNRESOLVED = "gene_identity_unresolved"
VEP_GENE_CONTEXT_UNRESOLVED = "vep_gene_context_unresolved"

_RESOLUTION_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "source",
        "reason",
        "gene",
        "gene_id",
        "transcript",
        "hgvs_c",
        "hgvs_p",
        "consequence",
        "identity_proof",
        "provenance",
        "missing_context_fields",
        "cross_checks",
    }
)
_CONTEXT_FIELDS = frozenset(
    {"gene", "gene_id", "transcript", "hgvs_c", "hgvs_p", "consequence"}
)
_RESOLUTION_REASONS = {
    "Ensembl VEP": frozenset(
        {
            "vep_primary_complete",
            "vep_primary_gene_resolved_with_limited_context",
            "vep_primary_verified_by_variantvalidator",
            "vep_primary_verified_by_genebe",
        }
    ),
    "VariantValidator": frozenset({"variantvalidator_exact_identity"}),
    "GeneBe": frozenset({"genebe_exact_identity_coherent_transcript"}),
}
_PROVENANCE_FIELDS = frozenset(
    {
        "provider",
        "provider_role",
        "verified_by",
        "trigger",
        "provider_version",
        "retrieved_at",
    }
)


class GeneIdentityResolutionError(ValueError):
    """Raised when a gene identity resolution record is malformed."""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _gene_symbol(value: object) -> str | None:
    text = _text(value)
    if (
        text is None
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", text) is None
        or re.fullmatch(r"ENSG[0-9]+(?:\.[0-9]+)?", text, re.IGNORECASE)
        or re.fullmatch(r"HGNC:[0-9]+", text, re.IGNORECASE)
    ):
        return None
    return text


def _identity(
    *,
    assembly: object,
    variant: object,
) -> dict[str, object] | None:
    item = _mapping(variant)
    chromosome = normalize_chromosome(item.get("chrom"))
    position = item.get("pos")
    reference = _text(item.get("ref"))
    alternate = _text(item.get("alt"))
    if (
        assembly not in {"GRCh37", "GRCh38"}
        or chromosome is None
        or isinstance(position, bool)
        or not isinstance(position, int)
        or position < 1
        or reference is None
        or alternate is None
    ):
        return None
    return {
        "assembly": assembly,
        "chrom": chromosome,
        "pos": position,
        "ref": reference.upper(),
        "alt": alternate.upper(),
    }


def _identity_proof(
    expected: dict[str, object],
    observed: object,
) -> dict[str, object] | None:
    item = _mapping(observed)
    normalized = _identity(
        assembly=item.get("assembly"),
        variant=item,
    )
    if normalized != expected:
        return None
    return {
        "decision": "EXACT",
        "expected": deepcopy(expected),
        "observed": normalized,
    }


def _missing_fields(
    values: Mapping[str, object],
    required: tuple[str, ...],
) -> list[str]:
    return [field for field in required if _text(values.get(field)) is None]


def _vep_values(annotation: Mapping[str, object]) -> dict[str, object]:
    return {
        "gene": annotation.get("gene"),
        "gene_id": annotation.get("gene_id"),
        "transcript": annotation.get("transcript"),
        "hgvs_c": annotation.get("hgvsc"),
        "hgvs_p": annotation.get("hgvsp") or annotation.get("protein_change"),
        "consequence": annotation.get("consequence"),
    }


def _merge_vep_context(
    annotation: Mapping[str, object],
    supporting: Mapping[str, object],
) -> dict[str, object]:
    direct = _vep_values(annotation)
    return {
        field: direct.get(field) or supporting.get(field)
        for field in _CONTEXT_FIELDS
    }


def vep_gene_context_is_complete(annotation: Mapping[str, object]) -> bool:
    """Return whether direct VEP supplied the minimum interpretation context."""

    sources = _mapping(annotation.get("sources"))
    vep = _mapping(sources.get("vep"))
    values = {
        "gene": annotation.get("gene"),
        "transcript": annotation.get("transcript"),
        "hgvs_c": annotation.get("hgvsc"),
        "consequence": annotation.get("consequence"),
    }
    return (
        vep.get("status") == "success"
        and _text(vep.get("provider")) == "Ensembl VEP"
        and _gene_symbol(annotation.get("gene")) is not None
        and not _missing_fields(
            values,
            ("gene", "transcript", "hgvs_c", "consequence"),
        )
    )


def _cross_check(
    value: object,
    selected_gene: str | None,
) -> dict[str, object]:
    gene = _gene_symbol(value)
    if gene is None:
        agreement = "unavailable"
    elif selected_gene is None:
        agreement = "not_compared"
    elif gene.casefold() == selected_gene.casefold():
        agreement = "agrees"
    else:
        agreement = "conflicts"
    return {"gene": gene, "agreement": agreement}


def _resolution(
    *,
    expected: dict[str, object],
    source: str | None,
    reason: str,
    values: Mapping[str, object],
    identity_proof: dict[str, object] | None,
    provenance: Mapping[str, object] | None,
    missing_context_fields: list[str],
    myvariant_gene: object,
    clinvar_gene: object,
) -> dict[str, Any]:
    gene = _gene_symbol(values.get("gene"))
    status = "resolved" if source is not None and gene is not None else "unresolved"
    selected_gene = gene if status == "resolved" else None
    return {
        "schema_version": GENE_IDENTITY_RESOLUTION_SCHEMA_VERSION,
        "status": status,
        "source": source if status == "resolved" else None,
        "reason": reason,
        "gene": selected_gene,
        "gene_id": _text(values.get("gene_id")) if status == "resolved" else None,
        "transcript": _text(values.get("transcript")) if status == "resolved" else None,
        "hgvs_c": _text(values.get("hgvs_c")) if status == "resolved" else None,
        "hgvs_p": _text(values.get("hgvs_p")) if status == "resolved" else None,
        "consequence": (
            _text(values.get("consequence")) if status == "resolved" else None
        ),
        "identity_proof": deepcopy(identity_proof) if status == "resolved" else None,
        "provenance": deepcopy(dict(provenance or {})) if status == "resolved" else {},
        "missing_context_fields": list(missing_context_fields),
        "cross_checks": {
            "myvariant": _cross_check(myvariant_gene, selected_gene),
            "clinvar": _cross_check(clinvar_gene, selected_gene),
        },
    }


def _variantvalidator_candidate(
    annotation: Mapping[str, object],
    expected: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], Mapping[str, object]] | None:
    sources = _mapping(annotation.get("sources"))
    vep = _mapping(sources.get("vep"))
    verification = _mapping(vep.get("gene_identity_verification"))
    if verification.get("status") == "success":
        payload = verification
        observed = verification.get("validated_variant")
    elif (
        vep.get("provider") == "VariantValidator"
        and vep.get("provider_role") == "fallback"
        and vep.get("fallback_for") == "ensembl_vep"
        and vep.get("status") in {"success", "partial"}
    ):
        payload = vep
        observed = vep.get("validated_variant") or vep.get("normalized_variant")
    else:
        return None
    proof = _identity_proof(expected, observed)
    values = {
        "gene": payload.get("validated_gene"),
        "gene_id": payload.get("validated_gene_id"),
        "transcript": payload.get("validated_transcript"),
        "hgvs_c": payload.get("validated_transcript_hgvs"),
        "hgvs_p": payload.get("validated_protein_hgvs"),
        "consequence": None,
    }
    if (
        proof is None
        or _gene_symbol(values.get("gene")) is None
    ):
        return None
    provenance = {
        "provider": "VariantValidator",
        "provider_role": (
            "verification" if payload is verification else "fallback"
        ),
        "trigger": payload.get("trigger") or vep.get("primary_failure"),
        "provider_version": payload.get("provider_version"),
        "retrieved_at": payload.get("retrieved_at"),
    }
    return values, proof, provenance


def _genebe_candidate(
    annotation: Mapping[str, object],
    expected: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], Mapping[str, object]] | None:
    sources = _mapping(annotation.get("sources"))
    genebe = _mapping(sources.get("genebe"))
    observed = {
        "assembly": genebe.get("request_assembly"),
        **dict(_mapping(genebe.get("returned_variant"))),
    }
    proof = _identity_proof(expected, observed)
    records = genebe.get("consequences")
    if (
        genebe.get("status") != "success"
        or _text(genebe.get("provider")) != "GeneBe"
        or genebe.get("representation_mismatch") is True
        or proof is None
        or not isinstance(records, list)
    ):
        return None
    source_gene = _gene_symbol(genebe.get("gene"))
    source_transcript = _text(genebe.get("transcript"))
    coherent: list[tuple[tuple[int, int, int, str], Mapping[str, object]]] = []
    for raw_record in records:
        record = _mapping(raw_record)
        gene = _gene_symbol(record.get("gene")) or source_gene
        transcript = _text(record.get("transcript"))
        hgvs_c = _text(record.get("hgvs_c"))
        if gene is None or transcript is None or hgvs_c is None:
            continue
        if source_gene is not None and gene.casefold() != source_gene.casefold():
            continue
        if source_transcript is not None and transcript != source_transcript:
            continue
        if not (hgvs_c.startswith(f"{transcript}:") or hgvs_c.startswith("c.")):
            continue
        coherent.append(
            (
                (
                    0 if record.get("canonical") is True else 1,
                    0 if record.get("mane_select") else 1,
                    0 if record.get("protein_coding") is True else 1,
                    transcript,
                ),
                record,
            )
        )
    if not coherent:
        return None
    coherent.sort(key=lambda item: item[0])
    selected = coherent[0][1]
    terms = selected.get("consequence_terms")
    consequence = (
        next((item for item in terms if _text(item) is not None), None)
        if isinstance(terms, list)
        else None
    )
    values = {
        "gene": _gene_symbol(selected.get("gene")) or source_gene,
        "gene_id": (
            f"HGNC:{genebe.get('gene_hgnc_id')}"
            if isinstance(genebe.get("gene_hgnc_id"), int)
            and not isinstance(genebe.get("gene_hgnc_id"), bool)
            else None
        ),
        "transcript": selected.get("transcript"),
        "hgvs_c": selected.get("hgvs_c"),
        "hgvs_p": selected.get("hgvs_p"),
        "consequence": consequence or genebe.get("effect"),
    }
    provenance = {
        "provider": "GeneBe",
        "provider_role": "supporting_resolver",
        "trigger": VEP_GENE_CONTEXT_UNRESOLVED,
        "provider_version": genebe.get("provider_version"),
        "retrieved_at": genebe.get("retrieved_at"),
    }
    return values, proof, provenance


def resolve_gene_identity(annotation: Mapping[str, object]) -> dict[str, Any]:
    """Resolve one gene identity using the approved deterministic precedence."""

    expected = _identity(
        assembly=annotation.get("assembly"),
        variant=annotation.get("variant"),
    )
    if expected is None:
        raise GeneIdentityResolutionError("Canonical variant identity is invalid.")
    sources = _mapping(annotation.get("sources"))
    vep = _mapping(sources.get("vep"))
    myvariant = _mapping(sources.get("myvariant"))
    clinvar = _mapping(sources.get("clinvar"))

    if vep_gene_context_is_complete(annotation):
        values = _vep_values(annotation)
        proof = _identity_proof(expected, vep.get("normalized_variant"))
        if proof is not None:
            return _resolution(
                expected=expected,
                source="Ensembl VEP",
                reason="vep_primary_complete",
                values=values,
                identity_proof=proof,
                provenance={
                    "provider": "Ensembl VEP",
                    "provider_role": "primary",
                    "trigger": None,
                    "provider_version": vep.get("provider_version"),
                    "retrieved_at": vep.get("retrieved_at"),
                },
                missing_context_fields=_missing_fields(
                    values,
                    (
                        "gene_id",
                        "transcript",
                        "hgvs_c",
                        "hgvs_p",
                        "consequence",
                    ),
                ),
                myvariant_gene=myvariant.get("gene"),
                clinvar_gene=clinvar.get("gene"),
            )

    variantvalidator = _variantvalidator_candidate(annotation, expected)
    direct_vep = (
        vep.get("status") == "success"
        and _text(vep.get("provider")) == "Ensembl VEP"
        and _identity_proof(expected, vep.get("normalized_variant")) is not None
    )
    vep_gene = (
        _gene_symbol(annotation.get("gene")) if direct_vep else None
    )
    authoritative_conflict = False
    if variantvalidator is not None:
        values, proof, provenance = variantvalidator
        resolved_gene = _gene_symbol(values.get("gene"))
        if (
            vep_gene is not None
            and resolved_gene is not None
            and vep_gene.casefold() != resolved_gene.casefold()
        ):
            authoritative_conflict = True
        elif vep_gene is not None:
            values = _merge_vep_context(annotation, values)
            return _resolution(
                expected=expected,
                source="Ensembl VEP",
                reason="vep_primary_verified_by_variantvalidator",
                values=values,
                identity_proof=proof,
                provenance={
                    "provider": "Ensembl VEP",
                    "provider_role": "primary",
                    "verified_by": "VariantValidator",
                    "trigger": VEP_GENE_CONTEXT_UNRESOLVED,
                    "provider_version": vep.get("provider_version"),
                    "retrieved_at": vep.get("retrieved_at"),
                },
                missing_context_fields=_missing_fields(
                    values,
                    (
                        "gene_id",
                        "transcript",
                        "hgvs_c",
                        "hgvs_p",
                        "consequence",
                    ),
                ),
                myvariant_gene=myvariant.get("gene"),
                clinvar_gene=clinvar.get("gene"),
            )
        else:
            return _resolution(
                expected=expected,
                source="VariantValidator",
                reason="variantvalidator_exact_identity",
                values=values,
                identity_proof=proof,
                provenance=provenance,
                missing_context_fields=_missing_fields(
                    values,
                    (
                        "gene_id",
                        "transcript",
                        "hgvs_c",
                        "hgvs_p",
                        "consequence",
                    ),
                ),
                myvariant_gene=myvariant.get("gene"),
                clinvar_gene=clinvar.get("gene"),
            )

    genebe = _genebe_candidate(annotation, expected)
    if genebe is not None and not authoritative_conflict:
        values, proof, provenance = genebe
        resolved_gene = _gene_symbol(values.get("gene"))
        if (
            vep_gene is not None
            and resolved_gene is not None
            and vep_gene.casefold() != resolved_gene.casefold()
        ):
            authoritative_conflict = True
        elif vep_gene is not None:
            values = _merge_vep_context(annotation, values)
            return _resolution(
                expected=expected,
                source="Ensembl VEP",
                reason="vep_primary_verified_by_genebe",
                values=values,
                identity_proof=proof,
                provenance={
                    "provider": "Ensembl VEP",
                    "provider_role": "primary",
                    "verified_by": "GeneBe",
                    "trigger": VEP_GENE_CONTEXT_UNRESOLVED,
                    "provider_version": vep.get("provider_version"),
                    "retrieved_at": vep.get("retrieved_at"),
                },
                missing_context_fields=_missing_fields(
                    values,
                    (
                        "gene_id",
                        "transcript",
                        "hgvs_c",
                        "hgvs_p",
                        "consequence",
                    ),
                ),
                myvariant_gene=myvariant.get("gene"),
                clinvar_gene=clinvar.get("gene"),
            )
        else:
            return _resolution(
                expected=expected,
                source="GeneBe",
                reason="genebe_exact_identity_coherent_transcript",
                values=values,
                identity_proof=proof,
                provenance=provenance,
                missing_context_fields=_missing_fields(
                    values,
                    (
                        "gene_id",
                        "transcript",
                        "hgvs_c",
                        "hgvs_p",
                        "consequence",
                    ),
                ),
                myvariant_gene=myvariant.get("gene"),
                clinvar_gene=clinvar.get("gene"),
            )

    if vep_gene is not None and not authoritative_conflict:
        values = _vep_values(annotation)
        proof = _identity_proof(expected, vep.get("normalized_variant"))
        if proof is not None:
            return _resolution(
                expected=expected,
                source="Ensembl VEP",
                reason="vep_primary_gene_resolved_with_limited_context",
                values=values,
                identity_proof=proof,
                provenance={
                    "provider": "Ensembl VEP",
                    "provider_role": "primary",
                    "trigger": VEP_GENE_CONTEXT_UNRESOLVED,
                    "provider_version": vep.get("provider_version"),
                    "retrieved_at": vep.get("retrieved_at"),
                },
                missing_context_fields=_missing_fields(
                    values,
                    (
                        "gene_id",
                        "transcript",
                        "hgvs_c",
                        "hgvs_p",
                        "consequence",
                    ),
                ),
                myvariant_gene=myvariant.get("gene"),
                clinvar_gene=clinvar.get("gene"),
            )

    vep_values = {
        "gene": annotation.get("gene"),
        "transcript": annotation.get("transcript"),
        "hgvs_c": annotation.get("hgvsc"),
        "consequence": annotation.get("consequence"),
    }
    return _resolution(
        expected=expected,
        source=None,
        reason=GENE_IDENTITY_UNRESOLVED,
        values={},
        identity_proof=None,
        provenance=None,
        missing_context_fields=_missing_fields(
            vep_values,
            ("gene", "transcript", "hgvs_c", "consequence"),
        ),
        myvariant_gene=myvariant.get("gene"),
        clinvar_gene=clinvar.get("gene"),
    )


def validate_gene_identity_resolution(value: object) -> dict[str, Any]:
    """Validate the bounded resolution record carried into EvidenceObject context."""

    if not isinstance(value, Mapping) or set(value) != _RESOLUTION_FIELDS:
        raise GeneIdentityResolutionError("Gene identity resolution fields are invalid.")
    item = deepcopy(dict(value))
    if item["schema_version"] != GENE_IDENTITY_RESOLUTION_SCHEMA_VERSION:
        raise GeneIdentityResolutionError("Gene identity resolution version is unsupported.")
    if item["status"] not in {"resolved", "unresolved"}:
        raise GeneIdentityResolutionError("Gene identity resolution status is invalid.")
    if not isinstance(item["missing_context_fields"], list) or any(
        field not in _CONTEXT_FIELDS for field in item["missing_context_fields"]
    ) or len(item["missing_context_fields"]) != len(
        set(item["missing_context_fields"])
    ):
        raise GeneIdentityResolutionError("Missing-context fields are invalid.")
    cross_checks = _mapping(item["cross_checks"])
    if set(cross_checks) != {"myvariant", "clinvar"}:
        raise GeneIdentityResolutionError("Gene identity cross-checks are invalid.")
    for cross_check in cross_checks.values():
        record = _mapping(cross_check)
        if set(record) != {"gene", "agreement"} or record.get("agreement") not in {
            "unavailable",
            "not_compared",
            "agrees",
            "conflicts",
        }:
            raise GeneIdentityResolutionError("Gene identity cross-check is invalid.")
        if record.get("gene") is not None and _gene_symbol(record.get("gene")) is None:
            raise GeneIdentityResolutionError("Gene identity cross-check gene is invalid.")
    if item["status"] == "unresolved":
        if (
            item["source"] is not None
            or item["gene"] is not None
            or item["identity_proof"] is not None
            or item["provenance"] != {}
            or item["reason"] != GENE_IDENTITY_UNRESOLVED
        ):
            raise GeneIdentityResolutionError("Unresolved gene identity state is invalid.")
        return item
    source = _text(item["source"])
    gene = _gene_symbol(item["gene"])
    proof = _mapping(item["identity_proof"])
    provenance = _mapping(item["provenance"])
    expected = _mapping(proof.get("expected"))
    observed = _mapping(proof.get("observed"))
    expected_identity = _identity(
        assembly=expected.get("assembly"),
        variant=expected,
    )
    if (
        source not in {"Ensembl VEP", "VariantValidator", "GeneBe"}
        or gene is None
        or item.get("reason") not in _RESOLUTION_REASONS[source]
        or proof.get("decision") != "EXACT"
        or set(proof) != {"decision", "expected", "observed"}
        or expected_identity is None
        or expected != expected_identity
        or observed != expected_identity
        or not set(provenance).issubset(_PROVENANCE_FIELDS)
        or _text(provenance.get("provider")) != source
        or provenance.get("provider_role")
        not in {"primary", "fallback", "verification", "supporting_resolver"}
    ):
        raise GeneIdentityResolutionError("Resolved gene identity state is invalid.")
    for field in ("gene_id", "transcript", "hgvs_c", "hgvs_p", "consequence"):
        if item[field] is not None and _text(item[field]) is None:
            raise GeneIdentityResolutionError("Resolved gene context is invalid.")
    verified_by = provenance.get("verified_by")
    if verified_by is not None and verified_by not in {"VariantValidator", "GeneBe"}:
        raise GeneIdentityResolutionError("Gene identity verifier is invalid.")
    return item


def evidence_has_resolved_gene_identity(evidence: Mapping[str, object]) -> bool:
    """Check the current resolver contract with historical-record compatibility."""

    gene = _gene_symbol(evidence.get("gene"))
    variant_context = _mapping(evidence.get("variant_context"))
    if gene is None or _text(variant_context.get("gene")) != gene:
        return False
    annotations = _mapping(evidence.get("annotations"))
    vep = _mapping(annotations.get("vep"))
    raw_resolution = vep.get("gene_identity_resolution")
    if raw_resolution is not None:
        try:
            resolution = validate_gene_identity_resolution(raw_resolution)
        except GeneIdentityResolutionError:
            return False
        expected = _identity(
            assembly=evidence.get("assembly"),
            variant=evidence.get("variant"),
        )
        proof = _mapping(resolution.get("identity_proof"))
        return (
            resolution["status"] == "resolved"
            and resolution["gene"] == gene
            and expected is not None
            and _mapping(proof.get("expected")) == expected
            and _mapping(proof.get("observed")) == expected
        )

    promotion = _mapping(evidence.get("annotation_promotion"))
    promoted_gene = _mapping(_mapping(promotion.get("fields")).get("gene"))
    proof = _mapping(promoted_gene.get("identity_proof"))
    if (
        promoted_gene.get("active_value") == gene
        and promoted_gene.get("source") in {"VariantValidator", "GeneBe"}
        and proof.get("decision") == "EXACT"
    ):
        return True

    # Historical EvidenceObjects predate the explicit resolver record.  They
    # remain usable only when their existing VEP and provider provenance agree.
    if vep.get("status") != "success" or _text(vep.get("gene")) != gene:
        return False
    provenance = _mapping(evidence.get("provenance"))
    providers = provenance.get("providers")
    return isinstance(providers, list) and any(
        _mapping(record).get("source") == "vep"
        and _mapping(record).get("provider") == "Ensembl VEP"
        and _mapping(record).get("status") == "success"
        for record in providers
    )


__all__ = [
    "GENE_IDENTITY_RESOLUTION_SCHEMA_VERSION",
    "GENE_IDENTITY_UNRESOLVED",
    "GeneIdentityResolutionError",
    "VEP_GENE_CONTEXT_UNRESOLVED",
    "evidence_has_resolved_gene_identity",
    "resolve_gene_identity",
    "validate_gene_identity_resolution",
    "vep_gene_context_is_complete",
]
