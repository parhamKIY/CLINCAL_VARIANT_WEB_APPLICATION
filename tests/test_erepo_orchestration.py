"""Offline enabled-ERepo orchestration contract coverage."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json

import pytest

import backend.annotation as annotation_module
from backend.annotation import annotate_variants
from backend.report import build_evidence_object, validate_evidence_object
from config import settings


FIXTURES = Path(__file__).parent / "fixtures" / "erepo"
pytestmark = pytest.mark.stage59_testing_v3


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        return deepcopy(self.payload)


class _ERepoSession:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.requests: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.requests.append(url)
        return self.responses.pop(0)


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _patch_non_erepo_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_vep(
        _session: object,
        inputs: list[str],
        _retries: int,
        *_orchestration: object,
    ) -> list[dict[str, object]]:
        return [
            {
                "input": inputs[0],
                "assembly_name": "GRCh38",
                "most_severe_consequence": "missense_variant",
                "transcript_consequences": [
                    {
                        "gene_symbol": "PAH",
                        "gene_id": "ENSG00000171759",
                        "transcript_id": "ENST00000553106",
                        "consequence_terms": ["missense_variant"],
                        "hgvsc": "ENST00000553106:c.782G>A",
                        "hgvsp": "ENSP00000448059:p.Arg261Gln",
                    }
                ],
            }
        ]

    def fake_genebe(
        annotations: list[dict[str, object]],
        *_orchestration: object,
    ) -> bool:
        for item in annotations:
            mark_not_found(item, "genebe")
        return False

    def mark_not_found(annotation: dict[str, object], source: str) -> None:
        sources = annotation["sources"]
        assert isinstance(sources, dict)
        source_data = sources[source]
        assert isinstance(source_data, dict)
        source_data["status"] = "not_found"

    monkeypatch.setattr(annotation_module, "_post_vep_batch", fake_vep)
    monkeypatch.setattr(
        annotation_module,
        "_apply_ucsc_normal_population_frequency_fallback",
        lambda *_args: None,
    )
    monkeypatch.setattr(annotation_module, "_annotate_with_genebe", fake_genebe)
    for source, function in (
        ("myvariant", "_annotate_with_myvariant"),
        ("clinvar", "_annotate_with_clinvar"),
        ("clingen", "_annotate_with_clingen"),
        ("cspec", "_annotate_with_cspec"),
    ):
        monkeypatch.setattr(annotation_module, function, lambda annotation, *_args, _source=source: mark_not_found(annotation, _source))


@pytest.mark.parametrize("summary_status", ["success", "no_match", "forbidden"])
def test_enabled_erepo_orchestration_is_sanitized_and_non_blocking(
    monkeypatch: pytest.MonkeyPatch, summary_status: str
) -> None:
    _patch_non_erepo_sources(monkeypatch)
    monkeypatch.setattr(settings, "ENABLE_EREPO", True)
    if summary_status == "success":
        responses = [
            _Response(200, _fixture("summary_classification_exact_success_ca229507.json")),
            _Response(200, _fixture("detail_classification_exact_success_ca229507.json")),
        ]
    elif summary_status == "no_match":
        responses = [_Response(404, _fixture("summary_classification_exact_no_match.json"))]
    else:
        responses = [_Response(403, {"status": {"code": 403}})]
    session = _ERepoSession(responses)

    annotation = annotate_variants(
        [{"chrom": "12", "pos": 102894804, "ref": "T", "alt": "A"}],
        session=session,  # type: ignore[arg-type]
        max_retries=0,
        use_cache=False,
    )[0]
    evidence = build_evidence_object(annotation)

    assert validate_evidence_object(evidence) == evidence
    context = evidence["pathogenicity"]["expert_curated_variant_context"]
    assert context["status"] == ({"success": "success", "no_match": "no_match", "forbidden": "forbidden"}[summary_status])
    assert annotation["sources"]["vep"]["status"] == "success"
    assert len(session.requests) == (2 if summary_status == "success" else 1)
    assert annotation["sources"]["genebe"]["automated_acmg_classification"] is None
