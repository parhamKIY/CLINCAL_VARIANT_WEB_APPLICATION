"""Stage 93 canonical human-link resolver acceptance checks."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from backend.human_links import (
    resolve_clinvar_human_url,
    resolve_doi_human_url,
    resolve_pmc_human_url,
    resolve_pubmed_human_url,
)
from backend.reference_model import (
    ReferenceModelError,
    build_reference_model_v2,
    validate_data_source_record,
)
from backend.references import canonicalize_reference
from backend.report_data import ReportDataError, validate_report_data
from test_pipeline import TestEvidenceObject as EvidenceFactory


pytestmark = pytest.mark.stage93_human_links


def _evidence() -> dict[str, object]:
    return deepcopy(EvidenceFactory._complete_evidence_object())


def test_stable_identifiers_resolve_to_human_pages() -> None:
    assert resolve_pubmed_human_url("PMID: 12345678") == (
        "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    )
    assert resolve_pmc_human_url("pmc123456") == (
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/"
    )
    assert resolve_doi_human_url("10.1000/example") == (
        "https://doi.org/10.1000/example"
    )
    assert resolve_clinvar_human_url("VCV000012345.1") == (
        "https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/"
    )


@pytest.mark.parametrize(
    ("source", "url"),
    [
        (
            "MyVariant.info",
            "https://myvariant.info/v1/variant/chr2:g.166848215C%3ET",
        ),
        (
            "Ensembl VEP",
            "https://rest.ensembl.org/vep/homo_sapiens/region",
        ),
        (
            "GeneBe",
            "https://api.genebe.net/api-public/v1/variants",
        ),
    ],
)
def test_machine_endpoints_cannot_become_canonical_links(
    source: str,
    url: str,
) -> None:
    reference = canonicalize_reference(source=source, url=url)

    assert reference["canonical_url"] is None
    assert reference["url_status"] == "unavailable"


def test_reference_model_resolves_provider_specific_human_targets() -> None:
    evidence = _evidence()
    annotations = evidence["annotations"]
    assert isinstance(annotations, dict)
    population = annotations["population"]
    assert isinstance(population, dict)
    population.update(
        {
            "rsid": "rs121913529",
            "variant_id": "chr2:g.166848215C>T",
        }
    )

    sources = {
        item["source"]: item
        for item in build_reference_model_v2(evidence)["data_sources"]
    }

    assert sources["ClinVar"]["human_url"] == (
        "https://www.ncbi.nlm.nih.gov/clinvar/variation/12345/"
    )
    assert sources["Ensembl"]["human_url"] == (
        "https://www.ensembl.org/Homo_sapiens/Variation/Explore?"
        "v=rs121913529"
    )
    assert sources["GeneBe"]["human_url"] == (
        "https://genebe.net/variant/hg38/2-166848215-C-T"
    )
    assert sources["MyVariant.info"]["human_url"] is None
    assert sources["MyVariant.info"]["record_identifier"] == (
        "chr2:g.166848215C>T"
    )


def test_ensembl_has_provenance_only_without_stable_identifier() -> None:
    source = next(
        item
        for item in build_reference_model_v2(_evidence())["data_sources"]
        if item["source"] == "Ensembl"
    )

    assert source["record_identifier"] is None
    assert source["human_url"] is None
    assert source["link_status"] == "unavailable"


def test_persisted_machine_url_is_rejected() -> None:
    source = next(
        item
        for item in build_reference_model_v2(_evidence())["data_sources"]
        if item["source"] == "MyVariant.info"
    )
    source["human_url"] = "https://myvariant.info/v1/variant/example"
    source["link_status"] = "validated"

    with pytest.raises(ReferenceModelError, match="machine-readable"):
        validate_data_source_record(source)


def test_renderer_contract_rejects_machine_url() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "stage83_golden_report_data_v4.json"
    )
    report = json.loads(fixture_path.read_text(encoding="utf-8"))
    source = report["data_sources"][0]
    source["human_url"] = "https://myvariant.info/v1/variant/example"
    source["link_status"] = "validated"

    with pytest.raises(ReportDataError, match="machine-readable"):
        validate_report_data(report)
