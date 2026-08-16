"""Generate or verify the deterministic Stage 6A counterfactual audit."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.annotation import _standardize_vep_response
from backend.shadow_composition import compare_shadow_to_vep, compose_shadow_annotation
from tests.test_pipeline import TestAnnotation


AUDIT_PATH = PROJECT_ROOT / "docs" / "stage_6a_counterfactual_audit.md"
IDENTITY = {
    "assembly": "GRCh38",
    "chrom": "1",
    "pos": 100,
    "ref": "A",
    "alt": "G",
}
GAPS = {
    "gene": True,
    "transcript": True,
    "hgvs_c": True,
    "hgvs_p": True,
    "consequence": True,
}


def _fallback_cases() -> list[tuple[str, Mapping[str, object]]]:
    """Return independent normalized fallback fixtures before VEP is read."""

    return [
        (
            "approved_vep_success_exact_genebe",
            {
                "variantvalidator": {},
                "genebe": {
                    "identity": dict(IDENTITY),
                    "records": [
                        {
                            "gene": "GENE1",
                            "transcript": "ENST000001",
                            "hgvs_c": "ENST000001:c.100A>G",
                            "hgvs_p": "ENSP000001:p.Lys34Arg",
                            "consequence_terms": ["missense_variant"],
                        }
                    ],
                },
                "myvariant": {},
            },
        ),
        (
            "approved_vep_success_versioned_transcript_conflict",
            {
                "variantvalidator": {
                    "identity": dict(IDENTITY),
                    "gene": "GENE1",
                    "transcript": "NM_000001.5",
                    "hgvs_c": "NM_000001.5:c.100A>G",
                    "hgvs_p": "NP_000001.1:p.(Lys34Arg)",
                },
                "genebe": {
                    "identity": dict(IDENTITY),
                    "records": [
                        {
                            "gene": "GENE1",
                            "transcript": "NM_000001.6",
                            "hgvs_c": "NM_000001.6:c.100A>G",
                            "hgvs_p": "NP_000001.1:p.(Lys34Arg)",
                            "consequence_terms": ["missense_variant"],
                        }
                    ],
                },
                "myvariant": {},
            },
        ),
        (
            "approved_vep_success_raw_effect_only",
            {
                "variantvalidator": {},
                "genebe": {
                    "identity": dict(IDENTITY),
                    # A GeneBe raw `effect` is intentionally absent from the
                    # normalized composer input: it has no transcript-bound
                    # semantic contract and cannot become consequence evidence.
                    "records": [],
                },
                "myvariant": {},
            },
        ),
    ]


def _vep_comparator() -> dict[str, object]:
    """Read the approved VEP-success fixture only after shadow candidates freeze."""

    annotation = _standardize_vep_response(
        TestAnnotation._variant(),
        TestAnnotation._vep_response(),
    )
    return {
        "gene": annotation["gene"],
        "transcript": annotation["transcript"],
        "hgvs_c": annotation["hgvsc"],
        "hgvs_p": annotation["hgvsp"],
        "consequence": annotation["consequence"],
        "impact": annotation["impact"],
        "mane": annotation["mane_select"],
        "canonical": annotation["is_canonical"],
    }


def audit_rows() -> list[dict[str, object]]:
    """Construct, freeze, then compare all bounded counterfactual cases."""

    frozen = [
        (
            case_id,
            compose_shadow_annotation(
                identity_context=IDENTITY,
                vep_state="operational_failure",
                field_gaps=GAPS,
                fallback_evidence=fallback,
                evaluation_mode="counterfactual",
            ),
        )
        for case_id, fallback in _fallback_cases()
    ]
    comparator = _vep_comparator()
    rows: list[dict[str, object]] = []
    for case_id, shadow in frozen:
        for row in compare_shadow_to_vep(shadow, comparator):
            rows.append(
                {
                    "case_id": case_id,
                    "identity": "GRCh38:1:100:A>G",
                    **row,
                }
            )
    return rows


def _cell(value: object) -> str:
    if value is None:
        return "N/A"
    return str(value).replace("|", "\\|")


def render_audit() -> str:
    """Render a stable tracked artifact with raw counts and denominators."""

    rows = audit_rows()
    counts = Counter(str(row["classification"]) for row in rows)
    lines = [
        "# Stage 6A counterfactual audit",
        "",
        "This deterministic offline audit freezes fallback-only shadow candidates before introducing the approved VEP-success fixture comparator. It is observational only: no candidate is promoted, and percentages are intentionally omitted.",
        "",
        "- Fixture source: `tests.test_pipeline.TestAnnotation._vep_response` (approved pre-Stage-6 deterministic fixture).",
        "- Normalized identity: `GRCh38:1:100:A>G`.",
        "- Composer state: `operational_failure` / `counterfactual`.",
        "- Comparator order: fallback fixture -> shadow composer -> frozen candidate -> VEP fixture.",
        "",
        "## Raw field-by-field results",
        "",
        "| Case | Identity | Field | Shadow candidate | VEP comparator | Classification | Reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            _cell(row[key])
            for key in (
                "case_id",
                "identity",
                "field",
                "shadow_candidate",
                "vep_comparator",
                "classification",
                "reason",
            )
        )
        + " |"
        for row in rows
    )
    lines.extend(
        [
            "",
            "## Raw counts",
            "",
            "| Classification | Count | Denominator |",
            "| --- | ---: | ---: |",
            *(
                f"| {classification} | {counts[classification]} | {len(rows)} |"
                for classification in sorted(counts)
            ),
            "",
            "`SEMANTICALLY_EQUIVALENT` is not used because Stage 6A defines no new equivalence rule. `impact`, `mane`, and `canonical` are structurally `NOT_COMPOSED` by policy.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = render_audit()
    if args.write:
        AUDIT_PATH.write_text(content, encoding="utf-8")
    if args.check:
        if not AUDIT_PATH.is_file() or AUDIT_PATH.read_text(encoding="utf-8") != content:
            print("Stage 6A counterfactual audit is stale.")
            return 1
        print("Stage 6A counterfactual audit: PASS")
        return 0
    if not args.write:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
