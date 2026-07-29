"""Tests for the clinical variant processing pipeline."""

import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from backend.annotation import (
    AnnotationError,
    annotate_variants,
)
from backend.prioritization import (
    PrioritizationError,
    prioritize_variants,
)
from backend.vcf_processing import (
    VCFProcessingError,
    normalize_vcf,
    parse_manual_variant,
    parse_vcf,
    process_vcf,
    validate_vcf,
)
from config import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MINIMAL_HEADER = (
    "##fileformat=VCFv4.2\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
)


class FakeResponse:
    """Small requests.Response substitute for offline annotation tests."""

    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class FakeSession:
    """Return queued responses or exceptions without network access."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)

        if isinstance(response, Exception):
            raise response

        assert isinstance(response, FakeResponse)
        return response

    def close(self) -> None:
        self.closed = True


def _write_vcf(
    tmp_path: Path,
    body: str,
    *,
    compressed: bool = False,
) -> Path:
    """Write a small test VCF."""
    suffix = ".vcf.gz" if compressed else ".vcf"
    path = tmp_path / f"sample{suffix}"
    content = MINIMAL_HEADER + body

    if compressed:
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(content)
    else:
        path.write_text(content, encoding="utf-8")

    return path


class TestVCFProcessing:
    def test_parse_vcf_splits_multiallelic_records(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\trs1\tA\tG,T\t99\tPASS\t.\n",
        )

        variants = parse_vcf(path)

        assert variants == [
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "G",
                "qual": 99.0,
                "filter": "PASS",
                "genotype": None,
            },
            {
                "chrom": "1",
                "pos": 100,
                "ref": "A",
                "alt": "T",
                "qual": 99.0,
                "filter": "PASS",
                "genotype": None,
            },
        ]

    def test_parse_compressed_vcf_and_extract_genotype(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
            "\tFORMAT\tPATIENT\n"
            "chr17\t43071077\t.\tA\tG\t.\tPASS\t.\tGT\t0/1\n",
            compressed=True,
        )

        variants = parse_vcf(path)

        assert variants == [
            {
                "chrom": "17",
                "pos": 43071077,
                "ref": "A",
                "alt": "G",
                "qual": None,
                "filter": "PASS",
                "genotype": "0/1",
            }
        ]

    def test_requested_sample_is_used(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO"
            "\tFORMAT\tSAMPLE_1\tSAMPLE_2\n"
            "2\t200\t.\tC\tT\t50\tPASS\t.\tGT\t0/0\t1|1\n",
        )

        variants = parse_vcf(path, sample_name="SAMPLE_2")

        assert variants[0]["genotype"] == "1|1"

    def test_missing_sample_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG\t.\t.\t.\n",
        )

        with pytest.raises(
            VCFProcessingError,
            match="has no sample columns",
        ):
            parse_vcf(path, sample_name="PATIENT")

    def test_max_variants_limits_streamed_output(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG,T\t.\t.\t.\n"
            "1\t200\t.\tC\tT\t.\t.\t.\n",
        )

        variants = parse_vcf(path, max_variants=2)

        assert len(variants) == 2
        assert [variant["alt"] for variant in variants] == ["G", "T"]

    def test_missing_file_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(
            VCFProcessingError,
            match="does not exist",
        ):
            validate_vcf(tmp_path / "missing.vcf")

    def test_invalid_extension_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "variants.txt"
        path.write_text("not a VCF", encoding="utf-8")

        with pytest.raises(
            VCFProcessingError,
            match=r"\.vcf or \.vcf\.gz",
        ):
            validate_vcf(path)

    def test_corrupt_vcf_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.vcf"
        path.write_text(
            "not a VCF header\nnot a VCF record\n",
            encoding="utf-8",
        )

        with pytest.raises(VCFProcessingError):
            parse_vcf(path)

    def test_manual_variant_is_standardized(self) -> None:
        variants = parse_manual_variant(
            "chrM:42:a:g,t",
            genotype="0/1",
        )

        assert variants == [
            {
                "chrom": "MT",
                "pos": 42,
                "ref": "A",
                "alt": "G",
                "qual": None,
                "filter": "PASS",
                "genotype": "0/1",
            },
            {
                "chrom": "MT",
                "pos": 42,
                "ref": "A",
                "alt": "T",
                "qual": None,
                "filter": "PASS",
                "genotype": "0/1",
            },
        ]

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "chr1:100:A",
            "chr1:not-a-position:A:G",
            "chr1:0:A:G",
            "chr1:100:?:G",
            "chr1:100:A:?",
        ],
    )
    def test_invalid_manual_variant_is_rejected(
        self,
        value: str,
    ) -> None:
        with pytest.raises(VCFProcessingError):
            parse_manual_variant(value)

    def test_process_vcf_requires_exactly_one_input(self) -> None:
        with pytest.raises(
            VCFProcessingError,
            match="exactly one",
        ):
            process_vcf()

        with pytest.raises(
            VCFProcessingError,
            match="exactly one",
        ):
            process_vcf(
                vcf_path="sample.vcf",
                manual_variant="1:100:A:G",
            )

    def test_process_vcf_returns_streaming_iterator(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG\t.\tPASS\t.\n",
        )

        result = process_vcf(vcf_path=path)

        assert not isinstance(result, list)
        assert list(result)[0]["alt"] == "G"

    def test_process_vcf_skips_normalization_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG\t.\tPASS\t.\n",
        )

        def unexpected_normalization(**_: object) -> Path:
            raise AssertionError("Normalization should be opt-in.")

        monkeypatch.setattr(
            "backend.vcf_processing.normalize_vcf",
            unexpected_normalization,
        )

        variants = list(process_vcf(vcf_path=path))

        assert variants[0]["pos"] == 100

    def test_enabled_normalization_requires_paths(
        self,
        tmp_path: Path,
    ) -> None:
        path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG\t.\tPASS\t.\n",
        )

        with pytest.raises(
            VCFProcessingError,
            match="required when normalization is enabled",
        ):
            process_vcf(vcf_path=path, normalize=True)

    def test_manual_input_rejects_bcftools_normalization(self) -> None:
        with pytest.raises(
            VCFProcessingError,
            match="only available for VCF file input",
        ):
            process_vcf(
                manual_variant="1:100:A:G",
                normalize=True,
            )

    def test_reference_sample_vcf_is_parsed(self) -> None:
        path = (
            PROJECT_ROOT
            / "data"
            / "samples"
            / "homo_sapiens_clinically_associated.vcf.gz"
        )

        variants = parse_vcf(path, max_variants=3)

        assert variants[0] == {
            "chrom": "1",
            "pos": 941284,
            "ref": "G",
            "alt": "A",
            "qual": None,
            "filter": None,
            "genotype": None,
        }
        assert len(variants) == 3

    def test_normalization_requires_bcftools(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vcf_path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG\t.\tPASS\t.\n",
        )
        reference_path = tmp_path / "reference.fa"
        reference_path.write_text(
            ">1\nACGT\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "backend.vcf_processing.shutil.which",
            lambda _: None,
        )

        with pytest.raises(
            VCFProcessingError,
            match="bcftools is not installed",
        ):
            normalize_vcf(
                vcf_path,
                tmp_path / "normalized.vcf.gz",
                reference_path,
            )

    def test_normalization_builds_safe_bcftools_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        vcf_path = _write_vcf(
            tmp_path,
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "1\t100\t.\tA\tG\t.\tPASS\t.\n",
        )
        reference_path = tmp_path / "reference.fa"
        reference_path.write_text(
            ">1\nACGT\n",
            encoding="utf-8",
        )
        output_path = tmp_path / "normalized.vcf.gz"

        monkeypatch.setattr(
            "backend.vcf_processing.shutil.which",
            lambda _: "bcftools",
        )

        def fake_run(
            command: list[str],
            **_: object,
        ) -> SimpleNamespace:
            assert command[:4] == [
                "bcftools",
                "norm",
                "--check-ref",
                "e",
            ]
            assert "--fasta-ref" in command
            assert "--multiallelics" in command
            assert "-any" in command
            output_path.write_bytes(b"normalized")
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(
            "backend.vcf_processing.subprocess.run",
            fake_run,
        )

        result = normalize_vcf(
            vcf_path,
            output_path,
            reference_path,
        )

        assert result == output_path.resolve()


class TestPrioritization:
    @staticmethod
    def _variant(
        position: int,
        *,
        filter_value: str | None = "PASS",
        quality: float | None = 50.0,
    ) -> dict[str, str | int | float | None]:
        """Build a standardized variant for prioritization tests."""
        return {
            "chrom": "1",
            "pos": position,
            "ref": "A",
            "alt": "G",
            "qual": quality,
            "filter": filter_value,
            "genotype": None,
        }

    def test_empty_input_returns_empty_list(self) -> None:
        assert prioritize_variants([], top_n=5, seed=1) == []

    def test_input_smaller_than_top_n_keeps_every_variant(self) -> None:
        variants = [
            self._variant(100),
            self._variant(200),
        ]

        candidates = prioritize_variants(
            variants,
            top_n=5,
            seed=7,
        )

        assert {item["pos"] for item in candidates} == {100, 200}

    def test_large_input_returns_exact_candidate_count(self) -> None:
        variants = [
            self._variant(position)
            for position in range(1, 101)
        ]

        candidates = prioritize_variants(
            variants,
            top_n=10,
            seed=42,
        )

        assert len(candidates) == 10
        assert len({item["pos"] for item in candidates}) == 10

    def test_same_seed_produces_same_random_selection(self) -> None:
        variants = [
            self._variant(position)
            for position in range(1, 51)
        ]

        first_result = prioritize_variants(
            variants,
            top_n=8,
            seed=123,
        )
        second_result = prioritize_variants(
            variants,
            top_n=8,
            seed=123,
        )

        assert first_result == second_result

    def test_default_candidate_count_comes_from_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "TOP_VARIANTS", 3)
        variants = (
            self._variant(position)
            for position in range(1, 20)
        )

        candidates = prioritize_variants(variants, seed=5)

        assert len(candidates) == 3

    def test_filter_and_missing_quality_do_not_affect_random_mvp(
        self,
    ) -> None:
        variants = [
            self._variant(
                100,
                filter_value="LowQual",
                quality=None,
            ),
            self._variant(200),
        ]

        candidates = prioritize_variants(
            variants,
            top_n=2,
            seed=1,
        )

        assert {item["pos"] for item in candidates} == {100, 200}

    @pytest.mark.parametrize("top_n", [0, -1, 1.5, True])
    def test_invalid_top_n_is_rejected(self, top_n: object) -> None:
        with pytest.raises(
            PrioritizationError,
            match="positive integer",
        ):
            prioritize_variants(
                [self._variant(100)],
                top_n=top_n,  # type: ignore[arg-type]
            )

    def test_missing_alt_field_is_rejected(self) -> None:
        variant = self._variant(100)
        del variant["alt"]

        with pytest.raises(
            PrioritizationError,
            match="missing: alt",
        ):
            prioritize_variants([variant], top_n=1)


class TestAnnotation:
    @staticmethod
    def _variant(
        position: int = 100,
    ) -> dict[str, str | int | float | None]:
        """Build a standardized candidate for VEP tests."""
        return {
            "chrom": "1",
            "pos": position,
            "ref": "A",
            "alt": "G",
            "qual": 50.0,
            "filter": "PASS",
            "genotype": "0/1",
        }

    @staticmethod
    def _vep_response(
        token: str = "cv_0",
        *,
        assembly: str = "GRCh38",
    ) -> dict[str, object]:
        """Build a representative Ensembl VEP response."""
        return {
            "input": f"1 100 {token} A G . . .",
            "assembly_name": assembly,
            "most_severe_consequence": "missense_variant",
            "transcript_consequences": [
                {
                    "gene_symbol": "GENE1",
                    "gene_id": "ENSG000001",
                    "transcript_id": "ENST000001",
                    "biotype": "protein_coding",
                    "consequence_terms": ["missense_variant"],
                    "impact": "MODERATE",
                    "hgvsc": "ENST000001:c.100A>G",
                    "hgvsp": "ENSP000001:p.Lys34Arg",
                    "canonical": 1,
                }
            ],
        }

    def test_successful_vep_response_is_standardized(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [self._vep_response()],
                )
            ]
        )

        annotations = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )

        assert len(annotations) == 1
        assert annotations[0]["assembly"] == "GRCh38"
        assert annotations[0]["gene"] == "GENE1"
        assert annotations[0]["transcript"] == "ENST000001"
        assert annotations[0]["consequence"] == "missense_variant"
        assert annotations[0]["impact"] == "MODERATE"
        assert annotations[0]["protein_change"] == "ENSP000001:p.Lys34Arg"
        assert annotations[0]["sources"]["vep"]["status"] == "success"
        assert "input" not in annotations[0]["sources"]["vep"]
        assert session.calls[0]["params"] == {
            "canonical": 1,
            "hgvs": 1,
            "pick_allele_gene": 1,
            "protein": 1,
            "mane": 1,
        }

    def test_multiple_candidates_use_one_batch_request(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [
                        self._vep_response("cv_0"),
                        self._vep_response("cv_1"),
                    ],
                )
            ]
        )

        annotations = annotate_variants(
            [self._variant(100), self._variant(200)],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )

        assert len(annotations) == 2
        assert len(session.calls) == 1
        request_json = session.calls[0]["json"]
        assert isinstance(request_json, dict)
        assert len(request_json["variants"]) == 2

    def test_batch_size_splits_requests(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [self._vep_response("cv_0")],
                ),
                FakeResponse(
                    200,
                    [self._vep_response("cv_1")],
                ),
            ]
        )

        annotations = annotate_variants(
            [self._variant(100), self._variant(200)],
            batch_size=1,
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )

        assert len(annotations) == 2
        assert len(session.calls) == 2

    def test_timeout_is_retried_then_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = FakeSession(
            [
                requests.Timeout("temporary timeout"),
                FakeResponse(
                    200,
                    [self._vep_response()],
                ),
            ]
        )
        delays: list[float] = []
        monkeypatch.setattr(
            "backend.annotation.time.sleep",
            delays.append,
        )

        annotations = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=1,
        )

        assert annotations[0]["sources"]["vep"]["status"] == "success"
        assert len(session.calls) == 2
        assert delays == [1.0]

    def test_exhausted_timeout_returns_structured_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = FakeSession(
            [
                requests.Timeout("temporary timeout"),
                requests.Timeout("final timeout"),
            ]
        )
        monkeypatch.setattr(
            "backend.annotation.time.sleep",
            lambda _: None,
        )

        annotations = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=1,
        )

        assert annotations[0]["sources"]["vep"]["status"] == "error"
        assert "request failed" in annotations[0]["warnings"][0]

    def test_http_error_does_not_stop_the_pipeline(self) -> None:
        session = FakeSession(
            [FakeResponse(400, {"error": "bad request"})]
        )

        annotations = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )

        assert len(annotations) == 1
        assert annotations[0]["sources"]["vep"]["status"] == "error"
        assert "HTTP 400" in annotations[0]["warnings"][0]

    def test_empty_vep_response_is_marked_not_found(self) -> None:
        session = FakeSession([FakeResponse(200, [])])

        annotations = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )

        assert annotations[0]["sources"]["vep"]["status"] == "not_found"
        assert annotations[0]["gene"] is None

    def test_assembly_mismatch_is_rejected(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [
                        self._vep_response(
                            assembly="GRCh37",
                        )
                    ],
                )
            ]
        )

        annotations = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )

        assert annotations[0]["sources"]["vep"]["status"] == "error"
        assert "assembly mismatch" in annotations[0]["warnings"][0]

    def test_invalid_batch_size_is_rejected(self) -> None:
        with pytest.raises(
            AnnotationError,
            match="between 1 and 200",
        ):
            annotate_variants([], batch_size=201)

    def test_missing_variant_field_is_rejected(self) -> None:
        variant = self._variant()
        del variant["alt"]

        with pytest.raises(
            AnnotationError,
            match="missing: alt",
        ):
            annotate_variants([variant], max_retries=0)
