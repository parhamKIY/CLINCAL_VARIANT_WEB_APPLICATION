"""Stage 115 record: final Word visual review remains reproducible and explicit."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "stage_83_fidelity_gate.md"


def test_stage115_records_word_renderer_matrix_and_review_boundary() -> None:
    contract = " ".join(CONTRACT.read_text(encoding="utf-8").split())

    required = {
        "2026-08-14",
        "Microsoft Word 16.0",
        "Poppler",
        "25 pages",
        "fully populated (4)",
        "sparse evidence (3)",
        "long interpretation (6)",
        "many references (5)",
        "no phenotype match (3)",
        "complex indel (4)",
        "no blank pages or page-edge overflow",
        "Codex document QA, not a clinical or laboratory sign-out",
    }

    assert all(value in contract for value in required)
