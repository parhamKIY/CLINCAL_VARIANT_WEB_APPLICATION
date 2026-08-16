"""Run one bounded, checkpointed Stage 9 live provider query at a time.

This operational probe is deliberately separate from deterministic acceptance.
It writes only normalized, non-PHI summaries and never treats reachability as
evidence correctness.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.annotation import VepOperationalError, _post_vep_batch, _to_vep_input
from backend.conditional_enrichment import (
    fetch_literature_evidence,
    fetch_population_evidence_with_fallback,
)
from backend.erepo import retrieve_expert_curated_context
from backend.medgen import (
    enrich_with_medgen,
    enrich_with_medgen_gene_disease,
    enrich_with_medgen_phenotype_gene,
)
from backend.retrieval_intelligence import build_variant_identifier_bundle
from backend.variant_identity import normalized_genomic_hgvs
from backend.vcf_processing import parse_manual_variants
from config import settings


LIVE_CLASSES = (
    "erepo_exact_context",
    "medgen_gene_disease_support",
    "medgen_phenotype_gene_support",
    "medgen_disease_hpo_context",
    "vep_annotation",
    "population_retrieval",
    "literature_retrieval",
)
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "stage9-live-validation.json"
PUBLIC_VARIANT = {"chrom": "1", "pos": 941284, "ref": "G", "alt": "A"}
PUBLIC_HPO = {"hpo_id": "HP:0001250", "label": "Seizure"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "network_error"
    if isinstance(exc, VepOperationalError):
        return exc.provider_status
    return "unexpected_error"


def _result(
    query_class: str,
    *,
    operational_outcome: str,
    retrieval_state: str,
    accepted_evidence_count: int = 0,
    accepted_evidence_type: str | None = None,
    failure_category: str | None = None,
    attempts: int | None = None,
    note: str | None = None,
) -> dict[str, object]:
    """Return the sole persisted schema for a live observation."""

    return {
        "query_class": query_class,
        "checked_at": _now(),
        "operational_outcome": operational_outcome,
        "retrieval_state": retrieval_state,
        "accepted_evidence_count": accepted_evidence_count,
        "accepted_evidence_type": accepted_evidence_type,
        "failure_category": failure_category,
        "attempts": attempts,
        "latency_ms": None,
        "note": note,
    }


def _public_candidate() -> dict[str, object]:
    return {
        "variant": dict(PUBLIC_VARIANT),
        "assembly": settings.GENOME_ASSEMBLY,
        "gene": "SCN1A",
        "sources": {
            "myvariant": {"rsid": "rs121913529"},
            "clingen": {"status": "unavailable", "curations": []},
        },
        "phen2gene": {"availability": "unavailable", "hpo_terms": [PUBLIC_HPO["hpo_id"]]},
        "mydisease": {"status": "not_found", "diseases": []},
    }


def _not_run(query_class: str, note: str) -> dict[str, object]:
    return _result(
        query_class,
        operational_outcome="NOT_RUN",
        retrieval_state="unattempted",
        note=note,
    )


def _erepo_exact_context() -> dict[str, object]:
    if not settings.ENABLE_EREPO:
        return _not_run("erepo_exact_context", "ERepo is disabled by configuration.")
    candidate = _public_candidate()
    hgvs = normalized_genomic_hgvs(candidate["variant"], settings.GENOME_ASSEMBLY)
    if hgvs is None:
        return _not_run("erepo_exact_context", "Public probe identity was not normalizable.")
    bundle = build_variant_identifier_bundle(
        candidate,
        input_index=0,
        normalized_genomic_hgvs=hgvs[0],
    )
    with requests.Session() as session:
        context = retrieve_expert_curated_context(bundle, session=session)
    details = context["expert_curated_variant_context"]
    assert isinstance(details, dict)
    return _result(
        "erepo_exact_context",
        operational_outcome=str(context["status"]),
        retrieval_state=str(details["retrieval_state"]),
        accepted_evidence_count=len(details["records"]),
        accepted_evidence_type="expert_curated_variant_context",
        attempts=sum(int(item.get("attempts", 0)) for item in details["strategy_results"]),
    )


def _medgen_gene_disease_support() -> dict[str, object]:
    if not settings.ENABLE_MEDGEN:
        return _not_run("medgen_gene_disease_support", "MedGen is disabled by configuration.")
    result = enrich_with_medgen_gene_disease([_public_candidate()], enabled=True)
    context = result["variants"][0]["medgen_gene_disease_context"]
    return _result(
        "medgen_gene_disease_support",
        operational_outcome=str(context["status"]),
        retrieval_state=str(context["retrieval_state"]),
        accepted_evidence_count=len(context["records"]),
        accepted_evidence_type="gene_disease_support",
        attempts=int(context["attempts"]),
    )


def _medgen_phenotype_gene_support() -> dict[str, object]:
    if not settings.ENABLE_MEDGEN:
        return _not_run("medgen_phenotype_gene_support", "MedGen is disabled by configuration.")
    result = enrich_with_medgen_phenotype_gene(
        [_public_candidate()],
        enabled=True,
        accepted_hpo_terms=[PUBLIC_HPO],
    )
    contexts = result["variants"][0]["medgen_phenotype_gene_context"]
    count = sum(len(context["records"]) for context in contexts)
    state = contexts[0] if contexts else {"status": "not_applicable", "retrieval_state": "unattempted", "attempts": 0}
    return _result(
        "medgen_phenotype_gene_support",
        operational_outcome=str(state["status"]),
        retrieval_state=str(state["retrieval_state"]),
        accepted_evidence_count=count,
        accepted_evidence_type="phenotype_gene_support",
        attempts=int(state["attempts"]),
    )


def _medgen_disease_hpo_context() -> dict[str, object]:
    if not settings.ENABLE_MEDGEN:
        return _not_run("medgen_disease_hpo_context", "MedGen is disabled by configuration.")
    result = enrich_with_medgen(
        [_public_candidate()],
        enabled=True,
        accepted_hpo_terms=[PUBLIC_HPO],
    )
    context = result["variants"][0]["medgen_disease_hpo_context"]
    return _result(
        "medgen_disease_hpo_context",
        operational_outcome=str(context["status"]),
        retrieval_state=str(context["retrieval_state"]),
        accepted_evidence_count=len(context["records"]),
        accepted_evidence_type="disease_hpo_context",
        attempts=int(context["attempts"]),
    )


def _vep_annotation() -> dict[str, object]:
    variant = parse_manual_variants([
        {**PUBLIC_VARIANT, "qual": None, "filter": "PASS"}
    ])[0]
    try:
        with requests.Session() as session:
            response = _post_vep_batch(
                session,
                [_to_vep_input("stage9", variant)],
                max_retries=0,
            )
    except (requests.RequestException, VepOperationalError) as exc:
        return _result(
            "vep_annotation",
            operational_outcome="unavailable",
            retrieval_state="operational_failure",
            failure_category=_failure_category(exc),
            attempts=1,
        )
    return _result(
        "vep_annotation",
        operational_outcome="success",
        retrieval_state="accepted_records" if response else "no_match",
        accepted_evidence_count=len(response),
        accepted_evidence_type="annotation",
        attempts=1,
    )


def _population_retrieval() -> dict[str, object]:
    result = fetch_population_evidence_with_fallback(_public_candidate())
    status = str(result["status"])
    return _result(
        "population_retrieval",
        operational_outcome=status,
        retrieval_state=("accepted_records" if status in {"available", "partial"} else status),
        accepted_evidence_count=1 if result.get("global_maf") is not None else 0,
        accepted_evidence_type="population_evidence",
        failure_category=result.get("failure_reason") if isinstance(result.get("failure_reason"), str) else None,
        attempts=result.get("request_attempts") if isinstance(result.get("request_attempts"), int) else None,
    )


def _literature_retrieval() -> dict[str, object]:
    result = fetch_literature_evidence(_public_candidate())
    status = str(result["status"])
    return _result(
        "literature_retrieval",
        operational_outcome=status,
        retrieval_state=("accepted_records" if status in {"available", "partial"} else status),
        accepted_evidence_count=len(result["articles"]),
        accepted_evidence_type="literature_evidence",
        failure_category=result.get("failure_reason") if isinstance(result.get("failure_reason"), str) else None,
    )


CHECKS: dict[str, Callable[[], dict[str, object]]] = {
    "erepo_exact_context": _erepo_exact_context,
    "medgen_gene_disease_support": _medgen_gene_disease_support,
    "medgen_phenotype_gene_support": _medgen_phenotype_gene_support,
    "medgen_disease_hpo_context": _medgen_disease_hpo_context,
    "vep_annotation": _vep_annotation,
    "population_retrieval": _population_retrieval,
    "literature_retrieval": _literature_retrieval,
}


def _write_summary(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def run_selected_checks(
    selected: Iterable[str],
    output_path: Path,
    *,
    resume: bool = False,
    checks: dict[str, Callable[[], dict[str, object]]] | None = None,
) -> dict[str, object]:
    """Run each query class independently and checkpoint after every result."""

    active_checks = CHECKS if checks is None else checks
    names = list(selected)
    unknown = sorted(set(names) - set(active_checks))
    if unknown:
        raise ValueError(f"Unknown live query class: {', '.join(unknown)}")
    previous: list[dict[str, object]] = []
    if resume and output_path.exists():
        loaded = json.loads(output_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("checks"), list):
            previous = [item for item in loaded["checks"] if isinstance(item, dict)]
    completed = {item.get("query_class") for item in previous}
    summary: dict[str, object] = {
        "schema_version": "1.0",
        "generated_at": _now(),
        "deterministic_correctness": "not_evaluated_by_live_runner",
        "checks": previous,
    }
    records = summary["checks"]
    assert isinstance(records, list)
    for name in names:
        if name in completed:
            continue
        started_at = perf_counter()
        try:
            record = active_checks[name]()
        except Exception as exc:  # keep later classes runnable and summary safe
            record = _result(
                name,
                operational_outcome="unavailable",
                retrieval_state="operational_failure",
                failure_category=_failure_category(exc),
                note="Normalized operational failure; raw exception omitted.",
            )
        record["latency_ms"] = round((perf_counter() - started_at) * 1000, 1)
        records.append(record)
        _write_summary(output_path, summary)
    return summary


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bounded Stage 9 live provider checks.")
    parser.add_argument("--classes", default=",".join(LIVE_CLASSES), help="Comma-separated query classes.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true", help="Retain completed query-class checkpoints.")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    settings.initialize()
    selected = [item.strip() for item in arguments.classes.split(",") if item.strip()]
    summary = run_selected_checks(selected, arguments.output.resolve(), resume=arguments.resume)
    for item in summary["checks"]:
        assert isinstance(item, dict)
        print(f"{item['query_class']}: {item['operational_outcome']}")
    print(f"Summary: {arguments.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
