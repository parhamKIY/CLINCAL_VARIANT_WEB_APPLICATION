"""Evaluate completed Stage 91 model-review records without provider calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.interpretation_quality import (  # noqa: E402
    ModelQualityError,
    evaluate_interpretation_models,
)


def _load_input(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelQualityError(f"Cannot read evaluation input: {exc}") from exc
    if not isinstance(payload, dict):
        raise ModelQualityError("Evaluation input must be a JSON object.")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the Stage 91 evidence-based model selection gate."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        payload = _load_input(args.input)
        decision = evaluate_interpretation_models(
            payload.get("candidates", []),
            current_default_model=payload.get("current_default_model", ""),
        )
    except ModelQualityError as exc:
        parser.error(str(exc))

    rendered = json.dumps(decision, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
