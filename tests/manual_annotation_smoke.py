"""Run live connectivity checks for external annotation data sources."""

import argparse
import sys
from pathlib import Path
from time import perf_counter

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.annotation import annotate_variants
from backend.vcf_processing import VCFProcessingError, parse_manual_variant
from config import settings


DEFAULT_VARIANT = "1:941284:G:A"
CLINGEN_API_URL = "https://ldh.clinicalgenome.org/ldh/srvc"


def _parse_arguments() -> argparse.Namespace:
    """Read an optional CHROM:POS:REF:ALT value from the command line."""
    parser = argparse.ArgumentParser(
        description="Run one live VEP and MyVariant annotation request.",
    )
    parser.add_argument(
        "variant",
        nargs="?",
        default=DEFAULT_VARIANT,
        help=(
            "Variant in CHROM:POS:REF:ALT format "
            f"(default: {DEFAULT_VARIANT})."
        ),
    )
    return parser.parse_args()


def check_production_annotation(variant_text: str) -> None:
    """Validate standardized VEP and MyVariant production evidence."""
    variants = parse_manual_variant(variant_text)
    started_at = perf_counter()
    annotations = annotate_variants(variants, batch_size=1)
    elapsed_seconds = perf_counter() - started_at

    if len(annotations) != 1:
        raise RuntimeError(
            "The annotation pipeline returned an unexpected result count."
        )

    annotation = annotations[0]
    vep_result = annotation["sources"]["vep"]
    myvariant_result = annotation["sources"]["myvariant"]

    print(f"Endpoint: {settings.VEP_BASE_URL}")
    print(f"Assembly: {settings.GENOME_ASSEMBLY}")
    print(f"Variant: {variant_text}")
    print(f"Response time: {elapsed_seconds:.2f} seconds")
    print(f"VEP status: {vep_result['status']}")
    print(f"MyVariant.info status: {myvariant_result['status']}")

    if vep_result["status"] != "success":
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(f"VEP annotation failed: {warning_text}")

    if myvariant_result["status"] != "success":
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(
            f"MyVariant.info annotation failed: {warning_text}"
        )

    # Print only the cleaned fields used by the application, not the raw API
    # response, so this smoke test exercises the real production data path.
    print(f"Gene: {annotation['gene'] or 'not available'}")
    print(f"Transcript: {annotation['transcript'] or 'not available'}")
    print(f"Consequence: {annotation['consequence'] or 'not available'}")
    print(f"Impact: {annotation['impact'] or 'not available'}")
    print(f"Protein change: {annotation['protein_change'] or 'not available'}")
    print(f"MyVariant.info ID: {myvariant_result['variant_id']}")
    print(f"dbSNP ID: {myvariant_result['rsid'] or 'not available'}")
    print(
        "Population frequency: "
        f"{annotation['population_frequency'] or 'not available'}"
    )


def check_clingen_connection() -> None:
    """Verify access to ClinGen's official Linked Data Hub API."""
    started_at = perf_counter()

    try:
        response = requests.get(
            CLINGEN_API_URL,
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"ClinGen request failed: {exc}"
        ) from exc
    except ValueError as exc:
        raise RuntimeError(
            "ClinGen returned invalid JSON."
        ) from exc

    elapsed_seconds = perf_counter() - started_at
    status = payload.get("status") if isinstance(payload, dict) else None
    data = payload.get("data") if isinstance(payload, dict) else None

    if (
        not isinstance(status, dict)
        or status.get("code") != 200
        or not isinstance(data, dict)
        or data.get("name") != "CG-LDH API"
    ):
        raise RuntimeError(
            "ClinGen returned an unexpected API response."
        )

    # This proves API connectivity only. The later ClinGen implementation
    # still needs to select and standardize the required evidence entities.
    print(f"Endpoint: {CLINGEN_API_URL}")
    print(f"Response time: {elapsed_seconds:.2f} seconds")
    print(f"ClinGen status: success (API version {data.get('version')})")


def main() -> int:
    """Validate configuration and check all currently selected sources."""
    arguments = _parse_arguments()

    try:
        settings.validate()
    except RuntimeError as exc:
        print(f"Configuration failed: {exc}", file=sys.stderr)
        return 1

    checks = (
        (
            "VEP and MyVariant.info",
            lambda: check_production_annotation(arguments.variant),
        ),
        ("ClinGen", check_clingen_connection),
    )
    failures: list[str] = []

    # Run every source even if one fails, so the output shows independent
    # connectivity results instead of hiding later services.
    for source_name, check in checks:
        print(f"\n--- {source_name} ---")
        try:
            check()
        except (RuntimeError, VCFProcessingError) as exc:
            failures.append(source_name)
            print(f"{source_name} check failed: {exc}", file=sys.stderr)

    if failures:
        print(
            "\nAnnotation smoke test: FAILED "
            f"({', '.join(failures)})",
            file=sys.stderr,
        )
        return 1

    print("\nAnnotation smoke test: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
