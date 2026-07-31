"""Live smoke test for the production provider-neutral LLM boundary."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.database import get_analysis
from backend.llm import call_llm
from backend.pipeline import run_analysis
from backend.report import (
    CLINICAL_REPORT_SECTION_ORDER,
    generate_and_save_clinical_report,
    generate_clinical_interpretation,
    validate_and_sanitize_clinical_interpretation,
)
from config import settings


DEFAULT_PIPELINE_VARIANT = "1:941284:G:A"
DEFAULT_PIPELINE_PHENOTYPES = ["HP:0001250"]


def _synthetic_evidence_object() -> dict[str, object]:
    """Return non-identifying evidence for the live clinical smoke test."""

    return {
        "schema_version": "1.0",
        "variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
        },
        "assembly": "GRCh38",
        "gene": "SCN1A",
        "gene_id": "ENSG00000144285",
        "transcript": "ENST00000303395",
        "consequence": "missense_variant",
        "impact": "MODERATE",
        "protein_change": "ENSP00000303540:p.Arg1645Cys",
        "population_frequency": 0.00001,
        "clinvar_accession": "VCV000012345.1",
        "clinvar_significance": "Pathogenic",
        "clinvar_review_status": "reviewed by expert panel",
        "clinvar_conditions": [
            "Developmental and epileptic encephalopathy",
        ],
        "clingen_curations": [
            {
                "disease": (
                    "Developmental and epileptic encephalopathy"
                ),
                "disease_id": "MONDO:0100062",
                "classification": "Definitive",
                "mode_of_inheritance": "Autosomal dominant",
                "pmids": ["12345678"],
                "report_url": (
                    "https://search.clinicalgenome.org/"
                    "kb/gene-validity/example"
                ),
            }
        ],
        "phenotype_score": 0.5,
        "hpo_terms": [
            "HP:0001250",
            "HP:0001263",
        ],
        "matched_hpo_terms": ["HP:0001250"],
        "source_statuses": {
            "vep": "success",
            "myvariant": "success",
            "clinvar": "success",
            "clingen": "success",
        },
        "references": [
            {
                "source": "NCBI ClinVar",
                "url": (
                    "https://www.ncbi.nlm.nih.gov/"
                    "clinvar/variation/12345/"
                ),
            }
        ],
        "warnings": [
            "Synthetic evidence for software testing only.",
        ],
    }


def check_configuration() -> None:
    """Validate settings and create the configured application directories."""
    settings.initialize()

    print("Configuration: OK")
    print(f"App name: {settings.APP_NAME}")
    print(f"LLM provider protocol: {settings.LLM_PROVIDER}")
    print(f"LLM base URL: {settings.LLM_BASE_URL}")
    print(f"LLM model: {settings.LLM_MODEL}")
    print("LLM API key: configured")
    print(f"LLM timeout: {settings.LLM_TIMEOUT} seconds")
    print(f"Upload directory: {settings.UPLOAD_DIR}")
    print(f"Report directory: {settings.REPORT_DIR}")
    print(f"Cache directory: {settings.CACHE_DIR}")
    print(f"HPO data directory: {settings.HPO_DATA_DIR}")


def check_llm_connection() -> None:
    """Send one request through the production LLM client."""
    response = call_llm(
        "Follow the user's instruction exactly.",
        "Reply with exactly the word OK.",
        max_tokens=8,
        temperature=0.0,
    )

    print(f"LLM connection: OK ({response.content.strip()})")
    print(f"Response model: {response.model}")


def check_clinical_interpretation() -> None:
    """Generate and inspect one interpretation from synthetic evidence."""

    evidence = _synthetic_evidence_object()
    response = generate_clinical_interpretation(evidence)
    content = response.content.strip()
    validated = validate_and_sanitize_clinical_interpretation(
        response,
        evidence,
    )

    forbidden_expansions = {
        "family history",
        "seizure",
    }
    detected_expansions = {
        phrase
        for phrase in forbidden_expansions
        if phrase.casefold() in content.casefold()
    }
    if detected_expansions:
        raise RuntimeError(
            "Clinical interpretation added facts absent from the "
            "synthetic Evidence Object: "
            f"{', '.join(sorted(detected_expansions))}."
        )

    print("Clinical interpretation: OK")
    print(f"Response model: {validated['model']}")
    print("--- Interpretation output ---")
    print(content)
    print("--- End interpretation output ---")


def check_clinical_report() -> None:
    """Generate, validate, render, and save one synthetic live report."""

    report_path = generate_and_save_clinical_report(
        _synthetic_evidence_object()
    )
    markdown = report_path.read_text(encoding="utf-8")
    headings = [
        line.removeprefix("## ")
        for line in markdown.splitlines()
        if line.startswith("## ")
    ]
    expected_headings = [
        title
        for _, title in CLINICAL_REPORT_SECTION_ORDER
    ]
    if headings != expected_headings:
        raise RuntimeError(
            "Saved clinical report has an invalid section order."
        )

    forbidden_expansions = {
        "family history",
        "seizure",
    }
    detected_expansions = {
        phrase
        for phrase in forbidden_expansions
        if phrase.casefold() in markdown.casefold()
    }
    if detected_expansions:
        raise RuntimeError(
            "Saved clinical report added facts absent from the "
            "synthetic Evidence Object: "
            f"{', '.join(sorted(detected_expansions))}."
        )
    if report_path.parent != settings.REPORT_DIR.resolve():
        raise RuntimeError(
            "Saved clinical report escaped the configured report "
            "directory."
        )

    print("Clinical report: OK")
    print(f"Saved report: {report_path}")
    print(f"Saved bytes: {len(markdown.encode('utf-8'))}")
    print("--- Report output ---")
    print(markdown.rstrip())
    print("--- End report output ---")


def check_complete_pipeline(
    variant: str,
    phenotypes: list[str],
) -> None:
    """Run the production pipeline from manual input to saved report."""

    result = run_analysis(
        vcf_path=None,
        manual_variant=variant,
        phenotypes=phenotypes,
        top_n=1,
    )
    if result["status"] not in {"success", "partial"}:
        raise RuntimeError(
            "Pipeline stopped at "
            f"{result['current_stage']}: {result['errors']}"
        )
    if result["report_path"] is None:
        raise RuntimeError(
            "Pipeline completed without a saved clinical report: "
            f"{result['errors']}"
        )
    if result["analysis_id"] is None:
        raise RuntimeError(
            "Pipeline completed without a stored analysis ID."
        )

    report_path = Path(result["report_path"])
    if not report_path.is_file():
        raise RuntimeError(
            "Pipeline returned a report path that is not a file."
        )
    if len(result["evidence_objects"]) != 1:
        raise RuntimeError(
            "Pipeline did not retain exactly one Evidence Object."
        )
    if any(
        not issue["recoverable"]
        for issue in result["errors"]
    ):
        raise RuntimeError(
            f"Pipeline returned a fatal issue: {result['errors']}"
        )
    stored = get_analysis(result["analysis_id"])
    if stored["report_path"] != str(report_path.resolve()):
        raise RuntimeError(
            "Stored analysis does not reference the generated report."
        )
    if stored["evidence_objects"] != result["evidence_objects"]:
        raise RuntimeError(
            "Stored analysis did not preserve the Evidence Objects."
        )

    print(f"Pipeline status: {result['status']}")
    print(f"Analysis ID: {result['analysis_id']}")
    print(f"Variant: {variant}")
    print(f"HPO terms: {', '.join(phenotypes)}")
    for stage in result["stages"]:
        print(
            f"{stage['stage']}: {stage['status']} "
            f"({stage['progress_percent']}%)"
        )
    for warning in result["warnings"]:
        print(f"Warning: {warning}")
    print(f"Saved report: {report_path}")
    print(f"Saved bytes: {len(report_path.read_bytes())}")


def parse_arguments() -> argparse.Namespace:
    """Parse manual smoke-test options."""

    parser = argparse.ArgumentParser(
        description=(
            "Test configuration, basic LLM connectivity, or the live "
            "clinical interpretation path."
        )
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--config-only",
        action="store_true",
        help="Validate configuration without making an LLM request.",
    )
    modes.add_argument(
        "--clinical",
        action="store_true",
        help="Interpret a bundled synthetic Evidence Object.",
    )
    modes.add_argument(
        "--report",
        action="store_true",
        help="Generate and save a complete synthetic clinical report.",
    )
    modes.add_argument(
        "--pipeline",
        action="store_true",
        help=(
            "Run live annotation, HPO matching, LLM interpretation, "
            "report storage, and analysis persistence."
        ),
    )
    parser.add_argument(
        "--variant",
        default=DEFAULT_PIPELINE_VARIANT,
        help=(
            "Manual CHROM:POS:REF:ALT input for --pipeline "
            f"(default: {DEFAULT_PIPELINE_VARIANT})."
        ),
    )
    parser.add_argument(
        "--hpo",
        action="append",
        dest="phenotypes",
        help=(
            "HPO identifier for --pipeline; repeat for multiple terms "
            "(default: HP:0001250)."
        ),
    )
    return parser.parse_args()


def main() -> int:
    """Run configuration validation and one live LLM request."""
    arguments = parse_arguments()

    try:
        check_configuration()
        if arguments.pipeline:
            check_complete_pipeline(
                arguments.variant,
                (
                    arguments.phenotypes
                    or DEFAULT_PIPELINE_PHENOTYPES
                ),
            )
        elif arguments.report:
            check_clinical_report()
        elif arguments.clinical:
            check_clinical_interpretation()
        elif not arguments.config_only:
            check_llm_connection()
    except RuntimeError as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1

    print("Smoke test: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
