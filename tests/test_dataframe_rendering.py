"""Regression tests for Streamlit dataframe rendering and PyArrow serialization."""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from frontend.results import build_variant_rows
from frontend.xlsx_selection import source_row_display_rows


def test_source_row_display_rows_with_mixed_alt_types_serializes_cleanly() -> None:
    """ALT containing both strings and integers must serialize to PyArrow without ArrowTypeError."""
    records = [
        {
            "row": 1,
            "chrom": "chr1",
            "start": 100000,
            "end": 100000,
            "ref": "A",
            "alt": "G",
            "qual": 99.5,
            "filter": "PASS",
            "depth": 50,
            "ad": 25,
            "gq": 99,
        },
        {
            "row": 2,
            "chrom": 2,  # Integer chromosome
            "start": 200000,
            "end": 200000,
            "ref": 0,  # ANNOVAR-like integer 0
            "alt": 0,  # ANNOVAR-like integer 0
            "qual": None,
            "filter": 0,  # Integer filter status
            "depth": None,
            "ad": None,
            "gq": None,
        },
        {
            "row": 3,
            "chrom": "chrX",
            "start": 300000,
            "end": 300000,
            "ref": "T",
            "alt": 1,  # Integer 1
            "qual": 45.0,
            "filter": "LowQual",
            "depth": 100,
            "ad": 50,
            "gq": 60,
        },
    ]

    rows = source_row_display_rows(records)

    # Check text fields normalized to strings or None
    assert rows[0]["ALT"] == "G"
    assert rows[1]["ALT"] == "0"
    assert rows[2]["ALT"] == "1"
    assert rows[1]["REF"] == "0"
    assert rows[1]["CHROM"] == "2"
    assert rows[1]["Source FILTER"] == "0"

    # Check numeric fields preserved
    assert rows[0]["Start"] == 100000
    assert rows[0]["QUAL"] == 99.5
    assert rows[0]["DP"] == 50
    assert rows[0]["AD"] == 25
    assert rows[0]["GQ"] == 99

    # Verify PyArrow serialization succeeds without ArrowTypeError
    df = pd.DataFrame(rows)
    table = pa.Table.from_pandas(df)
    assert table.num_rows == 3

    # Verify column types in PyArrow
    schema_types = {field.name: str(field.type) for field in table.schema}
    assert "string" in schema_types["ALT"].lower()
    assert "string" in schema_types["REF"].lower()
    assert "string" in schema_types["CHROM"].lower()
    assert "string" in schema_types["Source FILTER"].lower()
    assert "int" in schema_types["Start"].lower()
    assert "double" in schema_types["QUAL"].lower() or "float" in schema_types["QUAL"].lower()


def test_build_variant_rows_with_mixed_types_serializes_cleanly() -> None:
    """build_variant_rows handles integer ALT/REF/CHROM/FILTER safely for st.dataframe."""
    variants = [
        {
            "chrom": "1",
            "pos": 1000,
            "ref": "A",
            "alt": "T",
            "qual": 99.0,
            "filter": "PASS",
        },
        {
            "chrom": 2,  # integer chrom
            "pos": 2000,
            "ref": 0,  # integer ref
            "alt": 0,  # integer alt
            "qual": 30.5,
            "filter": 0,  # integer filter
        },
    ]

    rows = build_variant_rows(variants)

    assert rows[0]["Alternate"] == "T"
    assert rows[1]["Alternate"] == "0"
    assert rows[1]["Reference"] == "0"
    assert rows[1]["Chromosome"] == "2"
    assert rows[1]["Filter"] == "0"

    # Numeric fields preserved
    assert rows[1]["Position"] == 2000
    assert rows[1]["Quality"] == 30.5

    # PyArrow table conversion passes
    df = pd.DataFrame(rows)
    table = pa.Table.from_pandas(df)
    assert table.num_rows == 2
