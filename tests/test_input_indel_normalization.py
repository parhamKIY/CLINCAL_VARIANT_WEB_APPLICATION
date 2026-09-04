"""Stage 2B reference-aware ANNOVAR-like input adaptation."""

from __future__ import annotations

from backend.input_preprocessing import (
    adapt_annovar_like_record,
    classify_source_representation,
)
from backend.annotation import (
    _to_clinvar_identifiers,
    _to_myvariant_hgvs,
    _to_vep_input,
    _variantvalidator_match,
)
from backend.pipeline import run_annovar_like_input_processing
from backend.reference_sequence import fetch_grch38_reference_sequence
from backend.variant_identity import normalized_genomic_hgvs


def _reference(
    sequences: dict[tuple[str, int, int], str],
):
    def fetch(
        *, assembly: str, chrom: str, start: int, end: int
    ) -> dict[str, object]:
        value = sequences.get((chrom, start, end))
        if value is None:
            return {
                "status": "unavailable",
                "assembly": assembly,
                "chrom": chrom,
                "start": start,
                "end": end,
                "sequence": None,
                "source": "mock_grch38",
            }
        return {
            "status": "success",
            "assembly": assembly,
            "chrom": chrom,
            "start": start,
            "end": end,
            "sequence": value,
            "source": "mock_grch38",
        }

    return fetch


def test_red_zero_alt_is_not_a_canonical_symbolic_deletion() -> None:
    assert classify_source_representation("GTGC", 0) == "ANNOVAR_DELETION"

    adapted = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 100,
            "end": 103,
            "ref": "GTGC",
            "alt": 0,
        },
        reference_fetcher=_reference(
            {("13", 99, 99): "A", ("13", 100, 103): "GTGC"}
        ),
    )

    assert adapted["status"] == "NORMALIZED_AND_ACCEPTED"
    assert {
        key: adapted["canonical_variant"][key]
        for key in ("chrom", "pos", "ref", "alt")
    } == {
        "chrom": "13",
        "pos": 99,
        "ref": "AGTGC",
        "alt": "A",
    }


def test_red_zero_ref_becomes_only_a_reference_anchored_insertion() -> None:
    assert classify_source_representation(0, "ACT") == "ANNOVAR_INSERTION"

    adapted = adapt_annovar_like_record(
        {
            "chrom": "X",
            "start": 200,
            "end": 200,
            "ref": 0,
            "alt": "ACT",
        },
        reference_fetcher=_reference({("X", 200, 200): "G"}),
    )

    assert adapted["status"] == "NORMALIZED_AND_ACCEPTED"
    assert {
        key: adapted["canonical_variant"][key]
        for key in ("chrom", "pos", "ref", "alt")
    } == {
        "chrom": "X",
        "pos": 200,
        "ref": "G",
        "alt": "GACT",
    }


def test_single_base_zero_alt_uses_the_same_verified_deletion_contract() -> None:
    adapted = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 200,
            "end": 200,
            "ref": "C",
            "alt": 0,
            "filter": "QDfilter",
            "qual": 1.0,
        },
        reference_fetcher=_reference(
            {("13", 199, 199): "A", ("13", 200, 200): "C"}
        ),
    )

    assert adapted["status"] == "NORMALIZED_AND_ACCEPTED"
    assert adapted["canonical_variant"] == {
        "chrom": "13",
        "pos": 199,
        "ref": "AC",
        "alt": "A",
        "qual": 1.0,
        "filter": "QDfilter",
    }
    assert adapted["source_provenance"]["source_alt"] == "0"


def test_reference_mismatch_and_outage_remain_unresolved_and_distinct() -> None:
    mismatch = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 100,
            "end": 103,
            "ref": "GTGC",
            "alt": "0",
        },
        reference_fetcher=_reference(
            {("13", 99, 99): "A", ("13", 100, 103): "AAAA"}
        ),
    )
    unavailable = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 100,
            "end": 103,
            "ref": "GTGC",
            "alt": "0",
        },
        reference_fetcher=_reference({}),
    )

    assert mismatch["status"] == "IDENTITY_UNRESOLVED"
    assert mismatch["failure_reason"] == "REFERENCE_MISMATCH"
    assert unavailable["status"] == "IDENTITY_UNRESOLVED"
    assert unavailable["failure_reason"] == "REFERENCE_LOOKUP_UNAVAILABLE"


def test_invalid_or_ambiguous_source_coordinates_fail_closed() -> None:
    invalid_length = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 100,
            "end": 101,
            "ref": "GTGC",
            "alt": 0,
        },
        reference_fetcher=_reference({}),
    )
    ambiguous_insertion = adapt_annovar_like_record(
        {
            "chrom": "X",
            "start": 200,
            "end": 201,
            "ref": 0,
            "alt": "ACT",
        },
        reference_fetcher=_reference({}),
    )
    malformed = adapt_annovar_like_record(
        {
            "chrom": "1",
            "start": 100,
            "end": 100,
            "ref": "N",
            "alt": "T",
        },
        reference_fetcher=_reference({}),
    )

    assert invalid_length["failure_reason"] == "INVALID_INTERVAL"
    assert ambiguous_insertion["failure_reason"] == "INVALID_INTERVAL"
    assert malformed["failure_reason"] == "UNSUPPORTED_REPRESENTATION"


def test_normalized_indel_enters_existing_provider_handoff_as_canonical_cpra() -> None:
    result = run_annovar_like_input_processing(
        [
            {
                "chrom": "13",
                "start": 100,
                "end": 103,
                "ref": "GTGC",
                "alt": 0,
                "filter": "QDfilter",
                "qual": 1.0,
            },
            {
                "chrom": "1",
                "start": 200,
                "end": 200,
                "ref": "A",
                "alt": "G",
                "filter": "PASS",
                "qual": 1.0,
            },
            {
                "chrom": "13",
                "start": 300,
                "end": 303,
                "ref": "GTGC",
                "alt": 0,
            },
        ],
        phenotypes=[],
        reference_fetcher=_reference(
            {
                ("13", 99, 99): "A",
                ("13", 100, 103): "GTGC",
                ("1", 200, 200): "A",
                ("13", 299, 299): "A",
                ("13", 300, 303): "AAAA",
            }
        ),
    )

    assert result["variant_count"] == 2
    assert [item["status"] for item in result["input_preprocessing_results"]] == [
        "NORMALIZED_AND_ACCEPTED",
        "ACCEPTED_DIRECT",
        "IDENTITY_UNRESOLVED",
    ]
    request = _to_vep_input("input-000", result["variants"][0])
    assert request == "13 99 input-000 AGTGC A . . ."
    assert "<DEL>" not in request and " 0 " not in request
    assert result["input_preprocessing_results"][2]["canonical_variant"] is None


def test_all_reference_unavailable_inputs_remain_input_level_unresolved() -> None:
    result = run_annovar_like_input_processing(
        [
            {
                "chrom": "13",
                "start": 100,
                "end": 103,
                "ref": "GTGC",
                "alt": 0,
            }
        ],
        phenotypes=[],
        reference_fetcher=_reference({}),
    )

    assert result["variants"] == []
    assert result["errors"] == []
    assert result["status"] == "partial"
    assert result["current_stage"] == "completed"
    assert result["progress_percent"] == 100
    assert result["workflow_state"] == "completed"
    assert any("could be normalized" in w for w in result["warnings"])
    assert result["input_preprocessing_results"][0]["status"] == (
        "IDENTITY_UNRESOLVED"
    )
    assert result["input_preprocessing_results"][0]["failure_reason"] == (
        "REFERENCE_LOOKUP_UNAVAILABLE"
    )


def test_inconsistent_interval_inputs_complete_as_partial_with_clear_warning() -> None:
    records = [
        {"row": 12, "chrom": "14", "start": 61720574, "end": 61720584, "ref": "TGAGTTATT", "alt": "0"}
    ]
    result = run_annovar_like_input_processing(records, phenotypes=[])

    assert result["variants"] == []
    assert result["errors"] == []
    assert result["status"] == "partial"
    assert result["current_stage"] == "completed"
    assert result["progress_percent"] == 100
    assert result["workflow_state"] == "completed"
    assert any("could be normalized" in w for w in result["warnings"])
    assert result["input_preprocessing_results"][0]["status"] == "IDENTITY_UNRESOLVED"
    assert result["input_preprocessing_results"][0]["failure_reason"] == "INVALID_INTERVAL"


def test_normalized_deletion_has_a_canonical_hgvs_handoff() -> None:
    adapted = adapt_annovar_like_record(
        {
            "chrom": "13",
            "start": 100,
            "end": 103,
            "ref": "GTGC",
            "alt": 0,
        },
        reference_fetcher=_reference(
            {("13", 99, 99): "A", ("13", 100, 103): "GTGC"}
        ),
    )

    hgvs = normalized_genomic_hgvs(
        adapted["canonical_variant"],
        "GRCh38",
    )
    assert hgvs is not None
    assert hgvs[0] == "NC_000013.11:g.100_103del"


def test_existing_provider_identity_helpers_accept_the_canonical_deletion() -> None:
    variant = {
        "chrom": "13",
        "pos": 99,
        "ref": "AGTGC",
        "alt": "A",
        "qual": None,
        "filter": "PASS",
    }

    assert _to_myvariant_hgvs(variant) == "chr13:g.100_103del"
    clinvar = _to_clinvar_identifiers(variant)
    assert clinvar is not None
    assert clinvar[0] == "NC_000013.11:g.100_103del"
    assert clinvar[1] == "NC_000013.11:99:GTGC:"
    assert _variantvalidator_match(
        {
            "metadata": {},
            "flag": "gene_variant",
            "record": {
                "primary_assembly_loci": {
                    "grch38": {
                        "vcf": {
                            "chr": "13",
                            "pos": 99,
                            "ref": "AGTGC",
                            "alt": "A",
                        },
                        "hgvs_genomic_description": clinvar[0],
                    }
                }
            },
        },
        variant,
    ) is not None


def test_ten_selected_inputs_preserve_order_without_quality_or_filter_gating() -> None:
    records = [
        {
            "chrom": "1",
            "start": index,
            "end": index,
            "ref": "A",
            "alt": "G",
            "qual": 1.0 if index == 1 else 99.0,
            "filter": "QDfilter" if index == 2 else "PASS",
            "depth": 1 if index == 3 else 100,
            "ad": 1 if index == 4 else 50,
            "gq": 0 if index == 5 else 99,
        }
        for index in range(1, 11)
    ]

    result = run_annovar_like_input_processing(records, phenotypes=[])

    assert result["variant_count"] == 10
    assert [item["source_index"] for item in result["input_preprocessing_results"]] == list(
        range(10)
    )
    assert all(
        item["status"] == "ACCEPTED_DIRECT"
        for item in result["input_preprocessing_results"]
    )
    assert result["input_preprocessing_results"][1]["source_provenance"]["source_filter"] == "QDfilter"
    assert result["input_preprocessing_results"][0]["source_provenance"]["source_qual"] == 1.0


def test_reference_accessor_returns_explicit_bounded_operational_unavailability() -> None:
    class FailingSession:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get(self, _url: str, **kwargs: object) -> object:
            self.calls.append(dict(kwargs))
            raise __import__("requests").Timeout("unavailable")

    session = FailingSession()
    result = fetch_grch38_reference_sequence(
        assembly="GRCh38",
        chrom="chr13",
        start=100,
        end=100,
        session=session,  # type: ignore[arg-type]
    )

    assert result["status"] == "unavailable"
    assert result["source"] == "reference_sequence_unavailable"
    assert result["failure_reason"] == "REFERENCE_LOOKUP_UNAVAILABLE"
    assert len(session.calls) == 4
    assert all(call["timeout"] == 5 for call in session.calls)
