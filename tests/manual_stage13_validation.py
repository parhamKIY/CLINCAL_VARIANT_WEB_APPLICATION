"""Run the Stage 13 live manual-validation matrix."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TypedDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import get_analysis
from backend.pipeline import PipelineResult, run_analysis
from config import settings


DEFAULT_OUTPUT_PATH = (
    PROJECT_ROOT / "output" / "stage13-manual-validation.json"
)
PUBLIC_SAMPLE_VCF = (
    PROJECT_ROOT
    / "data"
    / "samples"
    / "mvp_demo.vcf"
)


class ManualCase(TypedDict):
    """One non-identifying live pipeline scenario."""

    name: str
    description: str
    vcf_path: Path | None
    manual_variants: list[dict[str, object]] | None
    phenotypes: list[str]


MANUAL_CASES: dict[str, ManualCase] = {
    "known-variant": {
        "name": "known-variant",
        "description": (
            "Known formatted variant with one seizure phenotype."
        ),
        "vcf_path": None,
        "manual_variants": [
            {
                "chrom": "1",
                "pos": 941284,
                "ref": "G",
                "alt": "A",
                "qual": None,
                "filter": "PASS",
            }
        ],
        "phenotypes": ["HP:0001250"],
    },
    "incomplete-phenotype": {
        "name": "incomplete-phenotype",
        "description": (
            "Known formatted variant without submitted HPO terms."
        ),
        "vcf_path": None,
        "manual_variants": [
            {
                "chrom": "1",
                "pos": 941284,
                "ref": "G",
                "alt": "A",
                "qual": None,
                "filter": "PASS",
            }
        ],
        "phenotypes": [],
    },
    "sample-vcf": {
        "name": "sample-vcf",
        "description": (
            "Professor-filtered public VCF table with one variant."
        ),
        "vcf_path": PUBLIC_SAMPLE_VCF,
        "manual_variants": None,
        "phenotypes": ["HP:0001250"],
    },
}


def _selected_cases(case_name: str) -> list[ManualCase]:
    """Resolve one case or the complete ordered validation matrix."""

    if case_name == "all":
        return list(MANUAL_CASES.values())
    return [MANUAL_CASES[case_name]]


def _validate_live_result(
    result: PipelineResult,
    *,
    allow_missing_report: bool,
) -> None:
    """Check the minimum manual acceptance criteria."""

    if result["status"] not in {"success", "partial"}:
        raise RuntimeError(
            "The pipeline returned a fatal terminal status at "
            f"{result['current_stage']}."
        )
    if any(
        not issue["recoverable"]
        for issue in result["errors"]
    ):
        raise RuntimeError(
            "The pipeline returned a non-recoverable issue."
        )
    if result["analysis_id"] is None:
        raise RuntimeError(
            "The pipeline did not persist the analysis."
        )
    if result["report_path"] is None and not allow_missing_report:
        raise RuntimeError(
            "The pipeline did not create a clinical report."
        )
    if (
        result["report_path"] is not None
        and not Path(result["report_path"]).is_file()
    ):
        raise RuntimeError(
            "The pipeline report path is not a readable file."
        )

    stored = get_analysis(result["analysis_id"])
    if stored["status"] != result["status"]:
        raise RuntimeError(
            "The retrieved analysis status differs from the result."
        )
    if stored["report_path"] != result["report_path"]:
        raise RuntimeError(
            "The retrieved analysis report differs from the result."
        )


def _run_case(
    case: ManualCase,
    *,
    allow_missing_report: bool,
) -> dict[str, object]:
    """Run and summarize one live case without storing raw patient data."""

    vcf_path = case["vcf_path"]
    if vcf_path is not None and not vcf_path.is_file():
        raise RuntimeError(
            f"Required public sample VCF is missing: {vcf_path}"
        )

    started_at = perf_counter()
    result = run_analysis(
        vcf_path=vcf_path,
        manual_variants=case["manual_variants"],
        phenotypes=case["phenotypes"],
    )
    elapsed_seconds = perf_counter() - started_at
    _validate_live_result(
        result,
        allow_missing_report=allow_missing_report,
    )

    return {
        "name": case["name"],
        "description": case["description"],
        "input_mode": (
            "vcf" if case["vcf_path"] is not None else "manual"
        ),
        "status": result["status"],
        "analysis_id": result["analysis_id"],
        "report_path": result["report_path"],
        "elapsed_seconds": round(elapsed_seconds, 3),
        "stages": [
            {
                "stage": stage["stage"],
                "status": stage["status"],
            }
            for stage in result["stages"]
        ],
        "warning_count": len(result["warnings"]),
        "error_count": len(result["errors"]),
    }


def _write_summary(
    summaries: list[dict[str, object]],
    output_path: Path,
    *,
    timeout_seconds: int | None,
) -> Path:
    """Write one non-identifying JSON validation record."""

    payload = {
        "generated_at": datetime.now(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "forced_timeout_seconds": timeout_seconds,
        "cases": summaries,
    }
    resolved_path = output_path.resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return resolved_path


def parse_arguments() -> argparse.Namespace:
    """Parse the live manual-validation options."""

    parser = argparse.ArgumentParser(
        description=(
            "Run representative live Stage 13 pipeline cases and save "
            "a non-identifying JSON summary."
        )
    )
    parser.add_argument(
        "--case",
        choices=(*MANUAL_CASES, "all"),
        default="known-variant",
        help="Validation case to run (default: known-variant).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        help=(
            "Temporarily use a small positive API and LLM timeout. "
            "Missing reports are accepted in this degraded mode."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Path for the non-identifying JSON summary.",
    )
    arguments = parser.parse_args()
    if (
        arguments.timeout_seconds is not None
        and arguments.timeout_seconds <= 0
    ):
        parser.error("--timeout-seconds must be greater than zero")
    return arguments


def main() -> int:
    """Execute the selected live cases and write their summary."""

    arguments = parse_arguments()
    settings.initialize()
    if arguments.timeout_seconds is not None:
        for name in (
            "REQUEST_TIMEOUT",
            "VEP_TIMEOUT",
            "GENEBE_TIMEOUT",
            "MYVARIANT_TIMEOUT",
            "CLINVAR_TIMEOUT",
            "CLINGEN_TIMEOUT",
            "CSPEC_TIMEOUT",
            "PHEN2GENE_TIMEOUT",
            "MYDISEASE_TIMEOUT",
            "CONDITIONAL_ENRICHMENT_TIMEOUT",
            "GNOMAD_TIMEOUT",
            "ENSEMBL_VARIATION_TIMEOUT",
            "LITVAR_TIMEOUT",
            "EUROPE_PMC_TIMEOUT",
            "PUBMED_TIMEOUT",
            "LLM_TIMEOUT",
        ):
            setattr(settings, name, arguments.timeout_seconds)

    summaries: list[dict[str, object]] = []
    try:
        for case in _selected_cases(arguments.case):
            print(f"Running case: {case['name']}")
            summary = _run_case(
                case,
                allow_missing_report=(
                    arguments.timeout_seconds is not None
                ),
            )
            summaries.append(summary)
            print(
                f"Result: {summary['status']} "
                f"({summary['elapsed_seconds']} seconds)"
            )
    except RuntimeError as exc:
        print(f"Manual validation failed: {exc}", file=sys.stderr)
        return 1

    output_path = _write_summary(
        summaries,
        arguments.output,
        timeout_seconds=arguments.timeout_seconds,
    )
    print(f"Validation summary: {output_path}")
    print("Stage 13 manual validation: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
