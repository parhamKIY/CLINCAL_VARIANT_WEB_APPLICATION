"""Stage 94 deterministic reference-validation regression matrix."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.human_links import (
    is_machine_readable_url,
    resolve_genebe_human_url,
)
from backend.reference_model import build_reference_model_v2
from backend.references import canonicalize_reference
from test_pipeline import TestEvidenceObject as EvidenceFactory


pytestmark = pytest.mark.stage94_reference_validation


def _evidence() -> dict[str, object]:
    return deepcopy(EvidenceFactory._complete_evidence_object())


@pytest.mark.parametrize(
    ("source", "identifier", "expected_url"),
    [
        (
            "PubMed",
            "PMID: 25741868",
            "https://pubmed.ncbi.nlm.nih.gov/25741868/",
        ),
        (
            "PubMed Central",
            "PMC4382204",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC4382204/",
        ),
        (
            "DOI",
            "10.1038/nature12373",
            "https://doi.org/10.1038/nature12373",
        ),
    ],
)
def test_literature_identifier_resolves_to_expected_article(
    source: str,
    identifier: str,
    expected_url: str,
) -> None:
    reference = canonicalize_reference(source=source, identifier=identifier)

    assert reference["canonical_url"] == expected_url
    assert reference["url_status"] == "validated"
    assert not is_machine_readable_url(source, expected_url)


@pytest.mark.parametrize(
    ("accession", "expected_url"),
    [
        (
            "VCV000033448.1",
            "https://www.ncbi.nlm.nih.gov/clinvar/variation/33448/",
        ),
        (
            "RCV000012345.2",
            "https://www.ncbi.nlm.nih.gov/clinvar/?term=RCV000012345.2",
        ),
        (
            "SCV000012345.3",
            "https://www.ncbi.nlm.nih.gov/clinvar/?term=SCV000012345.3",
        ),
    ],
)
def test_clinvar_identifier_resolves_to_human_record(
    accession: str,
    expected_url: str,
) -> None:
    reference = canonicalize_reference(
        source="NCBI ClinVar",
        identifier=accession,
    )

    assert reference["canonical_url"] == expected_url
    assert reference["url_status"] == "validated"


@pytest.mark.parametrize(
    ("assembly", "expected_url"),
    [
        (
            "GRCh38",
            "https://genebe.net/variant/hg38/13-52152671-T-C",
        ),
        (
            "GRCh37",
            "https://genebe.net/variant/hg19/13-52152671-T-C",
        ),
    ],
)
def test_supported_genebe_variant_resolves_to_human_record(
    assembly: str,
    expected_url: str,
) -> None:
    resolved = resolve_genebe_human_url(
        assembly=assembly,
        variant={"chrom": "13", "pos": 52152671, "ref": "T", "alt": "C"},
    )

    assert resolved == (expected_url, "13-52152671-T-C")
    assert not is_machine_readable_url("GeneBe", expected_url)


def test_myvariant_never_exposes_a_primary_raw_json_link() -> None:
    evidence = _evidence()
    references = evidence["references"]
    assert isinstance(references, list)
    references.append(
        {
            "source": "MyVariant.info",
            "url": "https://myvariant.info/v1/variant/chr2:g.166848215C%3ET",
        }
    )

    source = next(
        item
        for item in build_reference_model_v2(evidence)["data_sources"]
        if item["source"] == "MyVariant.info"
    )

    assert source["record_identifier"] == "chr2:g.166848215C>T"
    assert source["human_url"] is None
    assert source["link_status"] == "unavailable"


def test_missing_ensembl_identifier_does_not_fabricate_link() -> None:
    source = next(
        item
        for item in build_reference_model_v2(_evidence())["data_sources"]
        if item["source"] == "Ensembl"
    )

    assert source["record_identifier"] is None
    assert source["human_url"] is None
    assert source["link_status"] == "unavailable"


def test_unsupported_provider_remains_provenance_only() -> None:
    evidence = _evidence()
    references = evidence["references"]
    assert isinstance(references, list)
    references.append({"source": "Unsupported Research Database"})

    source = next(
        item
        for item in build_reference_model_v2(evidence)["data_sources"]
        if item["source"] == "Unsupported Research Database"
    )

    assert source["capability"] == "database_record"
    assert source["human_url"] is None
    assert source["link_status"] == "unavailable"


def test_reference_model_contains_no_machine_readable_links() -> None:
    evidence = _evidence()
    annotations = evidence["annotations"]
    assert isinstance(annotations, dict)
    population = annotations["population"]
    assert isinstance(population, dict)
    population["rsid"] = "rs121913529"

    for source in build_reference_model_v2(evidence)["data_sources"]:
        url = source["human_url"]
        assert (url is not None) == (source["link_status"] == "validated")
        assert not is_machine_readable_url(source["source"], url)
