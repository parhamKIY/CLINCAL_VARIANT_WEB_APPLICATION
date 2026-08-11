"""Stage 103 user-facing reference and provenance acceptance suite."""

from __future__ import annotations

from copy import deepcopy
from io import BytesIO

import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE

from backend.human_links import (
    is_machine_readable_url,
    resolve_doi_human_url,
    resolve_pmc_human_url,
    resolve_pubmed_human_url,
)
from backend.llm import LLMClient
from backend.references import canonicalize_reference
from backend.report_data_projection import build_report_data_from_draft
from backend.report_docx import render_report_data_docx
from backend.variant_interpretation import interpret_variant
from backend.variant_report import build_draft_variant_report
from frontend.report_preview import render_draft_report_preview_pages
from test_pipeline import (
    FakeLLMAdapter,
    TestEvidenceObject as EvidenceFactory,
    _variant_interpretation_response,
)


pytestmark = pytest.mark.stage103_reference_acceptance
_ANALYSIS_ID = f"analysis-{'3' * 32}"
_TIMESTAMP = "2026-08-11T15:00:00Z"


def _evidence_with_all_reference_types() -> dict[str, object]:
    evidence = deepcopy(EvidenceFactory._complete_evidence_object())
    annotations = evidence["annotations"]
    assert isinstance(annotations, dict)
    population = annotations["population"]
    assert isinstance(population, dict)
    population["rsid"] = "rs121913529"

    enrichment = evidence["conditional_enrichment"]
    assert isinstance(enrichment, dict)
    literature = enrichment["literature"]
    assert isinstance(literature, dict)
    literature["articles"] = [
        {
            "pmcid": "PMC4544753",
            "title": "Sequence variant interpretation standards",
        },
        {
            "doi": "10.1038/gim.2015.30",
            "title": "ACMG and AMP sequence variant interpretation guidance",
        },
    ]
    return evidence


def _artifacts(
    evidence: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    supplied = evidence or _evidence_with_all_reference_types()
    interpretation = interpret_variant(
        supplied,
        client=LLMClient(
            FakeLLMAdapter(_variant_interpretation_response())
        ),
        timestamp=_TIMESTAMP,
    )
    draft = build_draft_variant_report(
        supplied,
        interpretation,
        variant_index=0,
    )
    report_data = build_report_data_from_draft(
        draft,
        analysis_id=_ANALYSIS_ID,
    )
    return draft, report_data


def _docx_hyperlinks(report_data: dict[str, object]) -> set[str]:
    document = Document(BytesIO(render_report_data_docx(report_data)))
    return {
        relationship.target_ref
        for relationship in document.part.rels.values()
        if relationship.reltype == RELATIONSHIP_TYPE.HYPERLINK
    }


def _expected_literature_url(reference: dict[str, object]) -> str | None:
    identifier = reference["identifier"]
    if reference["identifier_type"] == "PMID":
        return resolve_pubmed_human_url(identifier)
    if reference["identifier_type"] == "PMCID":
        return resolve_pmc_human_url(identifier)
    if reference["identifier_type"] == "DOI":
        return resolve_doi_human_url(identifier)
    return None


def test_all_literature_links_target_their_exact_human_records() -> None:
    draft, report_data = _artifacts()
    page_three = render_draft_report_preview_pages(draft)[2]
    docx_links = _docx_hyperlinks(report_data)
    references = report_data["literature_references"]

    assert {item["identifier_type"] for item in references} == {
        "PMID",
        "PMCID",
        "DOI",
    }
    for reference in references:
        expected = _expected_literature_url(reference)
        assert expected is not None
        assert reference["canonical_url"] == expected
        assert reference["url_status"] == "validated"
        assert f'href="{expected}"' in page_three
        assert expected in docx_links


def test_database_sources_stay_separate_from_numbered_literature() -> None:
    draft, report_data = _artifacts()
    content = draft["reviewed_report"]
    page_three = render_draft_report_preview_pages(draft)[2]
    literature_html, data_source_html = page_three.split(
        "<h2>Data Sources</h2>",
        maxsplit=1,
    )

    assert all(
        reference["identifier_type"] in {"PMID", "PMCID", "DOI"}
        for reference in content["literature_references"]
    )
    assert all(
        "reference_id" not in source for source in content["data_sources"]
    )
    assert "ClinVar" not in literature_html.split(
        "<h2>Literature References</h2>",
        maxsplit=1,
    )[1]
    assert "ClinVar" in data_source_html
    assert report_data["literature_references"] == (
        content["literature_references"]
    )
    assert report_data["data_sources"] == content["data_sources"]


def test_raw_json_endpoint_cannot_masquerade_as_normal_reference() -> None:
    evidence = _evidence_with_all_reference_types()
    raw_url = (
        "https://myvariant.info/v1/variant/"
        "chr2:g.166848215C%3ET"
    )
    references = evidence["references"]
    assert isinstance(references, list)
    references.append({"source": "MyVariant.info", "url": raw_url})

    draft, report_data = _artifacts(evidence)
    content = draft["reviewed_report"]
    myvariant = next(
        source
        for source in content["data_sources"]
        if source["source"] == "MyVariant.info"
    )
    page_three = render_draft_report_preview_pages(draft)[2]

    assert all(
        reference["source"] != "MyVariant.info"
        for reference in content["literature_references"]
    )
    assert myvariant["human_url"] is None
    assert myvariant["link_status"] == "unavailable"
    assert "Programmatic annotation source" in page_three
    assert raw_url not in page_three
    assert raw_url not in _docx_hyperlinks(report_data)


@pytest.mark.parametrize(
    ("source", "identifier"),
    [
        ("PubMed", "not-a-pmid"),
        ("PubMed Central", "not-a-pmcid"),
        ("DOI", "not-a-doi"),
        ("NCBI ClinVar", "not-an-accession"),
    ],
)
def test_invalid_identifiers_never_generate_fabricated_links(
    source: str,
    identifier: str,
) -> None:
    reference = canonicalize_reference(
        source=source,
        identifier=identifier,
    )

    assert reference["canonical_url"] is None
    assert reference["url_status"] == "unavailable"


def test_available_provider_links_are_human_readable_pages() -> None:
    draft, _ = _artifacts()
    sources = {
        source["source"]: source
        for source in draft["reviewed_report"]["data_sources"]
    }
    expected = {
        "ClinVar": "https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/",
        "Ensembl": (
            "https://www.ensembl.org/Homo_sapiens/Variation/Explore?"
            "v=rs121913529"
        ),
        "GeneBe": "https://genebe.net/variant/hg38/2-166848215-C-T",
    }

    for source_name, expected_url in expected.items():
        source = sources[source_name]
        assert source["human_url"] == expected_url
        assert source["link_status"] == "validated"
        assert not is_machine_readable_url(source_name, expected_url)


def test_missing_stable_identifier_remains_clean_unlinked_provenance() -> None:
    evidence = _evidence_with_all_reference_types()
    annotations = evidence["annotations"]
    assert isinstance(annotations, dict)
    population = annotations["population"]
    assert isinstance(population, dict)
    population.pop("rsid", None)

    draft, report_data = _artifacts(evidence)
    ensembl = next(
        source
        for source in draft["reviewed_report"]["data_sources"]
        if source["source"] == "Ensembl"
    )
    page_three = render_draft_report_preview_pages(draft)[2]

    assert ensembl["record_identifier"] is None
    assert ensembl["human_url"] is None
    assert ensembl["link_status"] == "unavailable"
    assert "Ensembl" in page_three
    assert "None" not in page_three
    assert "https://www.ensembl.org/" not in page_three
    assert all(
        not link.startswith("https://www.ensembl.org/")
        for link in _docx_hyperlinks(report_data)
    )
