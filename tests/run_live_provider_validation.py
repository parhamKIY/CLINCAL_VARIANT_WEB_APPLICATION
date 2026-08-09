"""Run bounded live contract checks for every configured external provider."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.annotation import annotate_variants
from backend.conditional_enrichment import (
    fetch_ensembl_population_evidence,
    fetch_gnomad_evidence,
    fetch_literature_evidence,
)
from backend.llm import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
    LLMValidationError,
)
from backend.mydisease import clear_mydisease_cache, enrich_with_mydisease
from backend.phenotype import clear_phen2gene_cache, enrich_with_phen2gene
from backend.phenotype_llm import extract_hpo_candidates
from backend.references import (
    build_canonical_references,
    validate_canonical_reference,
)
from backend.report import build_evidence_object
from backend.vcf_processing import parse_manual_variants
from backend.variant_interpretation import interpret_variant
from config import settings


DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "live-provider-validation.json"
DEFAULT_VARIANT = "1:941284:G:A"
DEFAULT_HPO = ["HP:0001250"]
ACCEPTED_NO_MATCH = {"success", "not_found"}
ACCEPTED_POPULATION = {
    "available",
    "partial",
    "no_match",
    "unavailable",
}
ACCEPTED_LITERATURE = {
    "available",
    "partial",
    "no_match",
    "unavailable",
}
SAFE_FAILURE_REASONS = {
    "forbidden",
    "http_error",
    "network_error",
    "rate_limited",
    "timeout",
    "upstream_error",
}


class SelectiveFailureSession(requests.Session):
    """Force selected fallbacks while preserving all other live requests."""

    def __init__(self, blocked_url_parts: tuple[str, ...]) -> None:
        super().__init__()
        self._blocked_url_parts = blocked_url_parts

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        if any(part in url for part in self._blocked_url_parts):
            raise requests.ConnectionError(
                "Intentional live-gate fallback trigger."
            )
        return super().request(method, url, **kwargs)


def _require_status(
    provider: str,
    status: object,
    allowed: set[str],
) -> str:
    if not isinstance(status, str) or status not in allowed:
        raise RuntimeError(
            f"{provider} returned unacceptable status "
            f"{status!r}; expected one of {sorted(allowed)}."
        )
    return status


def _require_safe_provider_state(
    provider: str,
    payload: object,
    allowed: set[str],
) -> str:
    """Accept usable data, valid missingness, or classified unavailability."""

    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider} returned no normalized evidence.")
    status = _require_status(provider, payload.get("status"), allowed)
    if status == "unavailable":
        reason = payload.get("failure_reason")
        if reason not in SAFE_FAILURE_REASONS:
            raise RuntimeError(
                f"{provider} returned unavailable without a safe "
                f"failure classification: {reason!r}."
            )
    return status


def _parse_variant(value: str) -> dict[str, object]:
    try:
        chrom, pos, ref, alt = value.split(":")
        position = int(pos)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Variant must use CHROM:POS:REF:ALT format."
        ) from exc
    return parse_manual_variants(
        [
            {
                "chrom": chrom,
                "pos": position,
                "ref": ref,
                "alt": alt,
                "qual": None,
                "filter": "PASS",
            }
        ]
    )[0]


def _annotation_checks(
    variant: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    annotations = annotate_variants([variant], batch_size=1)
    if len(annotations) != 1:
        raise RuntimeError(
            "Annotation returned an unexpected result count."
        )
    candidate = annotations[0]
    sources = candidate.get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError("Annotation returned no source map.")

    checks: list[dict[str, object]] = []
    requirements = {
        "vep": {"success", "unavailable"},
        "genebe": ACCEPTED_NO_MATCH | {"unavailable"},
        "myvariant": ACCEPTED_NO_MATCH,
        "clinvar": ACCEPTED_NO_MATCH | {"unavailable"},
        "clingen": ACCEPTED_NO_MATCH,
        "cspec": ACCEPTED_NO_MATCH | {"unavailable"},
    }
    for name, allowed in requirements.items():
        source = sources.get(name)
        if not isinstance(source, dict):
            raise RuntimeError(f"{name} returned no normalized evidence.")
        status = _require_safe_provider_state(name, source, allowed)
        checks.append(
            {
                "provider": name,
                "status": status,
                "provider_version": source.get("provider_version"),
                "failure_reason": source.get("failure_reason"),
            }
        )
    return candidate, checks


def _phenotype_checks(
    candidate: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    clear_phen2gene_cache()
    phen2gene = enrich_with_phen2gene(
        [candidate],
        DEFAULT_HPO,
        use_cache=False,
    )
    availability = _require_status(
        "phen2gene",
        phen2gene.get("availability"),
        {"available", "partial"},
    )
    enriched = phen2gene["variants"][0]

    clear_mydisease_cache()
    mydisease_candidate = {
        "variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
        },
        "gene": "SCN1A",
        "phen2gene": {
            "availability": "available",
            "score": 0.95,
        },
        "sources": {
            "genebe": {
                "status": "success",
                "gene": "SCN1A",
                "gene_hgnc_id": 10585,
            }
        },
    }
    mydisease = enrich_with_mydisease(
        [mydisease_candidate],
        DEFAULT_HPO,
    )
    mydisease_status = _require_status(
        "mydisease",
        mydisease.get("status"),
        {"available", "partial", "no_association"},
    )
    return enriched, [
        {
            "provider": "phen2gene",
            "status": availability,
            "request_attempts": phen2gene.get("request_attempts"),
        },
        {
            "provider": "mydisease",
            "status": mydisease_status,
            "request_attempts": mydisease.get("request_attempts"),
        },
    ]


def _conditional_checks(
    candidate: dict[str, object],
) -> list[dict[str, object]]:
    checks: list[dict[str, object]] = []

    gnomad = fetch_gnomad_evidence(candidate)
    checks.append(
        {
            "provider": "gnomad",
            "status": _require_safe_provider_state(
                "gnomad",
                gnomad,
                ACCEPTED_POPULATION,
            ),
            "http_status": gnomad.get("http_status"),
            "failure_reason": gnomad.get("failure_reason"),
        }
    )

    ensembl = fetch_ensembl_population_evidence(candidate)
    checks.append(
        {
            "provider": "ensembl_variation",
            "status": _require_safe_provider_state(
                "ensembl_variation",
                ensembl,
                ACCEPTED_POPULATION,
            ),
            "http_status": ensembl.get("http_status"),
            "failure_reason": ensembl.get("failure_reason"),
        }
    )

    litvar_result = fetch_literature_evidence(candidate)
    litvar = litvar_result["providers"]["litvar"]
    checks.append(
        {
            "provider": "litvar",
            "status": _require_safe_provider_state(
                "litvar",
                litvar,
                ACCEPTED_LITERATURE,
            ),
            "http_status": litvar.get("http_status"),
            "failure_reason": litvar.get("failure_reason"),
        }
    )

    with SelectiveFailureSession(
        ("ncbi.nlm.nih.gov/research/litvar2-api",)
    ) as session:
        europe_result = fetch_literature_evidence(
            candidate,
            session=session,
        )
    europe = europe_result["providers"]["europe_pmc"]
    checks.append(
        {
            "provider": "europe_pmc",
            "status": _require_safe_provider_state(
                "europe_pmc",
                europe,
                ACCEPTED_LITERATURE,
            ),
            "http_status": europe.get("http_status"),
            "failure_reason": europe.get("failure_reason"),
        }
    )

    with SelectiveFailureSession(
        (
            "ncbi.nlm.nih.gov/research/litvar2-api",
            "ebi.ac.uk/europepmc",
        )
    ) as session:
        pubmed_result = fetch_literature_evidence(
            candidate,
            session=session,
        )
    pubmed = pubmed_result["providers"]["pubmed"]
    checks.append(
        {
            "provider": "pubmed",
            "status": _require_safe_provider_state(
                "pubmed",
                pubmed,
                ACCEPTED_LITERATURE,
            ),
            "http_status": pubmed.get("http_status"),
            "failure_reason": pubmed.get("failure_reason"),
        }
    )
    return checks


def _canonical_link_checks(
    candidate: dict[str, object],
) -> list[dict[str, object]]:
    """Validate and probe a bounded set of exact report reference links."""

    evidence = build_evidence_object(candidate)
    references = build_canonical_references(evidence)
    linked = [
        validate_canonical_reference(reference)
        for reference in references
        if reference["canonical_url"] is not None
    ]
    if not linked:
        raise RuntimeError(
            "The live evidence produced no validated canonical links."
        )

    checks: list[dict[str, object]] = []
    seen_sources: set[str] = set()
    with requests.Session() as session:
        session.headers.update(
            {"User-Agent": "clinical-variant-stage61-validation/1.0"}
        )
        for reference in linked:
            source = reference["source"]
            if source in seen_sources:
                continue
            seen_sources.add(source)
            url = reference["canonical_url"]
            assert url is not None
            try:
                response = session.get(
                    url,
                    allow_redirects=False,
                    stream=True,
                    timeout=settings.REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                checks.append(
                    {
                        "provider": f"canonical_link:{source}",
                        "status": "unavailable",
                        "identifier": reference["identifier"],
                        "http_status": None,
                        "failure_reason": (
                            "timeout"
                            if isinstance(exc, requests.Timeout)
                            else "network_error"
                        ),
                    }
                )
            else:
                try:
                    status_code = response.status_code
                finally:
                    response.close()
                checks.append(
                    {
                        "provider": f"canonical_link:{source}",
                        "status": (
                            "reachable"
                            if 200 <= status_code < 400
                            else "unavailable"
                        ),
                        "identifier": reference["identifier"],
                        "http_status": status_code,
                        "failure_reason": (
                            None
                            if 200 <= status_code < 400
                            else (
                                "forbidden"
                                if status_code == 403
                                else "http_error"
                            )
                        ),
                    }
                )
            if len(checks) >= 5:
                break
    if not any(check["status"] == "reachable" for check in checks):
        raise RuntimeError(
            "No representative canonical reference link was reachable."
        )
    return checks


def _llm_checks(
    candidate: dict[str, object],
) -> list[dict[str, object]]:
    """Probe the two configured model roles through their V3 contracts."""

    def unavailable(
        provider: str,
        model: str,
        error: LLMError,
    ) -> dict[str, object]:
        if isinstance(
            error,
            (
                LLMAuthenticationError,
                LLMConfigurationError,
                LLMResponseError,
                LLMValidationError,
            ),
        ):
            raise error
        if isinstance(error, LLMTimeoutError):
            reason = "timeout"
        elif isinstance(error, LLMRateLimitError):
            reason = "rate_limited"
        elif isinstance(error, LLMRequestError):
            reason = "network_error"
        else:
            raise error
        return {
            "provider": provider,
            "status": "unavailable",
            "configured_model": model,
            "response_model": None,
            "failure_reason": reason,
            "error_type": type(error).__name__,
        }

    checks: list[dict[str, object]] = []
    try:
        phenotype = extract_hpo_candidates(
            "بیمار دچار تشنج است.",
            model=settings.PHENOTYPE_EXTRACTION_MODEL,
            max_retries=0,
        )
    except LLMError as exc:
        checks.append(
            unavailable(
                "phenotype_extraction_llm",
                settings.PHENOTYPE_EXTRACTION_MODEL,
                exc,
            )
        )
    else:
        checks.append(
            {
                "provider": "phenotype_extraction_llm",
                "status": "success",
                "configured_model": settings.PHENOTYPE_EXTRACTION_MODEL,
                "response_model": phenotype["model"],
                "candidate_count": len(phenotype["candidates"]),
                "failure_reason": None,
            }
        )

    evidence = build_evidence_object(candidate)
    try:
        interpretation = interpret_variant(
            evidence,
            model=settings.VARIANT_INTERPRETATION_MODEL,
            max_retries=0,
        )
    except LLMError as exc:
        checks.append(
            unavailable(
                "variant_interpretation_llm",
                settings.VARIANT_INTERPRETATION_MODEL,
                exc,
            )
        )
    else:
        checks.append(
            {
                "provider": "variant_interpretation_llm",
                "status": interpretation["status"],
                "configured_model": settings.VARIANT_INTERPRETATION_MODEL,
                "response_model": interpretation["response_model"],
                "prompt_mode": interpretation["prompt_mode"],
                "failure_reason": None,
            }
        )
    return checks


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded live schema/connectivity checks through the "
            "production provider clients."
        )
    )
    parser.add_argument(
        "--variant",
        default=DEFAULT_VARIANT,
        help=(
            "Public CHROM:POS:REF:ALT probe variant "
            f"(default: {DEFAULT_VARIANT})."
        ),
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip the two configured LLM model probes.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Write a non-clinical JSON status summary to this path.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    started_at = perf_counter()
    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "assembly": settings.GENOME_ASSEMBLY,
        "probe_variant": arguments.variant,
        "checks": [],
    }
    try:
        settings.initialize()
        variant = _parse_variant(arguments.variant)
        candidate, annotation_checks = _annotation_checks(variant)
        summary["checks"].extend(annotation_checks)
        candidate, phenotype_checks = _phenotype_checks(candidate)
        summary["checks"].extend(phenotype_checks)
        summary["checks"].extend(_conditional_checks(candidate))
        summary["checks"].extend(_canonical_link_checks(candidate))
        if not arguments.skip_llm:
            summary["checks"].extend(_llm_checks(candidate))
    except Exception as exc:
        summary["status"] = "failed"
        summary["failure"] = str(exc)
        exit_code = 1
    else:
        summary["status"] = "passed"
        summary["failure"] = None
        exit_code = 0

    summary["elapsed_seconds"] = round(perf_counter() - started_at, 3)
    output_path = arguments.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )

    for check in summary["checks"]:
        print(f"{check['provider']}: {check['status']}")
    print(f"Summary: {output_path}")
    print(
        "Live provider validation: "
        f"{str(summary['status']).upper()}"
    )
    if summary["failure"]:
        print(str(summary["failure"]), file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
