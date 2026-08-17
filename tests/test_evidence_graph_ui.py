"""Deterministic tests for the presentation-only Evidence Graph explorer."""

from __future__ import annotations

from contextlib import contextmanager

import frontend.evidence_graph as evidence_graph


def test_simple_view_covers_the_required_semantic_paths() -> None:
    diagram = evidence_graph.EVIDENCE_GRAPH_VIEWS[evidence_graph.SIMPLE_VIEW].diagram

    for label in (
        "Input variant / patient HPO",
        "Annotation",
        "Automated ACMG context",
        "Expert curated variant context",
        "ClinVar clinical evidence",
        "Gene-disease validity",
        "Gene-disease support",
        "Phenotype-gene ranking",
        "Phenotype-gene support",
        "Disease/HPO context",
        "Population evidence",
        "Literature evidence",
        "Stage 7 coverage",
        "Stage 8 final disposition",
        "Sanitized evidence for one selected LLM",
        "Mandatory human review",
    ):
        assert label in diagram


def test_advanced_view_preserves_correlation_and_stage6_boundaries() -> None:
    diagram = evidence_graph.EVIDENCE_GRAPH_VIEWS[evidence_graph.ADVANCED_VIEW].diagram

    assert "GenCC validity evidence" in diagram
    assert "MedGen supporting context" in diagram
    assert "MyVariant ClinVar-derived rescue" in diagram
    assert "correlated rescue, not independent vote" in diagram
    assert "Promotable: gene, transcript, HGVS.c, HGVS.p" in diagram
    assert "Not actively promoted: consequence, impact, MANE, canonical" in diagram
    assert "Excluded from LLM" in diagram
    assert "annotation promotion" in diagram
    assert "Mandatory human review" in diagram


def test_renderer_is_static_and_does_not_mutate_analysis_state(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}
    state = {"pipeline_result": None}

    @contextmanager
    def fake_container(**_kwargs: object):
        yield

    monkeypatch.setattr(evidence_graph.st, "session_state", state)
    monkeypatch.setattr(evidence_graph.st, "container", fake_container)
    monkeypatch.setattr(
        evidence_graph.st,
        "segmented_control",
        lambda *_args, **_kwargs: evidence_graph.ADVANCED_VIEW,
    )
    monkeypatch.setattr(
        evidence_graph.st,
        "mermaid_chart",
        lambda diagram, **_kwargs: observed.setdefault("diagram", diagram),
    )
    monkeypatch.setattr(
        evidence_graph.st,
        "selectbox",
        lambda *_args, **_kwargs: "Annotation",
    )
    monkeypatch.setattr(
        evidence_graph.st,
        "caption",
        lambda message, *_args, **_kwargs: observed.setdefault(
            "captions", []
        ).append(message),
    )
    monkeypatch.setattr(evidence_graph.st, "markdown", lambda *_args, **_kwargs: None)

    evidence_graph._render_evidence_graph_contents()

    assert observed["diagram"] == evidence_graph.EVIDENCE_GRAPH_VIEWS[
        evidence_graph.ADVANCED_VIEW
    ].diagram
    assert state == {"pipeline_result": None}
    assert any("Promotable fields" in caption for caption in observed["captions"])
