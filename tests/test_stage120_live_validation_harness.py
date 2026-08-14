"""Stage 120 bounded live-provider validation harness checks."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from tests import run_live_provider_validation as live_command
from tools.live_provider_validation import ProviderTask, run_live_validation


pytestmark = pytest.mark.stage120_live_validation_harness


def test_hung_provider_is_bounded_by_overall_deadline(tmp_path: Path) -> None:
    output = tmp_path / "summary.json"
    release = threading.Event()
    closed = threading.Event()

    def hung() -> list[dict[str, object]]:
        release.wait(5)
        return []

    summary, exit_code = run_live_validation(
        [
            ProviderTask("hung", hung, close=closed.set),
            ProviderTask("later", lambda: []),
        ],
        output_path=output,
        overall_deadline_seconds=0.05,
        emit=lambda _line: None,
    )
    release.set()

    assert exit_code == 1
    assert summary["status"] == "overall_deadline_exceeded"
    assert summary["elapsed_seconds"] < 0.5
    assert closed.is_set()
    assert [item["status"] for item in summary["providers"]] == [
        "overall_deadline_exceeded",
        "overall_deadline_exceeded",
    ]
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == (
        "overall_deadline_exceeded"
    )


def test_earlier_results_are_checkpointed_and_sensitive_fields_are_absent(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    release = threading.Event()

    def earlier() -> list[dict[str, object]]:
        return [
            {
                "provider": "earlier_detail",
                "status": "success",
                "http_status": 200,
                "url": "https://token:SECRET@example.invalid",
                "raw_response": "SECRET_RESPONSE",
                "clinical_input": "1:941284:G:A",
            }
        ]

    def hung() -> list[dict[str, object]]:
        release.wait(5)
        return []

    summary, exit_code = run_live_validation(
        [ProviderTask("earlier", earlier), ProviderTask("hung", hung)],
        output_path=output,
        overall_deadline_seconds=0.05,
        emit=lambda _line: None,
    )
    release.set()
    payload = output.read_text(encoding="utf-8")

    assert exit_code == 1
    assert summary["checks"] == [
        {"provider": "earlier_detail", "status": "success", "http_status": 200}
    ]
    assert "SECRET" not in payload
    assert "https://" not in payload
    assert "1:941284:G:A" not in payload


def test_interruption_writes_a_valid_partial_summary(tmp_path: Path) -> None:
    output = tmp_path / "summary.json"

    def interrupted() -> list[dict[str, object]]:
        raise KeyboardInterrupt

    summary, exit_code = run_live_validation(
        [ProviderTask("interruptible", interrupted), ProviderTask("later", lambda: [])],
        output_path=output,
        overall_deadline_seconds=1,
        emit=lambda _line: None,
    )

    assert exit_code == 1
    assert summary["status"] == "cancelled"
    assert json.loads(output.read_text(encoding="utf-8")) == summary
    assert summary["providers"][0]["status"] == "unavailable"
    assert summary["providers"][1]["status"] == "not_started"


def test_complete_run_preserves_provider_order_and_progress_lines(tmp_path: Path) -> None:
    output = tmp_path / "summary.json"
    lines: list[str] = []

    summary, exit_code = run_live_validation(
        [
            ProviderTask("first", lambda: [{"status": "success"}]),
            ProviderTask("second", lambda: [{"status": "no_match"}]),
        ],
        output_path=output,
        overall_deadline_seconds=1,
        emit=lines.append,
    )

    assert exit_code == 0
    assert summary["status"] == "completed"
    assert [item["provider"] for item in summary["providers"]] == [
        "first",
        "second",
    ]
    assert [item["status"] for item in summary["providers"]] == [
        "completed",
        "completed",
    ]
    assert lines == [
        "Provider first: running",
        "Provider first: completed (1 checks)",
        "Provider second: running",
        "Provider second: completed (1 checks)",
    ]


def test_skip_llm_never_initializes_llm_tasks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.json"
    monkeypatch.setattr(live_command.settings, "initialize", lambda: None)
    monkeypatch.setattr(live_command, "_parse_variant", lambda _value: {})
    monkeypatch.setattr(
        live_command,
        "_annotation_checks",
        lambda _variant: ({}, [{"provider": "vep", "status": "success"}]),
    )
    monkeypatch.setattr(
        live_command,
        "_phenotype_checks",
        lambda candidate: (candidate, [{"provider": "phen2gene", "status": "success"}]),
    )
    monkeypatch.setattr(live_command, "_conditional_checks", lambda _candidate: [])
    monkeypatch.setattr(live_command, "_canonical_link_checks", lambda _candidate: [])
    monkeypatch.setattr(
        live_command,
        "_llm_checks",
        lambda _candidate: (_ for _ in ()).throw(AssertionError("LLM initialized")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_provider_validation.py",
            "--skip-llm",
            "--deadline-seconds",
            "1",
            "--output",
            str(output),
        ],
    )

    assert live_command.main() == 0
    providers = json.loads(output.read_text(encoding="utf-8"))["providers"]
    assert all(item["provider"] != "llm" for item in providers)
