"""Run live connectivity checks for external annotation data sources."""

import argparse
import sys
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.annotation import annotate_variants
from backend.vcf_processing import VCFProcessingError, parse_manual_variants
from config import settings


DEFAULT_VARIANT = "1:941284:G:A"


def _parse_arguments() -> argparse.Namespace:
    """Read an optional CHROM:POS:REF:ALT value from the command line."""
    parser = argparse.ArgumentParser(
        description=(
            "Run one live VEP, GeneBe, MyVariant, ClinVar, ClinGen, "
            "and CSpec request."
        ),
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
    """Validate standardized evidence from all annotation sources."""
    try:
        chrom, pos, ref, alt = variant_text.split(":")
        position = int(pos)
    except (TypeError, ValueError) as exc:
        raise VCFProcessingError(
            "Variant must use CHROM:POS:REF:ALT format."
        ) from exc
    variants = parse_manual_variants(
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
    )
    started_at = perf_counter()
    annotations = annotate_variants(variants, batch_size=1)
    elapsed_seconds = perf_counter() - started_at

    if len(annotations) != 1:
        raise RuntimeError(
            "The annotation pipeline returned an unexpected result count."
        )

    annotation = annotations[0]
    vep_result = annotation["sources"]["vep"]
    genebe_result = annotation["sources"]["genebe"]
    myvariant_result = annotation["sources"]["myvariant"]
    clinvar_result = annotation["sources"]["clinvar"]
    clingen_result = annotation["sources"]["clingen"]
    cspec_result = annotation["sources"]["cspec"]

    print(f"VEP endpoint: {settings.VEP_BASE_URL}")
    print(f"ClinGen/GenCC endpoint: {settings.CLINGEN_BASE_URL}")
    print(f"ClinGen CSpec endpoint: {settings.CSPEC_BASE_URL}")
    print(f"Assembly: {settings.GENOME_ASSEMBLY}")
    print(f"Variant: {variant_text}")
    print(f"Response time: {elapsed_seconds:.2f} seconds")
    print(f"VEP status: {vep_result['status']}")
    print(f"GeneBe status: {genebe_result['status']}")
    print(f"MyVariant.info status: {myvariant_result['status']}")
    print(f"NCBI ClinVar status: {clinvar_result['status']}")
    print(f"ClinGen/GenCC status: {clingen_result['status']}")
    print(f"ClinGen CSpec status: {cspec_result['status']}")

    if vep_result["status"] != "success":
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(f"VEP annotation failed: {warning_text}")

    if genebe_result["status"] not in {"success", "not_found"}:
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(
            f"GeneBe annotation failed: {warning_text}"
        )

    if myvariant_result["status"] != "success":
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(
            f"MyVariant.info annotation failed: {warning_text}"
        )

    if clinvar_result["status"] != "success":
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(
            f"NCBI ClinVar annotation failed: {warning_text}"
        )

    if clingen_result["status"] not in {"success", "not_found"}:
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(
            f"ClinGen annotation failed: {warning_text}"
        )

    if cspec_result["status"] not in {"success", "not_found"}:
        warning_text = "; ".join(annotation["warnings"]) or "No details"
        raise RuntimeError(
            f"ClinGen CSpec lookup failed: {warning_text}"
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
    print(
        "ClinVar accession: "
        f"{clinvar_result['accession_version'] or 'not available'}"
    )
    print(
        "Clinical significance: "
        f"{clinvar_result['clinical_significance'] or 'not available'}"
    )
    print(
        "ClinVar review status: "
        f"{clinvar_result['review_status'] or 'not available'}"
    )
    print(f"ClinGen/GenCC query gene: {clingen_result['query_gene']}")
    print(
        "ClinGen/GenCC curation count: "
        f"{clingen_result['curation_count']}"
    )
    for curation in clingen_result["curations"]:
        print(
            "ClinGen relationship: "
            f"{clingen_result['gene']} - {curation['disease']} "
            f"({curation['classification']})"
        )
    print(
        "ClinGen CSpec specification count: "
        f"{cspec_result['specification_count']}"
    )
    for specification in cspec_result["specifications"]:
        print(
            "ClinGen CSpec guideline: "
            f"{specification['title']} "
            f"(version {specification['version']}, "
            f"scope {specification['scope_match']})"
        )


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
            (
                "VEP, GeneBe, MyVariant.info, ClinVar, ClinGen, "
                "and CSpec"
            ),
            lambda: check_production_annotation(arguments.variant),
        ),
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
