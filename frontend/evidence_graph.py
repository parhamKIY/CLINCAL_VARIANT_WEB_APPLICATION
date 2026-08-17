"""Presentation-only explorer for the approved Evidence Graph architecture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import streamlit as st


@dataclass(frozen=True)
class EvidenceGraphView:
    """One static, documentation-derived architecture view."""

    description: str
    diagram: str


@dataclass(frozen=True)
class EvidenceGraphNodeDetail:
    """Safe explanatory metadata for one graph node."""

    summary: str
    details: tuple[str, ...]


SIMPLE_VIEW = "Simple view"
ADVANCED_VIEW = "Advanced view"

EVIDENCE_GRAPH_VIEWS: Mapping[str, EvidenceGraphView] = {
    SIMPLE_VIEW: EvidenceGraphView(
        description=(
            "Providers supply distinct semantic evidence capabilities. "
            "This is not an independent-vote or simple fallback-chain model."
        ),
        diagram="""
flowchart TB
    INPUT["Input variant / patient HPO"]
    VEP["Ensembl VEP"] --> ANNOTATION["Annotation"]
    VALIDATOR["VariantValidator fallback / promotion"] -. exact validated path .-> ANNOTATION
    GENEBE["GeneBe"] --> ACMG["Automated ACMG context"]
    EREPO["ERepo"] --> EXPERT["Expert curated variant context"]
    CLINVAR["NCBI ClinVar"] --> CLINICAL["ClinVar clinical evidence"]
    MYVARIANT["MyVariant ClinVar rescue"] -. correlated ClinVar upstream .-> CLINICAL
    GENCC["GenCC"] --> VALIDITY["Gene-disease validity"]
    MEDGEN["MedGen"] --> SUPPORT["Gene-disease support"]
    PHEN2GENE["Phen2Gene"] --> RANKING["Phenotype-gene ranking"]
    MEDGEN --> PHENOTYPE_SUPPORT["Phenotype-gene support"]
    MYDISEASE["MyDisease"] --> DISEASE_HPO["Disease/HPO context"]
    MEDGEN --> DISEASE_HPO
    POPULATION_ROUTES["Approved population routes"] --> POPULATION["Population evidence"]
    LITVAR["LitVar2"] --> EUROPE_PMC["Europe PMC"] --> PUBMED["PubMed"] --> LITERATURE["Literature evidence"]
    INPUT --> ANNOTATION
    INPUT --> RANKING
    ANNOTATION --> SEMANTIC["Semantic evidence"]
    ACMG --> SEMANTIC
    EXPERT --> SEMANTIC
    CLINICAL --> SEMANTIC
    VALIDITY --> SEMANTIC
    SUPPORT --> SEMANTIC
    RANKING --> SEMANTIC
    PHENOTYPE_SUPPORT --> SEMANTIC
    DISEASE_HPO --> SEMANTIC
    POPULATION --> SEMANTIC
    LITERATURE --> SEMANTIC
    SEMANTIC --> COVERAGE["Stage 7 coverage"] --> DISPOSITION["Stage 8 final disposition"] --> LLM["Sanitized evidence for one selected LLM"] --> HUMAN["Mandatory human review"]
    classDef provider fill:#eef4ff,stroke:#4f6fae,color:#102a43;
    classDef capability fill:#ecfdf3,stroke:#227a52,color:#123524;
    classDef boundary fill:#fff7e6,stroke:#a15c00,color:#4b2e00;
    class VEP,VALIDATOR,GENEBE,EREPO,CLINVAR,MYVARIANT,GENCC,MEDGEN,PHEN2GENE,MYDISEASE,POPULATION_ROUTES,LITVAR,EUROPE_PMC,PUBMED provider;
    class ANNOTATION,ACMG,EXPERT,CLINICAL,VALIDITY,SUPPORT,RANKING,PHENOTYPE_SUPPORT,DISEASE_HPO,POPULATION,LITERATURE,SEMANTIC capability;
    class COVERAGE,DISPOSITION,LLM,HUMAN boundary;
""",
    ),
    ADVANCED_VIEW: EvidenceGraphView(
        description=(
            "Advanced view preserves the approved provider roles, correlation, "
            "Stage 6 promotion boundary, and LLM sanitization boundary."
        ),
        diagram="""
flowchart TB
    INPUT["Input: exact assembly / CPRA plus accepted HPO"]
    subgraph ANNOTATION_PATH["Annotation — Stage 6 controlled path"]
        VEP["Ensembl VEP preferred"] --> ANN["Annotation semantic capability"]
        VV["VariantValidator exact fallback"] -. operational VEP failure only .-> PROMOTABLE["Promotable: gene, transcript, HGVS.c, HGVS.p"]
        GENEBE["GeneBe supporting record"] -. exact source / transcript proof .-> PROMOTABLE
        PROMOTABLE -. approved promotion only .-> ANN
        NOT_PROMOTED["Not actively promoted: consequence, impact, MANE, canonical"]
    end
    subgraph CLINICAL_PATH["Clinical evidence and shared upstream"]
        NCBI["NCBI ClinVar direct"] --> CLINVAR_NODE["ClinVar clinical evidence"]
        MV["MyVariant ClinVar-derived rescue"] -. correlated rescue, not independent vote .-> CLINVAR_NODE
    end
    subgraph GENE_DISEASE_PATH["Gene-disease semantics remain separated"]
        GENCC["GenCC validity evidence"] --> VALIDITY["Gene-disease validity"]
        MEDGEN_GD["MedGen supporting context"] --> SUPPORT["Gene-disease support"]
    end
    subgraph PHENOTYPE_PATH["Phenotype semantics remain separated"]
        P2G["Phen2Gene rank / score"] --> RANK["Phenotype-gene ranking"]
        MEDGEN_PG["MedGen / local support"] --> P_SUPPORT["Phenotype-gene support"]
        MYD["MyDisease plus MedGen"] --> DHPO["Disease/HPO context"]
    end
    EREPO["ERepo"] --> EXPERT["Expert curated variant context"]
    GENEBE --> ACMG["Automated ACMG context"]
    POP["Approved population routes with shared-upstream tracking"] --> POPULATION["Population evidence"]
    LITVAR["LitVar2"] --> PMC["Europe PMC"] --> PUBMED["PubMed"] --> LITERATURE["Literature evidence"]
    INPUT --> ANN
    INPUT --> RANK
    ANN --> SEMANTIC["Normalized semantic evidence"]
    ACMG --> SEMANTIC
    EXPERT --> SEMANTIC
    CLINVAR_NODE --> SEMANTIC
    VALIDITY --> SEMANTIC
    SUPPORT --> SEMANTIC
    RANK --> SEMANTIC
    P_SUPPORT --> SEMANTIC
    DHPO --> SEMANTIC
    POPULATION --> SEMANTIC
    LITERATURE --> SEMANTIC
    SEMANTIC --> STAGE7["Stage 7 coverage: FULL / DEGRADED / UNAVAILABLE / NOT_TRIGGERED / NOT_APPLICABLE"]
    STAGE7 --> STAGE8["Stage 8 final disposition"]
    STAGE8 --> SANITIZED["Sanitized semantic evidence only"] --> LLM["One selected interpretation LLM"] --> REVIEW["Mandatory human review"]
    EXCLUDED["Excluded from LLM: raw provider payloads, raw VCF/genotypes, shadow composition, annotation promotion, rejected candidate diagnostics"] -. sanitization boundary .-> SANITIZED
    classDef provider fill:#eef4ff,stroke:#4f6fae,color:#102a43;
    classDef capability fill:#ecfdf3,stroke:#227a52,color:#123524;
    classDef caution fill:#fff7e6,stroke:#a15c00,color:#4b2e00;
    class VEP,VV,GENEBE,NCBI,MV,GENCC,MEDGEN_GD,P2G,MEDGEN_PG,MYD,EREPO,POP,LITVAR,PMC,PUBMED provider;
    class ANN,CLINVAR_NODE,VALIDITY,SUPPORT,RANK,P_SUPPORT,DHPO,EXPERT,ACMG,POPULATION,LITERATURE,SEMANTIC capability;
    class PROMOTABLE,NOT_PROMOTED,STAGE7,STAGE8,SANITIZED,LLM,REVIEW,EXCLUDED caution;
""",
    ),
}

EVIDENCE_GRAPH_NODE_DETAILS: Mapping[str, EvidenceGraphNodeDetail] = {
    "Annotation": EvidenceGraphNodeDetail(
        summary="Exact annotation remains Ensembl VEP-preferred.",
        details=(
            "Validated fallback: VariantValidator under the approved controlled path.",
            "Promotable fields: gene, transcript, HGVS.c, HGVS.p.",
            "Not actively promoted: consequence, impact, MANE, canonical.",
        ),
    ),
    "ClinVar clinical evidence": EvidenceGraphNodeDetail(
        summary="NCBI ClinVar is direct clinical evidence.",
        details=(
            "MyVariant-derived ClinVar is a correlated rescue, not an independent vote.",
            "Valid no-match is missingness, not an operational failure or negative evidence.",
        ),
    ),
    "Gene-disease validity": EvidenceGraphNodeDetail(
        summary="GenCC supplies the validity-classification semantic node.",
        details=(
            "MedGen support does not become a GenCC validity classification.",
            "Operational availability and semantic capability remain distinct.",
        ),
    ),
    "Phenotype-gene ranking": EvidenceGraphNodeDetail(
        summary="Phen2Gene supplies the ranking semantic node.",
        details=(
            "MedGen/local support never creates a Phen2Gene rank or score.",
            "Accepted HPO terms remain input context, not provider availability.",
        ),
    ),
    "Stage 7 coverage": EvidenceGraphNodeDetail(
        summary="Coverage is a runtime-only, provider-neutral capability projection.",
        details=(
            "It references normalized evidence paths and preserves retrieval and correlation context.",
            "This viewer does not calculate, persist, or overlay coverage state.",
        ),
    ),
    "Sanitized evidence for one selected LLM": EvidenceGraphNodeDetail(
        summary="One selected model receives sanitized normalized semantic evidence.",
        details=(
            "Raw provider payloads, raw VCF/genotypes, shadow composition, annotation promotion, and rejected candidate diagnostics are excluded.",
            "The LLM does not replace mandatory human review.",
        ),
    ),
    "Mandatory human review": EvidenceGraphNodeDetail(
        summary="Human review, editing, selection, and confirmation remain mandatory.",
        details=(
            "The application is decision support, not autonomous diagnosis or classification.",
        ),
    ),
}


def _render_evidence_graph_contents() -> None:
    """Render static architecture metadata without provider or state access."""

    view_name = st.segmented_control(
        "Graph view",
        tuple(EVIDENCE_GRAPH_VIEWS),
        default=SIMPLE_VIEW,
        key="evidence_graph_view",
        width="stretch",
    )
    selected_view = EVIDENCE_GRAPH_VIEWS.get(
        view_name,
        EVIDENCE_GRAPH_VIEWS[SIMPLE_VIEW],
    )
    st.caption(selected_view.description)
    st.mermaid_chart(selected_view.diagram, width="stretch")
    selected_node = st.selectbox(
        "Explore a semantic node",
        tuple(EVIDENCE_GRAPH_NODE_DETAILS),
        key="evidence_graph_node",
    )
    detail = EVIDENCE_GRAPH_NODE_DETAILS[selected_node]
    with st.container(border=True):
        st.markdown(f"**{selected_node}**")
        st.caption(detail.summary)
        for item in detail.details:
            st.caption(item)


@st.dialog(
    "Evidence Graph",
    width="large",
    icon=":material/account_tree:",
)
def render_evidence_graph() -> None:
    """Open the static architecture explorer without provider or state access."""

    _render_evidence_graph_contents()


__all__ = [
    "ADVANCED_VIEW",
    "EVIDENCE_GRAPH_NODE_DETAILS",
    "EVIDENCE_GRAPH_VIEWS",
    "SIMPLE_VIEW",
    "render_evidence_graph",
]
