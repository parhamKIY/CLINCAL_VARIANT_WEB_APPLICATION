"""Tests for the clinical variant processing pipeline."""

import gzip
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.vcf_processing import (
    VCFProcessingError,
    normalize_vcf,
    parse_manual_variant,
    parse_vcf,
    process_vcf,
    validate_vcf,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MINIMAL_HEADER = (
    "##fileformat=VCFv4.2\n"
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
)


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
