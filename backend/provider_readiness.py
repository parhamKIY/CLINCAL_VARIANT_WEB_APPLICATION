"""Provider-readiness catalog shared by pre-analysis health checks.

The catalog describes operational connectivity only.  It deliberately does
not change evidence collection, fallback activation, or clinical provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from config import settings


ProbeKind = Literal["transport", "authenticated"]
CredentialMode = Literal["none", "optional", "required"]


@dataclass(frozen=True)
class ProviderReadinessTarget:
    """One configured source shown by the future pre-analysis readiness UI."""

    provider: str
    label: str
    url: str
    capabilities: tuple[str, ...]
    fallback_for: tuple[str, ...] = ()
    credential_mode: CredentialMode = "none"
    probe_kind: ProbeKind = "transport"

    def __post_init__(self) -> None:
        if not self.provider or not self.provider.isidentifier():
            raise ValueError("Provider identifier must be a Python identifier.")
        if not self.label.strip() or not self.capabilities:
            raise ValueError("Provider label and capabilities are required.")
        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Provider readiness targets require HTTPS URLs.")
        if self.credential_mode not in {"none", "optional", "required"}:
            raise ValueError("Unsupported provider credential mode.")
        if self.probe_kind not in {"transport", "authenticated"}:
            raise ValueError("Unsupported provider probe kind.")


FALLBACK_CHAINS: dict[str, tuple[str, ...]] = {
    "variant_annotation": ("vep", "variantvalidator"),
    "variant_context": ("myvariant", "ensembl_variation"),
    "clinvar_evidence": ("clinvar", "myvariant"),
    "cspec_context": ("cspec", "local_cspec_cache"),
    "phenotype_gene_context": ("phen2gene", "local_hpo_gene"),
    "disease_hpo_context": ("mydisease", "local_hpo_disease"),
    "population_frequency": ("gnomad", "ensembl_variation"),
    "variant_literature": ("litvar2", "europe_pmc", "pubmed"),
    "gene_disease_literature": ("europe_pmc", "pubmed"),
    "variant_interpretation": ("llm", "llm_fallback_model"),
}


def configured_provider_readiness_targets() -> tuple[ProviderReadinessTarget, ...]:
    """Return all network providers used by the active analysis workflow.

    Local CSpec and HPO fallbacks intentionally do not appear: they have no
    network endpoint to probe.  LLM health is marked authenticated because a
    future capability probe must validate the configured key without logging it.
    """

    return (
        ProviderReadinessTarget(
            "vep",
            "Ensembl VEP",
            settings.VEP_BASE_URL,
            ("variant_annotation",),
        ),
        ProviderReadinessTarget(
            "variantvalidator",
            "VariantValidator",
            settings.VARIANTVALIDATOR_BASE_URL,
            ("variant_annotation",),
            ("variant_annotation",),
        ),
        ProviderReadinessTarget(
            "genebe",
            "GeneBe",
            settings.GENEBE_BASE_URL,
            ("classification_context",),
            credential_mode="optional",
        ),
        ProviderReadinessTarget(
            "myvariant",
            "MyVariant.info",
            settings.MYVARIANT_BASE_URL,
            ("variant_context", "clinvar_evidence"),
            ("clinvar_evidence",),
        ),
        ProviderReadinessTarget(
            "clinvar",
            "NCBI ClinVar",
            settings.CLINVAR_BASE_URL,
            ("clinvar_evidence",),
        ),
        ProviderReadinessTarget(
            "clingen",
            "UCSC GenCC track",
            settings.CLINGEN_BASE_URL,
            ("gene_disease_validity",),
        ),
        ProviderReadinessTarget(
            "cspec",
            "ClinGen CSpec Registry",
            settings.CSPEC_BASE_URL,
            ("cspec_context",),
        ),
        ProviderReadinessTarget(
            "phen2gene",
            "Phen2Gene",
            settings.PHEN2GENE_BASE_URL,
            ("phenotype_gene_context",),
        ),
        ProviderReadinessTarget(
            "mydisease",
            "MyDisease.info",
            settings.MYDISEASE_BASE_URL,
            ("disease_hpo_context",),
        ),
        ProviderReadinessTarget(
            "gnomad",
            "gnomAD",
            settings.GNOMAD_BASE_URL,
            ("population_frequency",),
        ),
        ProviderReadinessTarget(
            "ensembl_variation",
            "Ensembl REST Variation",
            settings.ENSEMBL_VARIATION_BASE_URL,
            ("variant_context", "population_frequency"),
            ("variant_context", "population_frequency"),
        ),
        ProviderReadinessTarget(
            "litvar2",
            "LitVar2",
            settings.LITVAR_BASE_URL,
            ("variant_literature",),
        ),
        ProviderReadinessTarget(
            "europe_pmc",
            "Europe PMC",
            settings.EUROPE_PMC_BASE_URL,
            ("variant_literature", "gene_disease_literature"),
            ("variant_literature",),
        ),
        ProviderReadinessTarget(
            "pubmed",
            "PubMed",
            settings.PUBMED_BASE_URL,
            ("variant_literature", "gene_disease_literature"),
            ("variant_literature", "gene_disease_literature"),
        ),
        ProviderReadinessTarget(
            "llm",
            "Configured interpretation endpoint",
            settings.LLM_BASE_URL,
            ("phenotype_extraction", "variant_interpretation"),
            credential_mode="required",
            probe_kind="authenticated",
        ),
    )


def provider_readiness_target_map() -> dict[str, ProviderReadinessTarget]:
    """Return the catalog keyed by stable provider identifier."""

    targets = configured_provider_readiness_targets()
    result = {target.provider: target for target in targets}
    if len(result) != len(targets):
        raise ValueError("Provider readiness identifiers must be unique.")
    return result


__all__ = [
    "CredentialMode",
    "FALLBACK_CHAINS",
    "ProbeKind",
    "ProviderReadinessTarget",
    "configured_provider_readiness_targets",
    "provider_readiness_target_map",
]
