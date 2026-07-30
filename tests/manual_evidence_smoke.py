"""Run the complete local Stage 7 Evidence Object workflow."""

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.phenotype import HPODataError, PhenotypeError, match_phenotypes
from backend.report import (
    EvidenceObjectError,
    build_evidence_object,
    validate_evidence_object,
)


DEFAULT_GENE = "SCN1A"
DEFAULT_HPO_IDS = ["HP:0001250", "HP:0001263"]


def _parse_arguments() -> argparse.Namespace:
    """Read optional gene and phenotype inputs."""
    parser = argparse.ArgumentParser(
        description="Test the complete local Stage 7 evidence workflow.",
    )
    parser.add_argument(
        "--gene",
        default=DEFAULT_GENE,
        help=f"Candidate gene symbol (default: {DEFAULT_GENE}).",
    )
    parser.add_argument(
        "--hpo",
        action="append",
        dest="hpo_ids",
        help=(
            "Patient HPO ID; repeat for multiple terms "
            "(defaults: HP:0001250 and HP:0001263)."
        ),
    )
    return parser.parse_args()


def _candidate_annotation(gene: str) -> dict[str, object]:
    """Build one standardized annotation-shaped smoke-test candidate."""
    return {
        "variant": {
            "chrom": "2",
            "pos": 166848215,
            "ref": "C",
            "alt": "T",
            "qual": 99.0,
            "filter": "PASS",
            "genotype": "0/1",
        },
        "assembly": "GRCh38",
        "gene": gene,
        "gene_id": None,
        "transcript": None,
        "consequence": "missense_variant",
        "impact": "MODERATE",
        "protein_change": None,
        "population_frequency": None,
        "sources": {
            "vep": {"status": "success"},
            "myvariant": {"status": "not_found"},
            "clinvar": {
                "status": "not_found",
                "accession": None,
                "accession_version": None,
                "clinical_significance": None,
                "review_status": None,
                "conditions": [],
            },
            "clingen": {
                "status": "not_found",
                "curations": [],
            },
        },
        "references": [
            {
                "source": "Ensembl VEP",
                "url": (
                    "https://rest.ensembl.org/"
                    "vep/homo_sapiens/region"
                ),
            }
        ],
        "warnings": [
            "Smoke-test candidate uses local representative evidence."
        ],
        "raw_api_payload": {"must": "not be copied"},
    }


def check_stage_7(gene: str, hpo_ids: list[str]) -> None:
    """Score one candidate and build its bounded Evidence Object."""
    started_at = perf_counter()
    candidate = _candidate_annotation(gene)
    scored_candidate = match_phenotypes([candidate], hpo_ids)[0]
    evidence = build_evidence_object(scored_candidate)
    validate_evidence_object(evidence)
    serialized = json.dumps(
        evidence,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    elapsed_seconds = perf_counter() - started_at

    if "genotype" in evidence["variant"]:
        raise RuntimeError("Patient genotype leaked into the Evidence Object.")
    if "sources" in evidence or "raw_api_payload" in evidence:
        raise RuntimeError("Raw annotation data leaked into the evidence.")
    if json.loads(serialized) != evidence:
        raise RuntimeError("Evidence Object JSON round-trip failed.")

    variant = evidence["variant"]
    print(f"Schema version: {evidence['schema_version']}")
    print(
        "Variant: "
        f"{variant['chrom']}:{variant['pos']}:"
        f"{variant['ref']}:{variant['alt']}"
    )
    print(f"Assembly: {evidence['assembly']}")
    print(f"Gene: {evidence['gene'] or 'not available'}")
    print(
        "Source statuses: "
        + ", ".join(
            f"{source}={status}"
            for source, status in evidence["source_statuses"].items()
        )
    )
    print(
        "HPO terms: "
        + (", ".join(evidence["hpo_terms"]) or "not provided")
    )
    print(
        "Matched HPO terms: "
        + (
            ", ".join(evidence["matched_hpo_terms"])
            or "none"
        )
    )
    score = evidence["phenotype_score"]
    print(
        "Phenotype score: "
        + (f"{score:.4f}" if score is not None else "not available")
    )
    print(f"References: {len(evidence['references'])}")
    print(f"Warnings: {len(evidence['warnings'])}")
    print(f"Serialized size: {len(serialized.encode('utf-8'))} bytes")
    print("Genotype and raw payload excluded: yes")
    print(f"Response time: {elapsed_seconds:.2f} seconds")


def main() -> int:
    """Run the complete local Evidence Object smoke test."""
    arguments = _parse_arguments()
    hpo_ids = arguments.hpo_ids or DEFAULT_HPO_IDS

    try:
        check_stage_7(arguments.gene, hpo_ids)
    except (
        EvidenceObjectError,
        HPODataError,
        PhenotypeError,
        RuntimeError,
    ) as exc:
        print(
            f"\nEvidence Object smoke test: FAILED ({exc})",
            file=sys.stderr,
        )
        return 1

    print("\nEvidence Object smoke test: PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
