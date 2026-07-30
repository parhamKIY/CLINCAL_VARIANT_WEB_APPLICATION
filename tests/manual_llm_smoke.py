"""Live smoke test for the production provider-neutral LLM boundary."""

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm import call_llm
from backend.report import (
    generate_clinical_interpretation,
    validate_and_sanitize_clinical_interpretation,
)
from config import settings


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


def parse_arguments() -> argparse.Namespace:
    """Parse manual smoke-test options."""

    parser = argparse.ArgumentParser(
        description=(
            "Test configuration, basic LLM connectivity, or the live "
            "clinical interpretation path."
        )
    )
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="Validate configuration without making an LLM request.",
    )
    parser.add_argument(
        "--clinical",
        action="store_true",
        help="Interpret a bundled synthetic Evidence Object.",
    )
    return parser.parse_args()


def main() -> int:
    """Run configuration validation and one live LLM request."""
    arguments = parse_arguments()

    try:
        check_configuration()
        if arguments.clinical:
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
