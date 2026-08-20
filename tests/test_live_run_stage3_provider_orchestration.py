"""Deterministic Stage 3 provider-orchestration acceptance tests."""

import json
from datetime import datetime, timezone

import backend.provider_readiness as readiness
import pytest
import requests

from backend.annotation import annotate_variants
from backend.pipeline import create_pipeline_result
from config import settings
import frontend.execution as frontend_execution
from tests.test_pipeline import (
    FakeResponse as _FakeResponse,
    FakeSession as _FakeSession,
    TestAnnotation as _AnnotationFixtures,
)


def _readiness_snapshot(
    provider: str,
    state: readiness.ReadinessState,
    *,
    checked_at: str | None = None,
    failure_category: str | None = None,
) -> readiness.ProviderReadinessSnapshot:
    timestamp = checked_at or (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    result = readiness.ProviderReadinessResult(
        provider=provider,
        state=state,
        dns_status="resolved",
        http_status=None if state == "unreachable" else 200,
        latency_ms=1.0,
        failure_category=(
            failure_category
            or ("timeout" if state == "unreachable" else "none")
        ),
        probe_kind="transport",
        checked_at=timestamp,
    )
    return readiness.build_provider_readiness_snapshot((result,))


def _variants(count: int = 4) -> list[dict[str, object]]:
    return [
        dict(_AnnotationFixtures._variant(100 + index))
        for index in range(count)
    ]


def _vep_payload(count: int = 4) -> list[dict[str, object]]:
    return [
        _AnnotationFixtures._vep_response(f"cv_{index}")
        for index in range(count)
    ]


def _variantvalidator_payloads(count: int = 4) -> list[_FakeResponse]:
    return [
        _FakeResponse(
            200,
            _AnnotationFixtures._variantvalidator_response(
                position=100 + index
            ),
        )
        for index in range(count)
    ]


def test_readiness_snapshot_classifies_fresh_and_stale_results() -> None:
    assert hasattr(readiness, "build_provider_readiness_snapshot")
    assert hasattr(readiness, "resolve_provider_readiness_hint")

    checked_at = "2026-08-20T08:00:00Z"
    result = readiness.ProviderReadinessResult(
        provider="vep",
        state="unreachable",
        dns_status="resolved",
        http_status=None,
        latency_ms=3_000.0,
        failure_category="timeout",
        probe_kind="transport",
        checked_at=checked_at,
    )
    snapshot = readiness.build_provider_readiness_snapshot((result,))

    fresh = readiness.resolve_provider_readiness_hint(
        snapshot,
        "vep",
        now=datetime(2026, 8, 20, 8, 4, 59, tzinfo=timezone.utc),
    )
    stale = readiness.resolve_provider_readiness_hint(
        snapshot,
        "vep",
        now=datetime(2026, 8, 20, 8, 5, 1, tzinfo=timezone.utc),
    )

    assert fresh.state == "fresh_unreachable"
    assert fresh.failure_category == "timeout"
    assert stale.state == "stale"
    assert stale.failure_category == "timeout"
    reachable = readiness.resolve_provider_readiness_hint(
        _readiness_snapshot("vep", "reachable"),
        "vep",
    )
    unknown = readiness.resolve_provider_readiness_hint(snapshot, "genebe")
    assert reachable.state == "fresh_reachable"
    assert unknown.state == "unknown"


def test_vep_fresh_unreachable_uses_one_short_confirmation_then_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [requests.Timeout("VEP unavailable") for _ in range(11)],
        variantvalidator_responses=_variantvalidator_payloads(),
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(),
        session=session,  # type: ignore[arg-type]
        batch_size=1,
        max_retries=2,
        readiness_snapshot=_readiness_snapshot("vep", "unreachable"),
    )

    assert len(session.post_calls) == 1
    assert session.post_calls[0]["timeout"] == (1.0, 3.0)
    assert len(session.variantvalidator_get_calls) == 4
    assert all(
        annotation["sources"]["vep"]["primary_failure"] == "timeout"
        and annotation["sources"]["vep"]["fallback_used"] is True
        and annotation["sources"]["vep"]["provider"] == "VariantValidator"
        and annotation["sources"]["vep"]["consequence_available"] is False
        for annotation in annotations
    )


def test_vep_stale_readiness_uses_normal_bounded_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [
            requests.Timeout("transient timeout"),
            _FakeResponse(200, _vep_payload(1)),
        ]
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
        readiness_snapshot=_readiness_snapshot(
            "vep",
            "unreachable",
            checked_at="2000-01-01T00:00:00Z",
        ),
    )[0]

    assert len(session.post_calls) == 2
    assert session.post_calls[0]["timeout"] == (
        min(5.0, settings.VEP_TIMEOUT),
        float(settings.VEP_TIMEOUT),
    )
    assert annotation["sources"]["vep"]["status"] == "success"
    assert session.variantvalidator_get_calls == []


def test_vep_fresh_reachable_uses_normal_primary_request() -> None:
    session = _FakeSession([_FakeResponse(200, _vep_payload(1))])

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
        readiness_snapshot=_readiness_snapshot("vep", "reachable"),
    )[0]

    assert len(session.post_calls) == 1
    assert session.post_calls[0]["timeout"] == (
        min(5.0, settings.VEP_TIMEOUT),
        float(settings.VEP_TIMEOUT),
    )
    assert annotation["sources"]["vep"]["provider"] == "Ensembl VEP"
    assert annotation["sources"]["vep"]["fallback_used"] is False


def test_vep_successful_fast_confirmation_consumes_unreachable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                [_AnnotationFixtures._vep_response("cv_0")],
            ),
            requests.Timeout("transient timeout after confirmation"),
            _FakeResponse(
                200,
                [_AnnotationFixtures._vep_response("cv_1")],
            ),
        ]
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        batch_size=1,
        max_retries=1,
        readiness_snapshot=_readiness_snapshot("vep", "unreachable"),
    )

    assert len(session.post_calls) == 3
    assert session.post_calls[0]["timeout"] == (1.0, 3.0)
    assert session.post_calls[1]["timeout"] == (
        min(5.0, settings.VEP_TIMEOUT),
        float(settings.VEP_TIMEOUT),
    )
    assert all(
        item["sources"]["vep"]["status"] == "success"
        and item["sources"]["vep"]["fallback_used"] is False
        for item in annotations
    )
    assert session.variantvalidator_get_calls == []


def test_vep_variant_rejection_is_isolated_without_opening_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [
            _FakeResponse(400, {"error": "batch contains rejected input"}),
            _FakeResponse(
                200,
                [_AnnotationFixtures._vep_response("cv_retry_0")],
            ),
            _FakeResponse(400, {"error": "variant rejected"}),
        ]
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
        readiness_snapshot=_readiness_snapshot("vep", "unreachable"),
    )

    assert len(session.post_calls) == 3
    assert session.post_calls[0]["timeout"] == (1.0, 3.0)
    assert session.post_calls[1]["timeout"] == (
        min(5.0, settings.VEP_TIMEOUT),
        float(settings.VEP_TIMEOUT),
    )
    assert annotations[0]["sources"]["vep"]["status"] == "success"
    rejected = annotations[1]["sources"]["vep"]
    assert rejected["status"] == "error"
    assert rejected["failure_scope"] == "variant_level"
    assert rejected["primary_failure"] is None
    assert rejected["fallback_used"] is False
    assert session.variantvalidator_get_calls == []


def test_vep_fixed_endpoint_404_is_provider_wide_and_does_not_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(404, {"error": "endpoint not found"})],
        variantvalidator_responses=_variantvalidator_payloads(2),
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
    )

    assert len(session.post_calls) == 1
    assert len(session.variantvalidator_get_calls) == 2
    assert all(
        item["sources"]["vep"]["failure_scope"] == "provider_wide"
        and item["sources"]["vep"]["primary_failure"] == "unavailable"
        and item["sources"]["vep"]["fallback_used"] is True
        for item in annotations
    )


def test_vep_provider_failure_during_isolation_starts_fallback_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    session = _FakeSession(
        [
            _FakeResponse(400, {"error": "batch contains rejected input"}),
            *[
                requests.Timeout("provider failed during isolated retry")
                for _ in range(3)
            ],
        ],
        variantvalidator_responses=_variantvalidator_payloads(2),
    )
    monkeypatch.setattr("backend.annotation.time.sleep", delays.append)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
    )

    assert len(session.post_calls) == 4
    assert delays == [1.0, 1.0, 2.0]
    assert len(session.variantvalidator_get_calls) == 2
    assert all(
        item["sources"]["vep"]["primary_failure"] == "timeout"
        and item["sources"]["vep"]["failure_scope"] == "provider_wide"
        and item["sources"]["vep"]["fallback_used"] is True
        for item in annotations
    )


def test_vep_transient_timeout_during_isolation_recovers_before_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    session = _FakeSession(
        [
            _FakeResponse(400, {"error": "variant rejected"}),
            requests.Timeout("transient isolated timeout"),
            _FakeResponse(
                200,
                [_AnnotationFixtures._vep_response("cv_retry_0")],
            ),
        ]
    )
    monkeypatch.setattr("backend.annotation.time.sleep", delays.append)

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )[0]

    assert len(session.post_calls) == 3
    assert delays == [1.0, 1.0]
    assert annotation["sources"]["vep"]["status"] == "success"
    assert annotation["sources"]["vep"]["fallback_used"] is False
    assert session.variantvalidator_get_calls == []


def test_vep_provider_wide_timeout_has_no_per_variant_retry_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [requests.Timeout("provider-wide timeout") for _ in range(11)],
        variantvalidator_responses=_variantvalidator_payloads(),
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(),
        session=session,  # type: ignore[arg-type]
        batch_size=1,
        max_retries=2,
    )

    assert len(session.post_calls) == 3
    assert len(session.variantvalidator_get_calls) == 4
    assert all(
        item["sources"]["vep"]["fallback_used"] is True
        for item in annotations
    )


def test_vep_valid_no_match_remains_terminal_without_fallback() -> None:
    session = _FakeSession([_FakeResponse(200, [])])

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
        readiness_snapshot=_readiness_snapshot("vep", "reachable"),
    )[0]

    assert len(session.post_calls) == 1
    assert session.variantvalidator_get_calls == []
    assert annotation["sources"]["vep"]["status"] == "not_found"
    assert annotation["sources"]["vep"]["primary_failure"] is None
    assert annotation["sources"]["vep"]["fallback_used"] is False


def test_genebe_fresh_unreachable_fast_fails_and_pipeline_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload())],
        genebe_responses=[
            requests.Timeout("GeneBe unavailable") for _ in range(11)
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
        readiness_snapshot=_readiness_snapshot("genebe", "unreachable"),
    )

    assert len(session.genebe_post_calls) == 1
    assert session.genebe_post_calls[0]["timeout"] == (1.0, 3.0)
    assert all(
        item["sources"]["genebe"]["status"] == "unavailable"
        and item["sources"]["genebe"]["primary_failure"] == "timeout"
        and item["sources"]["genebe"]["failure_scope"] == "provider_wide"
        and item["sources"]["genebe"]["automated_acmg_classification"]
        is None
        and item["sources"]["vep"]["status"] == "success"
        and item["sources"]["myvariant"]["status"] != "pending"
        for item in annotations
    )


def test_genebe_successful_fast_confirmation_consumes_unreachable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload(2))],
        genebe_responses=[
            _FakeResponse(
                200,
                {
                    "variants": [
                        _AnnotationFixtures._genebe_variant_response()
                    ]
                },
            ),
            requests.Timeout("transient timeout after confirmation"),
            _FakeResponse(
                200,
                {
                    "variants": [
                        _AnnotationFixtures._genebe_variant_response(
                            position=101
                        )
                    ]
                },
            ),
        ],
    )
    monkeypatch.setattr(
        "backend.annotation.MAX_GENEBE_BATCH_SIZE",
        1,
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
        readiness_snapshot=_readiness_snapshot("genebe", "unreachable"),
    )

    assert len(session.genebe_post_calls) == 3
    assert session.genebe_post_calls[0]["timeout"] == (1.0, 3.0)
    assert session.genebe_post_calls[1]["timeout"] == (
        min(5.0, settings.GENEBE_TIMEOUT),
        float(settings.GENEBE_TIMEOUT),
    )
    assert all(
        item["sources"]["genebe"]["status"] == "success"
        for item in annotations
    )


def test_genebe_provider_wide_timeout_has_no_per_variant_retry_storm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload())],
        genebe_responses=[
            requests.Timeout("provider-wide timeout") for _ in range(11)
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
    )

    assert len(session.genebe_post_calls) == 3
    assert all(
        item["sources"]["genebe"]["status"] == "unavailable"
        for item in annotations
    )


def test_genebe_variant_level_invalid_record_is_retried_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload(1))],
        genebe_responses=[
            _FakeResponse(200, {"variants": [{"chr": "1", "pos": 0}]}),
            _FakeResponse(
                200,
                {
                    "variants": [
                        _AnnotationFixtures._genebe_variant_response()
                    ]
                },
            ),
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )[0]

    assert len(session.genebe_post_calls) == 2
    assert annotation["sources"]["genebe"]["status"] == "success"
    assert annotation["sources"]["genebe"]["failure_scope"] is None


def test_genebe_variant_rejection_is_isolated_without_opening_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload(2))],
        genebe_responses=[
            _FakeResponse(400, {"error": "batch contains rejected input"}),
            _FakeResponse(
                200,
                {
                    "variants": [
                        _AnnotationFixtures._genebe_variant_response()
                    ]
                },
            ),
            _FakeResponse(400, {"error": "variant rejected"}),
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
        readiness_snapshot=_readiness_snapshot("genebe", "unreachable"),
    )

    assert len(session.genebe_post_calls) == 3
    assert session.genebe_post_calls[0]["timeout"] == (1.0, 3.0)
    assert session.genebe_post_calls[1]["timeout"] == (
        min(5.0, settings.GENEBE_TIMEOUT),
        float(settings.GENEBE_TIMEOUT),
    )
    assert annotations[0]["sources"]["genebe"]["status"] == "success"
    rejected = annotations[1]["sources"]["genebe"]
    assert rejected["status"] == "invalid_response"
    assert rejected["failure_scope"] == "variant_level"
    assert rejected["primary_failure"] is None


def test_genebe_fixed_endpoint_405_is_provider_wide_and_does_not_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload(2))],
        genebe_responses=[
            _FakeResponse(405, {"error": "method not allowed"})
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
    )

    assert len(session.genebe_post_calls) == 1
    assert all(
        item["sources"]["genebe"]["status"] == "unavailable"
        and item["sources"]["genebe"]["failure_scope"] == "provider_wide"
        and item["sources"]["genebe"]["primary_failure"] == "unavailable"
        for item in annotations
    )


def test_genebe_provider_failure_during_isolation_aborts_retry_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload(2))],
        genebe_responses=[
            _FakeResponse(
                200,
                {
                    "variants": [
                        {"chr": "1", "pos": 0},
                        {"chr": "1", "pos": 0},
                    ]
                },
            ),
            *[
                requests.Timeout("provider failed during isolated retry")
                for _ in range(3)
            ],
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", delays.append)

    annotations = annotate_variants(
        _variants(2),
        session=session,  # type: ignore[arg-type]
        max_retries=2,
    )

    assert len(session.genebe_post_calls) == 4
    assert delays == [1.0, 1.0, 2.0]
    assert all(
        item["sources"]["genebe"]["status"] == "unavailable"
        and item["sources"]["genebe"]["failure_scope"] == "provider_wide"
        and item["sources"]["genebe"]["primary_failure"] == "timeout"
        for item in annotations
    )


def test_genebe_transient_timeout_during_isolation_recovers_before_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    session = _FakeSession(
        [_FakeResponse(200, _vep_payload(1))],
        genebe_responses=[
            _FakeResponse(200, {"variants": [{"chr": "1", "pos": 0}]}),
            requests.Timeout("transient isolated timeout"),
            _FakeResponse(
                200,
                {
                    "variants": [
                        _AnnotationFixtures._genebe_variant_response()
                    ]
                },
            ),
        ],
    )
    monkeypatch.setattr("backend.annotation.time.sleep", delays.append)

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )[0]

    assert len(session.genebe_post_calls) == 3
    assert delays == [1.0, 1.0]
    assert annotation["sources"]["genebe"]["status"] == "success"
    assert annotation["sources"]["genebe"]["failure_scope"] is None


def test_provider_circuit_is_isolated_between_analysis_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.annotation.time.sleep", lambda _: None)
    call_counts: list[int] = []
    for _ in range(2):
        session = _FakeSession(
            [requests.Timeout("VEP unavailable")],
            variantvalidator_responses=_variantvalidator_payloads(1),
        )
        annotate_variants(
            _variants(1),
            session=session,  # type: ignore[arg-type]
            max_retries=2,
            readiness_snapshot=_readiness_snapshot("vep", "unreachable"),
        )
        call_counts.append(len(session.post_calls))

    assert call_counts == [1, 1]


def test_readiness_snapshot_is_not_stored_as_evidence() -> None:
    snapshot = _readiness_snapshot("vep", "reachable")
    session = _FakeSession([_FakeResponse(200, _vep_payload(1))])

    annotation = annotate_variants(
        _variants(1),
        session=session,  # type: ignore[arg-type]
        max_retries=0,
        readiness_snapshot=snapshot,
    )[0]

    serialized = json.dumps(annotation, sort_keys=True)
    assert snapshot.results[0].checked_at not in serialized
    assert "fresh_reachable" not in serialized


def test_frontend_execution_passes_snapshot_as_ephemeral_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _readiness_snapshot("vep", "unreachable")
    observed: dict[str, object] = {}

    def fake_run_analysis(**kwargs: object) -> object:
        observed.update(kwargs)
        return create_pipeline_result()

    monkeypatch.setattr(frontend_execution, "run_analysis", fake_run_analysis)

    frontend_execution.execute_analysis(
        uploaded_vcf=None,
        manual_variants=_variants(1),
        phenotypes=[],
        readiness_snapshot=snapshot,
    )

    assert observed["readiness_snapshot"] is snapshot
    assert "readiness_snapshot" not in create_pipeline_result()
