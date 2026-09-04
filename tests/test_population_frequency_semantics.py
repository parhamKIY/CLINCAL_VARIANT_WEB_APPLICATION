"""Population-frequency source separation and LLM-boundary contracts."""

from __future__ import annotations

from typing import Any

from backend.annotation import (
    _apply_ucsc_normal_population_frequency_fallback,
    _base_annotation,
    _standardize_myvariant_response,
)
from backend.conditional_enrichment import UCSC_GNOMAD_TRACKS
from backend.report import build_evidence_object
from backend.shadow_composition import semantic_evidence_for_llm
from config import settings


class _Response:
    def __init__(self, payload: object) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _UCSCSession:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _annotation() -> dict[str, Any]:
    annotation = _base_annotation(
        {"chrom": "5", "pos": 60944943, "ref": "C", "alt": "T"},
        status="success",
    )
    annotation.update(
        {
            "gene": "ERCC8",
            "gene_id": "ENSG00000049167",
            "transcript": "NM_000082.4",
            "consequence": "synonymous_variant",
            "hgvsc": "NM_000082.4:c.66G>A",
            "hgvsp": "NP_000073.1:p.(Glu22=)",
            "protein_change": "NP_000073.1:p.(Glu22=)",
        }
    )
    annotation["sources"]["vep"].update(
        {
            "gene": "ERCC8",
            "gene_id": "ENSG00000049167",
            "transcript": "NM_000082.4",
            "consequence": "synonymous_variant",
        }
    )
    return annotation


def _mixed_population_payload() -> dict[str, object]:
    return {
        "_id": "chr5:g.60944943C>T",
        "gnomad_exome": {
            "af": {"af": 0.001, "af_afr": 0.002, "af_asj": 0.003}
        },
        "gnomad_genome": {
            "af": {"af": 0.0008, "af_nfe": 0.004}
        },
        "exac": {"af": 0.005},
        "dbsnp": {
            "alleles": [
                {
                    "allele": "T",
                    "freq": {"sgdp_prj": 0.5, "gnomad": 0.0025},
                }
            ]
        },
    }


def test_unrelated_dataset_max_never_becomes_generic_or_llm_frequency() -> None:
    annotation = _annotation()

    _standardize_myvariant_response(
        annotation,
        _mixed_population_payload(),
        "chr5:g.60944943C>T",
    )

    source = annotation["sources"]["myvariant"]
    provenance = annotation["population_frequency_provenance"]
    assert annotation["population_frequency"] == 0.001
    assert provenance["underlying_dataset"] == "gnomAD"
    assert provenance["available_global_af"] == {
        "gnomad_exome": 0.001,
        "gnomad_genome": 0.0008,
    }
    assert provenance["selection_method"] == (
        "maximum_gnomad_global_af_across_exome_genome"
    )
    assert provenance["population_max"] == {
        "frequency": 0.004,
        "population": "NFE",
        "dataset": "gnomad_genome",
        "selection_method": (
            "maximum_reported_gnomad_population_or_subgroup_af"
        ),
    }
    assert source["population_frequencies"]["dbsnp_sgdp_prj"] == 0.5
    assert source["max_population_frequency"] == 0.004
    assert source["max_population"] == "NFE"
    assert source["max_population_dataset"] == "gnomad_genome"

    evidence = build_evidence_object(annotation)
    llm_evidence = semantic_evidence_for_llm(evidence)
    llm_population = llm_evidence["annotations"]["population"]
    assert llm_evidence["population_frequency"] == 0.001
    assert llm_population["population_frequency"] == 0.001
    assert llm_population["max_population_frequency"] == 0.004
    assert llm_population["population_frequencies"]["dbsnp_sgdp_prj"] == 0.5


def test_dbsnp_or_exac_frequency_does_not_suppress_gnomad_fallback() -> None:
    annotation = _annotation()
    payload = _mixed_population_payload()
    payload.pop("gnomad_exome")
    payload.pop("gnomad_genome")
    _standardize_myvariant_response(
        annotation,
        payload,
        "chr5:g.60944943C>T",
    )
    tracks = UCSC_GNOMAD_TRACKS["GRCh38"]["tracks"]
    assert isinstance(tracks, dict)
    session = _UCSCSession(
        [
            _Response(
                {
                    tracks["exome"]: [
                        {
                            "chrom": "chr5",
                            "chromStart": 60944942,
                            "chromEnd": 60944943,
                            "ref": "C",
                            "alt": "T",
                            "AF": 0.0009,
                            "AC": 9,
                            "AN": 10_000,
                            "FILTER": "PASS",
                        }
                    ]
                }
            ),
            _Response(
                {
                    tracks["genome"]: [
                        {
                            "chrom": "chr5",
                            "chromStart": 60944942,
                            "chromEnd": 60944943,
                            "ref": "C",
                            "alt": "T",
                            "AF": 0.0007,
                            "AC": 7,
                            "AN": 10_000,
                            "FILTER": "PASS",
                        }
                    ]
                }
            ),
        ]
    )

    assert annotation["population_frequency"] is None
    assert annotation["population_frequency_provenance"]["status"] == (
        "no_usable_gnomad_frequency"
    )
    assert annotation["sources"]["myvariant"]["population_frequencies"] == {
        "exac": 0.005,
        "dbsnp_sgdp_prj": 0.5,
        "dbsnp_gnomad": 0.0025,
    }

    _apply_ucsc_normal_population_frequency_fallback(
        annotation,
        session,  # type: ignore[arg-type]
    )

    assert annotation["population_frequency"] == 0.0009
    assert annotation["population_frequency_provenance"]["provider"] == (
        "UCSC gnomAD"
    )
    assert len(session.calls) == 2
