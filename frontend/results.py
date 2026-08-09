"""Streamlit views for frontend-safe pipeline result collections."""

from __future__ import annotations

import streamlit as st

from backend.fallback_transparency import build_fallback_notices
from backend.pipeline import PipelineResult
from backend.references import build_canonical_references


SOURCE_LABELS = {
    "vep": "VEP",
    "genebe": "GeneBe",
    "myvariant": "MyVariant.info",
    "clinvar": "ClinVar",
    "clingen": "ClinGen/GenCC",
}


def _dictionary(value: object) -> dict[str, object]:
    """Return a mapping or an empty fallback."""

    if isinstance(value, dict):
        return value
    return {}


def _variant(item: dict[str, object]) -> dict[str, object]:
    """Return a nested or direct standardized variant."""

    nested = item.get("variant")
    if isinstance(nested, dict):
        return nested
    return item


def _variant_label(item: dict[str, object]) -> str:
    """Build a compact allele label from one result item."""

    variant = _variant(item)
    values = (
        variant.get("chrom"),
        variant.get("pos"),
        variant.get("ref"),
        variant.get("alt"),
    )
    if any(value is None for value in values):
        return "Variant unavailable"
    return ":".join(str(value) for value in values)


def _joined_text(value: object) -> str | None:
    """Flatten a visible string list for tabular display."""

    if not isinstance(value, list):
        return None
    items = [str(item) for item in value if str(item).strip()]
    return ", ".join(items) if items else None


def build_variant_rows(
    variants: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return the complete genotype-free filtered input table."""

    rows: list[dict[str, object]] = []
    for item in variants:
        variant = _variant(item)
        rows.append(
            {
                "Variant": _variant_label(item),
                "Chromosome": variant.get("chrom"),
                "Position": variant.get("pos"),
                "Reference": variant.get("ref"),
                "Alternate": variant.get("alt"),
                "Quality": variant.get("qual"),
                "Filter": variant.get("filter"),
            }
        )
    return rows


def _clinvar_fields(
    annotation: dict[str, object],
) -> tuple[object, object]:
    """Extract the curated ClinVar accession and significance."""

    sources = _dictionary(annotation.get("sources"))
    clinvar = _dictionary(sources.get("clinvar"))
    accession = (
        clinvar.get("accession_version")
        or clinvar.get("accession")
    )
    significance = clinvar.get("clinical_significance")
    return accession, significance


def _genebe_fields(
    annotation: dict[str, object],
) -> tuple[object, object]:
    """Extract independent GeneBe status and automated classification."""

    sources = _dictionary(annotation.get("sources"))
    genebe = _dictionary(sources.get("genebe"))
    return (
        genebe.get("status"),
        genebe.get("automated_acmg_classification"),
    )


def _cspec_fields(
    annotation: dict[str, object],
) -> tuple[object, object]:
    """Extract CSpec availability without interpreting rule content."""

    sources = _dictionary(annotation.get("sources"))
    cspec = _dictionary(sources.get("cspec"))
    return cspec.get("status"), cspec.get("specification_count", 0)


def build_annotation_rows(
    annotations: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return clinically useful flattened annotation rows."""

    rows: list[dict[str, object]] = []
    for annotation in annotations:
        accession, significance = _clinvar_fields(annotation)
        genebe_status, genebe_classification = _genebe_fields(
            annotation
        )
        cspec_status, cspec_count = _cspec_fields(annotation)
        warnings = annotation.get("warnings")
        rows.append(
            {
                "Variant": _variant_label(annotation),
                "Gene": annotation.get("gene"),
                "Consequence": annotation.get("consequence"),
                "Impact": annotation.get("impact"),
                "Protein change": annotation.get("protein_change"),
                "GeneBe status": genebe_status,
                "GeneBe automated classification": (
                    genebe_classification
                ),
                "Population frequency": annotation.get(
                    "population_frequency"
                ),
                "ClinVar accession": accession,
                "ClinVar significance": significance,
                "CSpec status": cspec_status,
                "CSpec specifications": cspec_count,
                "Warnings": (
                    len(warnings) if isinstance(warnings, list) else 0
                ),
            }
        )
    return rows


def build_phenotype_rows(
    phenotype_results: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return flattened HPO matching results."""

    rows: list[dict[str, object]] = []
    for result in phenotype_results:
        phen2gene = _dictionary(result.get("phen2gene"))
        mydisease = _dictionary(result.get("mydisease"))
        mydisease_diseases = mydisease.get("diseases")
        inferred_context = mydisease.get(
            "inferred_pathway_context"
        )
        disease_items = (
            mydisease_diseases
            if isinstance(mydisease_diseases, list)
            else []
        )
        matched_hpo_count = sum(
            len(
                disease.get("matched_patient_hpo_terms", [])
            )
            for disease in disease_items
            if isinstance(disease, dict)
            and isinstance(
                disease.get("matched_patient_hpo_terms"),
                list,
            )
        )
        rows.append(
            {
                "Variant": _variant_label(result),
                "Gene": result.get("gene"),
                "Phenotype score": result.get("phenotype_score"),
                "Matched terms": result.get(
                    "phenotype_match_count",
                    0,
                ),
                "Matched HPO": _joined_text(
                    result.get("matched_hpo_terms")
                ),
                "Submitted HPO": _joined_text(
                    result.get("hpo_terms")
                ),
                "Phenotype-gene provider": phen2gene.get("provider"),
                "Phenotype-gene availability": phen2gene.get(
                    "availability"
                ),
                "Phenotype-gene score": phen2gene.get("score"),
                "Phenotype-gene rank": phen2gene.get("rank"),
                "Phenotype-gene method": phen2gene.get("method"),
                "Phenotype-gene status": phen2gene.get("status"),
                "Fallback used": phen2gene.get("fallback_used", False),
                "Primary provider failure": phen2gene.get(
                    "primary_failure"
                ),
                "MyDisease result": mydisease.get("status"),
                "MyDisease HTTP status": mydisease.get("http_status"),
                "MyDisease provider total": mydisease.get(
                    "provider_total"
                ),
                "MyDisease provider returned": mydisease.get(
                    "provider_returned_count",
                    0,
                ),
                "MyDisease diseases": mydisease.get(
                    "disease_count",
                    0,
                ),
                "MyDisease matched HPO": matched_hpo_count,
                "MyDisease inferred context": (
                    len(inferred_context)
                    if isinstance(inferred_context, list)
                    else 0
                ),
            }
        )
    return rows


def build_mydisease_rows(
    phenotype_results: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return displayable normalized MyDisease disease context."""

    rows: list[dict[str, object]] = []
    for result in phenotype_results:
        mydisease = _dictionary(result.get("mydisease"))
        diseases = mydisease.get("diseases")
        if not isinstance(diseases, list):
            continue
        for item in diseases:
            disease = _dictionary(item)
            relation = _dictionary(
                disease.get("gene_disease_relation")
            )
            rows.append(
                {
                    "Variant": _variant_label(result),
                    "Gene": result.get("gene"),
                    "Gene ID": relation.get("requested_gene_id"),
                    "Disease ID": disease.get("disease_id"),
                    "Disease": disease.get("disease_name"),
                    "Association type": relation.get(
                        "association_type"
                    ),
                    "Matched patient HPO": _joined_text(
                        disease.get("matched_patient_hpo_terms")
                    ),
                    "Phenotype match": disease.get(
                        "phenotype_match_status"
                    ),
                    "Disease HPO terms": len(
                        disease.get("supporting_hpo_terms", [])
                    )
                    if isinstance(
                        disease.get("supporting_hpo_terms"),
                        list,
                    )
                    else 0,
                    "Upstream sources": _joined_text(
                        disease.get("upstream_sources")
                    ),
                    "MyDisease build": mydisease.get(
                        "provider_version"
                    ),
                }
            )
    return rows


def build_monarch_rows(
    phenotype_results: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return legacy read-only Monarch association evidence."""

    rows: list[dict[str, object]] = []
    for result in phenotype_results:
        monarch = _dictionary(result.get("monarch"))
        associations = monarch.get("associations")
        if not isinstance(associations, list):
            continue
        for item in associations:
            association = _dictionary(item)
            sources = association.get("sources")
            publications = association.get("publications")
            rows.append(
                {
                    "Variant": _variant_label(result),
                    "Gene": result.get("gene"),
                    "Gene ID": association.get("gene_id"),
                    "Disease ID": association.get("disease_id"),
                    "Disease": association.get("disease_name"),
                    "HPO ID": association.get("hpo_id"),
                    "Phenotype": association.get("hpo_name"),
                    "Association type": association.get(
                        "association_type"
                    ),
                    "Predicate": association.get("predicate"),
                    "Direction": association.get("direction"),
                    "Sources": _joined_text(
                        sources if isinstance(sources, list) else []
                    ),
                    "Publications": _joined_text(
                        publications
                        if isinstance(publications, list)
                        else []
                    ),
                    "Monarch release": monarch.get(
                        "provider_version"
                    ),
                }
            )
    return rows


def _evidence_summary_rows(
    evidence_objects: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return a compact overview of bounded Evidence Objects."""

    rows: list[dict[str, object]] = []
    for evidence in evidence_objects:
        source_statuses = _dictionary(
            evidence.get("source_statuses")
        )
        successful_sources = sum(
            status == "success"
            for status in source_statuses.values()
        )
        warnings = evidence.get("warnings")
        fallback_notices = build_fallback_notices(
            evidence.get("capability_results")
        )
        rows.append(
            {
                "Variant": _variant_label(evidence),
                "Gene": evidence.get("gene"),
                "Consequence": evidence.get("consequence"),
                "ClinVar significance": evidence.get(
                    "clinvar_significance"
                ),
                "Phenotype score": evidence.get("phenotype_score"),
                "Successful sources": successful_sources,
                "Fallback capabilities": len(fallback_notices),
                "Warnings": (
                    len(warnings) if isinstance(warnings, list) else 0
                ),
            }
        )
    return rows


def _render_variant_table(result: PipelineResult) -> None:
    """Render every filtered input variant without patient genotypes."""

    rows = build_variant_rows(result["variants"])
    if not rows:
        st.info("No filtered variants were provided.")
        return
    st.dataframe(
        rows,
        hide_index=True,
        key="filtered_variant_results",
        column_order=(
            "Variant",
            "Chromosome",
            "Position",
            "Reference",
            "Alternate",
            "Quality",
            "Filter",
        ),
        column_config={
            "Position": st.column_config.NumberColumn(format="%d"),
            "Quality": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.caption(
        "Input display excludes VCF genotype and sample fields."
    )


def _render_annotation_table(result: PipelineResult) -> None:
    """Render standardized annotation fields only."""

    rows = build_annotation_rows(result["annotations"])
    if not rows:
        st.info("No annotations were retained.")
        return
    st.dataframe(
        rows,
        hide_index=True,
        key="annotation_results",
        column_order=(
            "Variant",
            "Gene",
            "Consequence",
            "Impact",
            "Protein change",
            "Population frequency",
            "ClinVar accession",
            "ClinVar significance",
            "CSpec status",
            "CSpec specifications",
            "Warnings",
        ),
        column_config={
            "Population frequency": st.column_config.NumberColumn(
                format="%.6g"
            ),
            "Warnings": st.column_config.NumberColumn(format="%d"),
            "CSpec specifications": st.column_config.NumberColumn(
                format="%d"
            ),
        },
    )


def _render_phenotype_table(result: PipelineResult) -> None:
    """Render optional HPO correlation output."""

    rows = build_phenotype_rows(result["phenotype_results"])
    if not rows:
        st.info("No phenotype-matching results were retained.")
        return
    if not any(row["Submitted HPO"] for row in rows):
        st.info(
            "No HPO terms were supplied; variants continued without "
            "phenotype scoring."
        )
    st.dataframe(
        rows,
        hide_index=True,
        key="phenotype_results",
        column_order=(
            "Variant",
            "Gene",
            "Phenotype score",
            "Matched terms",
            "Matched HPO",
            "Submitted HPO",
            "Phenotype-gene provider",
            "Phenotype-gene availability",
            "Phenotype-gene score",
            "Phenotype-gene rank",
            "Phenotype-gene method",
            "Phenotype-gene status",
            "Fallback used",
            "Primary provider failure",
            "MyDisease result",
            "MyDisease HTTP status",
            "MyDisease provider total",
            "MyDisease provider returned",
            "MyDisease diseases",
            "MyDisease matched HPO",
            "MyDisease inferred context",
        ),
        column_config={
            "Phenotype score": st.column_config.NumberColumn(
                format="percent"
            ),
            "Matched terms": st.column_config.NumberColumn(format="%d"),
            "Phenotype-gene score": st.column_config.NumberColumn(
                format="%.6f"
            ),
            "Phenotype-gene rank": (
                st.column_config.NumberColumn(format="%d")
            ),
            "MyDisease diseases": st.column_config.NumberColumn(
                format="%d"
            ),
            "MyDisease HTTP status": st.column_config.NumberColumn(
                format="%d"
            ),
            "MyDisease provider total": st.column_config.NumberColumn(
                format="%d"
            ),
            "MyDisease provider returned": (
                st.column_config.NumberColumn(format="%d")
            ),
            "MyDisease matched HPO": st.column_config.NumberColumn(
                format="%d"
            ),
            "MyDisease inferred context": st.column_config.NumberColumn(
                format="%d"
            ),
        },
    )
    st.caption(
        "Phenotype-gene rank is source-specific context. Local fallback "
        "uses direct HPO-gene overlap only; it does not reproduce the "
        "Phen2Gene algorithm, reorder variants, or change pathogenicity."
    )
    mydisease_rows = build_mydisease_rows(
        result["phenotype_results"]
    )
    if mydisease_rows:
        st.markdown("**MyDisease.info gene-disease-phenotype context**")
        st.dataframe(
            mydisease_rows,
            hide_index=True,
            key="mydisease_context_results",
            column_order=(
                "Variant",
                "Gene",
                "Gene ID",
                "Disease ID",
                "Disease",
                "Association type",
                "Matched patient HPO",
                "Phenotype match",
                "Disease HPO terms",
                "Upstream sources",
                "MyDisease build",
            ),
        )
    st.caption(
        "Only direct structured gene-disease associations populate the "
        "primary MyDisease.info table. A no_association or no exact HPO "
        "match means missing context, not negative biological evidence; "
        "this context does not change pathogenicity."
    )
    monarch_rows = build_monarch_rows(result["phenotype_results"])
    if monarch_rows:
        st.markdown("**Legacy Monarch evidence (read-only)**")
        st.dataframe(
            monarch_rows,
            hide_index=True,
            key="legacy_monarch_association_results",
        )


def _render_source_statuses(evidence: dict[str, object]) -> None:
    """Render the four normalized annotation source statuses."""

    statuses = _dictionary(evidence.get("source_statuses"))
    with st.container(horizontal=True):
        for source in ("vep", "myvariant", "clinvar", "clingen"):
            value = str(statuses.get(source, "not available"))
            st.metric(
                SOURCE_LABELS[source],
                value.replace("_", " ").title(),
                border=True,
            )


def _render_fallback_notices(evidence: dict[str, object]) -> None:
    """Show concise degraded-mode notices and exact fallback provenance."""

    notices = build_fallback_notices(evidence.get("capability_results"))
    if not notices:
        return
    with st.container(border=True):
        st.markdown(":material/swap_horiz: **Fallback evidence used**")
        st.caption(
            "Fallback results remain source-specific and are not relabelled "
            "as primary-provider evidence."
        )
        for notice in notices:
            st.write(f"- {notice['message']}")
        with st.expander(
            "Fallback provenance details",
            icon=":material/info:",
        ):
            st.table(
                [
                    {
                        "Capability": notice["capability_label"],
                        "Primary": notice["primary_provider_label"],
                        "Fallback": notice["fallback_provider_label"],
                        "Primary failure": notice["primary_failure"],
                        "Method": notice["method_label"],
                    }
                    for notice in notices
                ]
            )


def _render_evidence_details(evidence: dict[str, object]) -> None:
    """Render one sanitized Evidence Object in clinical sections."""

    with st.container(horizontal=True):
        st.metric(
            "Gene",
            evidence.get("gene") or "Not available",
            border=True,
        )

        frequency = evidence.get("population_frequency")
        st.metric(
            "Population frequency",
            (
                f"{frequency:.6g}"
                if isinstance(frequency, (int, float))
                else "Not available"
            ),
            border=True,
        )

        phenotype_score = evidence.get("phenotype_score")
        st.metric(
            "Phenotype score",
            (
                f"{phenotype_score:.0%}"
                if isinstance(phenotype_score, (int, float))
                else "Not available"
            ),
            border=True,
        )

    _render_fallback_notices(evidence)

    st.markdown("**Annotation**")
    st.table(
        [
            {
                "Consequence": evidence.get("consequence"),
                "Impact": evidence.get("impact"),
                "Transcript": evidence.get("transcript"),
                "Protein change": evidence.get("protein_change"),
            }
        ]
    )

    st.markdown("**Evidence source status**")
    _render_source_statuses(evidence)

    with st.container(border=True):
        st.markdown("**ClinVar evidence**")
        st.write(
            "Accession:",
            evidence.get("clinvar_accession") or "Not available",
        )
        st.write(
            "Clinical significance:",
            evidence.get("clinvar_significance") or "Not available",
        )
        st.write(
            "Review status:",
            evidence.get("clinvar_review_status") or "Not available",
        )
        conditions = evidence.get("clinvar_conditions")
        st.write(
            "Conditions:",
            (
                ", ".join(str(item) for item in conditions)
                if isinstance(conditions, list) and conditions
                else "Not available"
            ),
        )

    curations = evidence.get("clingen_curations")
    st.markdown("**ClinGen/GenCC gene–disease evidence**")
    if isinstance(curations, list) and curations:
        st.dataframe(
            [
                {
                    "Disease": _dictionary(curation).get("disease"),
                    "Disease ID": _dictionary(curation).get(
                        "disease_id"
                    ),
                    "Classification": _dictionary(curation).get(
                        "classification"
                    ),
                    "Inheritance": _dictionary(curation).get(
                        "mode_of_inheritance"
                    ),
                    "PMIDs": _joined_text(
                        _dictionary(curation).get("pmids")
                    ),
                    "Report": _dictionary(curation).get("report_url"),
                }
                for curation in curations
            ],
            hide_index=True,
            key="evidence_clingen_curations",
            column_config={
                "Report": st.column_config.LinkColumn(
                    "Report",
                    display_text="Open",
                )
            },
        )
    else:
        st.caption("No ClinGen/GenCC curations were retained.")

    with st.container(border=True):
        st.markdown("**Phenotype correlation**")
        st.write(
            "Submitted HPO terms:",
            _joined_text(evidence.get("hpo_terms"))
            or "Not available",
        )
        st.write(
            "Matched HPO terms:",
            _joined_text(evidence.get("matched_hpo_terms"))
            or "None",
        )

    references = build_canonical_references(evidence)
    st.markdown("**References**")
    if references:
        st.dataframe(
            [
                {
                    "Reference": reference["reference_id"],
                    "Source": reference["source"],
                    "Identifier": reference["identifier"],
                    "Link status": reference["url_status"],
                    "URL": reference["canonical_url"],
                }
                for reference in references
            ],
            hide_index=True,
            key="evidence_references",
            column_config={
                "URL": st.column_config.LinkColumn(
                    "Reference",
                    display_text="Open",
                )
            },
        )
    else:
        st.caption("No source references were retained.")

    warnings = evidence.get("warnings")
    if isinstance(warnings, list) and warnings:
        for warning in warnings:
            st.warning(str(warning))

    with st.expander(
        "Sanitized Evidence Object",
        icon=":material/data_object:",
    ):
        st.json(evidence, expanded=2)


def _render_evidence_view(result: PipelineResult) -> None:
    """Render Evidence Object overview and selected details."""

    evidence_objects = result["evidence_objects"]
    if not evidence_objects:
        st.info("No Evidence Objects were retained.")
        return

    st.dataframe(
        _evidence_summary_rows(evidence_objects),
        hide_index=True,
        key="evidence_summary",
        column_config={
            "Phenotype score": st.column_config.NumberColumn(
                format="percent"
            ),
            "Successful sources": st.column_config.NumberColumn(
                format="%d"
            ),
            "Fallback capabilities": st.column_config.NumberColumn(
                format="%d"
            ),
            "Warnings": st.column_config.NumberColumn(format="%d"),
        },
    )
    selected_index = st.selectbox(
        "Evidence Object details",
        range(len(evidence_objects)),
        key="selected_evidence_object",
        format_func=lambda index: (
            f"{_variant_label(evidence_objects[index])} — "
            f"{evidence_objects[index].get('gene') or 'gene unavailable'}"
        ),
    )
    _render_evidence_details(evidence_objects[selected_index])


def render_analysis_results(result: PipelineResult) -> None:
    """Render retained Stage 10 collections without raw provider payloads."""

    st.subheader("Analysis results")
    with st.container(horizontal=True):
        st.metric(
            "Input variants",
            result["variant_count"],
            border=True,
        )
        st.metric(
            "Annotations",
            len(result["annotations"]),
            border=True,
        )
        st.metric(
            "Evidence Objects",
            len(result["evidence_objects"]),
            border=True,
        )

    variants_tab, annotations_tab, phenotype_tab, evidence_tab = (
        st.tabs(
            (
                "Input variants",
                "Annotations",
                "Phenotype",
                "Evidence",
            )
        )
    )
    with variants_tab:
        _render_variant_table(result)
    with annotations_tab:
        _render_annotation_table(result)
    with phenotype_tab:
        _render_phenotype_table(result)
    with evidence_tab:
        _render_evidence_view(result)

    if result["warnings"]:
        with st.expander(
            f"Pipeline warnings ({len(result['warnings'])})",
            icon=":material/warning:",
        ):
            for warning in result["warnings"]:
                st.warning(warning)


__all__ = [
    "build_annotation_rows",
    "build_variant_rows",
    "build_phenotype_rows",
    "render_analysis_results",
]
