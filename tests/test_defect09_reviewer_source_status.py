"""Defect 09 reviewer-facing source-status semantics."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from typing import Iterator

import pytest

import frontend.evidence_review as evidence_review
import frontend.results as results_view
from frontend.evidence_review import _render_evidence_sections
from frontend.results import _render_source_statuses
from frontend.report_preview import render_draft_report_preview_pages
from frontend.source_status import (
    build_reviewer_source_status,
    build_reviewer_section_status,
)
from frontend.warning_semantics import build_warning_notices
from tests.test_pipeline import (
    TestStage40FrontendReviewWorkflow as _FrontendWorkflow,
)


@pytest.mark.parametrize(
    ("source", "capability", "status", "category", "message"),
    [
        (
            "NCBI ClinVar",
            "clinvar_evidence",
            "no_match",
            "Primary lookup: no exact match",
            "No exact ClinVar allele record was found by the primary lookup.",
        ),
        (
            "NCBI ClinVar",
            "clinvar_evidence",
            "not_found",
            "Primary lookup: no exact match",
            "No exact ClinVar allele record was found by the primary lookup.",
        ),
        (
            "ClinGen CSpec",
            "cspec_context",
            "no_match",
            "Expected absence",
            "No applicable ClinGen CSpec specification was identified for "
            "the current gene/disease scope.",
        ),
        (
            "Literature enrichment",
            "literature",
            "no_match",
            "Expected absence",
            "No qualifying variant-specific literature was found after the "
            "configured search strategy.",
        ),
        (
            "Phen2Gene",
            "phenotype_gene",
            "no_match",
            "Expected absence",
            "No supported phenotype association was identified.",
        ),
        (
            "Phenotype evidence",
            "phenotype_gene",
            "not_supported",
            "Expected absence",
            "No supported phenotype association was identified.",
        ),
    ],
)
def test_no_match_is_source_specific_for_reviewers(
    source: str,
    capability: str,
    status: str,
    category: str,
    message: str,
) -> None:
    presentation = build_reviewer_source_status(
        {
            "source": source,
            "capability": capability,
            "status": status,
            "operational_status": status,
            "provider_role": "primary",
            "fallback_used": False,
            "primary_failure": None,
            "method": "exact_lookup",
        }
    )

    assert presentation == {
        "category": category,
        "message": message,
        "recovery": None,
    }
    assert "no_match" not in " ".join(
        value for value in presentation.values() if value
    ).casefold()


def test_successful_fallback_names_the_actual_secondary_source() -> None:
    presentation = build_reviewer_source_status(
        {
            "source": "MyVariant.info",
            "capability": "clinvar_evidence",
            "status": "available",
            "operational_status": "success",
            "provider_role": "fallback",
            "fallback_used": True,
            "primary_failure": "timeout",
            "method": "myvariant_clinvar_derivation",
        }
    )

    assert presentation == {
        "category": "Evidence rescue completed",
        "message": "Secondary evidence was recovered from MyVariant.info.",
        "recovery": (
            "The primary ClinVar lookup was unavailable; the retained evidence "
            "is source-attributed to MyVariant.info."
        ),
    }


def test_operational_failure_and_unattempted_lookup_are_distinct() -> None:
    failed = build_reviewer_source_status(
        {
            "source": "NCBI ClinVar",
            "capability": "clinvar_evidence",
            "status": "unavailable",
            "operational_status": "timeout",
            "provider_role": "primary",
            "fallback_used": False,
            "primary_failure": None,
            "method": "exact_lookup",
        }
    )
    unattempted = build_reviewer_source_status(
        {
            "source": "Literature enrichment",
            "capability": "literature",
            "status": "not_assessed",
            "operational_status": "not_triggered",
            "provider_role": "primary",
            "fallback_used": False,
            "primary_failure": None,
            "method": "bounded_literature_search_chain",
        }
    )

    assert failed["category"] == "Provider unavailable"
    assert "could not complete" in failed["message"]
    assert unattempted == {
        "category": "Not queried",
        "message": (
            "Literature enrichment was not queried because its configured "
            "trigger conditions were not met."
        ),
        "recovery": None,
    }


def test_section_status_uses_matching_source_recovery_provenance() -> None:
    presentation = build_reviewer_section_status(
        source="NCBI ClinVar",
        status="available via fallback",
        data_sources=[
            {
                "source": "MyVariant.info",
                "capability": "clinvar_evidence",
                "status": "available",
                "operational_status": "success",
                "provider_role": "fallback",
                "fallback_used": True,
                "primary_failure": "timeout",
                "method": "myvariant_clinvar_derivation",
            }
        ],
    )

    assert presentation["category"] == "Evidence rescue completed"
    assert "MyVariant.info" in presentation["message"]


def test_primary_evidence_renderer_uses_reviewer_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels: list[str] = []
    captions: list[str] = []

    @contextmanager
    def fake_expander(label: str, **_kwargs: object) -> Iterator[None]:
        labels.append(label)
        yield

    monkeypatch.setattr(evidence_review.st, "expander", fake_expander)
    monkeypatch.setattr(evidence_review.st, "caption", captions.append)
    monkeypatch.setattr(evidence_review.st, "table", lambda *_args, **_kwargs: None)

    _render_evidence_sections(
        {
            "evidence_sections": [
                {
                    "source": "ClinGen CSpec",
                    "status": "no_match",
                    "items": [],
                }
            ],
            "data_sources": [
                {
                    "source": "ClinGen CSpec",
                    "capability": "cspec_context",
                    "status": "no_match",
                    "operational_status": "no_match",
                    "provider_role": "primary",
                    "fallback_used": False,
                    "primary_failure": None,
                    "method": "gene_disease_scope_lookup",
                }
            ],
        }
    )

    assert labels == ["ClinGen CSpec — Expected absence"]
    assert any("current gene/disease scope" in caption for caption in captions)
    assert "no_match" not in " ".join([*labels, *captions])


def test_legacy_source_metrics_do_not_render_generic_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def fake_container(**_kwargs: object) -> Iterator[None]:
        yield

    def fake_metric(label: str, value: str, **kwargs: object) -> None:
        metrics.append((label, value, kwargs))

    monkeypatch.setattr(results_view.st, "container", fake_container)
    monkeypatch.setattr(results_view.st, "metric", fake_metric)

    _render_source_statuses(
        {
            "source_statuses": {
                "vep": "success",
                "myvariant": "success",
                "clinvar": "no_match",
                "clingen": "success",
            }
        }
    )

    clinvar = next(metric for metric in metrics if metric[0] == "ClinVar")
    assert clinvar[1] == "Primary lookup: no exact match"
    assert "No exact ClinVar allele record" in str(clinvar[2]["help"])


def test_variant_notice_uses_cspec_scope_instead_of_generic_no_match() -> None:
    notices = build_warning_notices(
        {
            "variant_index": 0,
            "reviewed_report": {
                "variant_summary": {"display_label": "Variant 1"},
                "phenotype_context": {"phenotype_status": "supported"},
                "variant_interpretation": {
                    "status": "success",
                    "warnings": [],
                },
                "data_sources": [
                    {
                        "source": "ClinGen CSpec",
                        "capability": "cspec_context",
                        "status": "no_match",
                        "operational_status": "no_match",
                        "provider_role": "primary",
                        "fallback_used": False,
                    }
                ],
                "limitations": [],
            },
        }
    )

    assert notices == [
        {
            "severity": "INFO",
            "message": (
                "No applicable ClinGen CSpec specification was identified for "
                "the current gene/disease scope."
            ),
        }
    ]


def test_report_preview_replaces_raw_no_match_with_reviewer_wording() -> None:
    report = deepcopy(
        _FrontendWorkflow._draft_result()[
            "draft_variant_reports"
        ][0]
    )
    for content_key in ("machine_original_report", "reviewed_report"):
        content = report[content_key]
        clinvar_section = next(
            section
            for section in content["evidence_sections"]
            if section["source"] == "NCBI ClinVar"
        )
        clinvar_section["status"] = "no_match"
        clinvar_source = next(
            source
            for source in content["data_sources"]
            if source["capability"] == "clinvar_evidence"
        )
        clinvar_source["status"] = "no_match"
        clinvar_source["operational_status"] = "no_match"
    digest = hashlib.sha256(
        json.dumps(
            report["machine_original_report"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    report["report_id"] = f"dvr-0-{digest}"

    pages = render_draft_report_preview_pages(report)
    rendered = " ".join(pages)

    assert "no_match" not in rendered
    assert "Primary lookup: no exact match" in rendered
    assert "No exact ClinVar allele record was found" in rendered
