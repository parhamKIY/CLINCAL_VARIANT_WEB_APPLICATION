"""Streamlit views for frontend-safe pipeline result collections."""

from __future__ import annotations

import streamlit as st

from backend.pipeline import PipelineResult


SOURCE_LABELS = {
    "vep": "VEP",
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


def build_candidate_rows(
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return a genotype-free candidate table."""

    rows: list[dict[str, object]] = []
    for candidate in candidates:
        variant = _variant(candidate)
        rows.append(
            {
                "Variant": _variant_label(candidate),
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


def build_annotation_rows(
    annotations: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return clinically useful flattened annotation rows."""

    rows: list[dict[str, object]] = []
    for annotation in annotations:
        accession, significance = _clinvar_fields(annotation)
        warnings = annotation.get("warnings")
        rows.append(
            {
                "Variant": _variant_label(annotation),
                "Gene": annotation.get("gene"),
                "Consequence": annotation.get("consequence"),
                "Impact": annotation.get("impact"),
                "Protein change": annotation.get("protein_change"),
                "Population frequency": annotation.get(
                    "population_frequency"
                ),
                "ClinVar accession": accession,
                "ClinVar significance": significance,
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
                "Warnings": (
                    len(warnings) if isinstance(warnings, list) else 0
                ),
            }
        )
    return rows


def _render_candidate_table(result: PipelineResult) -> None:
    """Render bounded candidate variants without patient genotypes."""

    rows = build_candidate_rows(result["candidates"])
    if not rows:
        st.info("No candidate variants were retained.")
        return
    st.dataframe(
        rows,
        hide_index=True,
        key="candidate_results",
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
        "Candidate display excludes VCF genotype and sample fields."
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
            "Warnings",
        ),
        column_config={
            "Population frequency": st.column_config.NumberColumn(
                format="%.6g"
            ),
            "Warnings": st.column_config.NumberColumn(format="%d"),
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
            "No HPO terms were supplied; candidates continued without "
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
        ),
        column_config={
            "Phenotype score": st.column_config.NumberColumn(
                format="percent"
            ),
            "Matched terms": st.column_config.NumberColumn(format="%d"),
        },
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

    references = evidence.get("references")
    st.markdown("**References**")
    if isinstance(references, list) and references:
        st.dataframe(
            [
                {
                    "Source": _dictionary(reference).get("source"),
                    "URL": _dictionary(reference).get("url"),
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
            "Processed variants",
            result["variant_count"],
            border=True,
        )
        st.metric(
            "Candidates",
            len(result["candidates"]),
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

    candidates_tab, annotations_tab, phenotype_tab, evidence_tab = (
        st.tabs(
            (
                "Candidates",
                "Annotations",
                "Phenotype",
                "Evidence",
            )
        )
    )
    with candidates_tab:
        _render_candidate_table(result)
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
    "build_candidate_rows",
    "build_phenotype_rows",
    "render_analysis_results",
]
