"""PyArrow-safe normalization for execution/provider trace tables."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

from frontend.technical_diagnostics import (
    build_provider_diagnostics_dataframe,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "provider": "ClinVar",
        "capability": "clinvar_evidence",
        "variant_identity": "GRCh38 2:100001 C>T",
        "status": "success",
        "retrieval_state": None,
        "attempt_count": 1,
        "latency_ms": 12.5,
        "fallback_used": False,
        "failure_category": "none",
        "provider_note": "ClinVar evidence was retained.",
    }
    row.update(overrides)
    return row


def test_trace_dataframe_normalizes_mixed_attempt_counts_for_pyarrow() -> None:
    rows = [
        _row(attempt_count=1),
        _row(provider="GeneBe", attempt_count="2"),
        _row(provider="MyVariant.info", attempt_count=None),
        {
            key: value
            for key, value in _row(provider="Phen2Gene").items()
            if key != "attempt_count"
        },
    ]

    dataframe = build_provider_diagnostics_dataframe(rows)
    arrow_table = pa.Table.from_pandas(dataframe, preserve_index=False)

    assert str(dataframe["attempt_count"].dtype) == "Int64"
    assert dataframe["attempt_count"].iloc[0] == 1
    assert dataframe["attempt_count"].iloc[1] == 2
    assert pd.isna(dataframe["attempt_count"].iloc[2])
    assert pd.isna(dataframe["attempt_count"].iloc[3])
    assert arrow_table.num_rows == 4


def test_trace_dataframe_normalizes_none_and_mixed_optional_values() -> None:
    dataframe = build_provider_diagnostics_dataframe(
        [
            _row(retrieval_state=None, latency_ms=None),
            _row(
                provider="GeneBe",
                retrieval_state="repository_cache",
                latency_ms="24.75",
                fallback_used="true",
            ),
        ]
    )
    arrow_table = pa.Table.from_pandas(dataframe, preserve_index=False)

    assert str(dataframe["latency_ms"].dtype) == "Float64"
    assert pd.isna(dataframe["latency_ms"].iloc[0])
    assert dataframe["latency_ms"].iloc[1] == 24.75
    assert str(dataframe["retrieval_state"].dtype) == "string"
    assert pd.isna(dataframe["retrieval_state"].iloc[0])
    assert dataframe["retrieval_state"].iloc[1] == "repository_cache"
    assert list(dataframe["fallback_used"]) == [False, True]
    assert arrow_table.num_rows == 2
