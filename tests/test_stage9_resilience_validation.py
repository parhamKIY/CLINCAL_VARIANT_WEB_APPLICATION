"""Stage 9 tests for the bounded live-validation checkpoint contract."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


RUNNER_PATH = Path(__file__).with_name("run_stage9_live_validation.py")
SPEC = importlib.util.spec_from_file_location("stage9_live_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_live_runner_checkpoints_each_isolated_result_and_resumes(
    tmp_path: Path,
) -> None:
    output = tmp_path / "stage9-live.json"

    def first() -> dict[str, object]:
        return runner._result(
            "first", operational_outcome="success", retrieval_state="accepted_records"
        )

    def failed() -> dict[str, object]:
        raise RuntimeError("sensitive provider response must not persist")

    checks = {"first": first, "failed": failed}
    first_summary = runner.run_selected_checks(["first", "failed"], output, checks=checks)
    saved = json.loads(output.read_text(encoding="utf-8"))

    assert [item["query_class"] for item in saved["checks"]] == ["first", "failed"]
    assert saved["checks"][1]["failure_category"] == "unexpected_error"
    assert "sensitive provider response" not in json.dumps(saved)
    assert len(first_summary["checks"]) == 2

    resumed = runner.run_selected_checks(["first"], output, resume=True, checks=checks)
    assert len(resumed["checks"]) == 2


def test_live_runner_rejects_unknown_query_class(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown live query class"):
        runner.run_selected_checks(["unknown"], tmp_path / "output.json", checks={})


def test_live_runner_records_disabled_configuration_as_not_run() -> None:
    record = runner._not_run("erepo_exact_context", "disabled")
    assert record["operational_outcome"] == "NOT_RUN"
    assert record["retrieval_state"] == "unattempted"
    assert record["accepted_evidence_count"] == 0


def test_vep_probe_supplies_a_complete_manual_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class _Session:
        def __enter__(self) -> _Session:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def parse(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        captured.extend(rows)
        return rows

    monkeypatch.setattr(runner, "parse_manual_variants", parse)
    monkeypatch.setattr(runner.requests, "Session", _Session)
    monkeypatch.setattr(runner, "_post_vep_batch", lambda *_args, **_kwargs: [])

    record = runner._vep_annotation()

    assert captured == [{**runner.PUBLIC_VARIANT, "qual": None, "filter": "PASS"}]
    assert record["operational_outcome"] == "success"
    assert record["retrieval_state"] == "no_match"
