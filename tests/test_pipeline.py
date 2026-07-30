"""Tests for the clinical variant processing pipeline."""

import gzip
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from backend.annotation import (
    AnnotationError,
    annotate_variants,
)
from backend.phenotype import (
    HPODataError,
    PhenotypeError,
    get_diseases_for_hpo,
    get_genes_for_hpo,
    lookup_hpo_term,
    update_hpo_data,
    update_hpo_ontology,
    validate_hpo_id,
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


class FakeDownloadResponse:
    """Stream deterministic bytes for offline ontology update tests."""

    def __init__(
        self,
        status_code: int,
        content: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers or {
            "Content-Length": str(len(content)),
        }
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeDownloadSession:
    """Record one mocked HPO ontology download request."""

    def __init__(
        self,
        response: (
            FakeDownloadResponse
            | requests.RequestException
            | list[
                FakeDownloadResponse
                | requests.RequestException
            ]
        ),
    ) -> None:
        self.responses = (
            list(response)
            if isinstance(response, list)
            else [response]
        )
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeDownloadResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, requests.RequestException):
            raise response
        return response


class FakeSession:
    """Return queued responses or exceptions without network access."""

    def __init__(
        self,
        responses: list[object],
        *,
        get_responses: list[object] | None = None,
        clinvar_responses: list[object] | None = None,
        clingen_responses: list[object] | None = None,
    ) -> None:
        self.responses = list(responses)
        self.get_responses = list(get_responses or [])
        self.clinvar_responses = list(clinvar_responses or [])
        self.clingen_responses = list(clingen_responses or [])
        self.calls: list[dict[str, object]] = []
        self.post_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.myvariant_get_calls: list[dict[str, object]] = []
        self.clinvar_get_calls: list[dict[str, object]] = []
        self.clingen_get_calls: list[dict[str, object]] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        call = {"method": "POST", "url": url, **kwargs}
        self.calls.append(call)
        self.post_calls.append(call)
        response = self.responses.pop(0)

        if isinstance(response, Exception):
            raise response

        assert isinstance(response, FakeResponse)
        return response

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        call = {"method": "GET", "url": url, **kwargs}
        self.calls.append(call)
        self.get_calls.append(call)

        is_clingen = url == (
            f"{settings.CLINGEN_BASE_URL}/getData/track"
        )
        is_clinvar = url.endswith(
            ("/esearch.fcgi", "/esummary.fcgi")
        )
        if is_clingen:
            self.clingen_get_calls.append(call)
            if not self.clingen_responses:
                params = kwargs.get("params", {})
                assert isinstance(params, dict)
                return FakeResponse(
                    200,
                    {
                        "genome": params.get("genome", "hg38"),
                        "track": "genCC",
                        "chrom": params.get("chrom", "chr1"),
                        "genCC": [],
                        "itemsReturned": 0,
                    },
                )
            response = self.clingen_responses.pop(0)
        elif is_clinvar:
            self.clinvar_get_calls.append(call)
            if not self.clinvar_responses:
                return FakeResponse(
                    200,
                    {
                        "esearchresult": {
                            "count": "0",
                            "idlist": [],
                        }
                    },
                )
            response = self.clinvar_responses.pop(0)
        else:
            self.myvariant_get_calls.append(call)
            if not self.get_responses:
                return FakeResponse(404, {"error": "not found"})
            response = self.get_responses.pop(0)

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


class TestPhenotype:
    """Verify the Stage 6 phenotype input contract."""

    @staticmethod
    def _write_hpo_fixture(tmp_path: Path) -> Path:
        """Write a minimal ontology without requiring network access."""
        ontology_path = tmp_path / "hp.obo"
        ontology_path.write_text(
            (
                "format-version: 1.2\n"
                "\n"
                "[Term]\n"
                "id: HP:0001250\n"
                "name: Seizure\n"
                "alt_id: HP:0001275\n"
                "\n"
                "[Term]\n"
                "id: HP:0001263\n"
                "name: Global developmental delay\n"
                "\n"
                "[Term]\n"
                "id: HP:0009999\n"
                "name: obsolete Example phenotype\n"
                "is_obsolete: true\n"
            ),
            encoding="utf-8",
        )
        return ontology_path

    @staticmethod
    def _hpo_release(
        version: str,
        *,
        hpo_id: str = "HP:0001250",
        name: str = "Seizure",
    ) -> bytes:
        """Build a minimal versioned ontology download."""
        return (
            "format-version: 1.2\n"
            f"data-version: hp/releases/{version}\n"
            "\n"
            "[Term]\n"
            f"id: {hpo_id}\n"
            f"name: {name}\n"
        ).encode("utf-8")

    @staticmethod
    def _write_hpo_gene_fixture(tmp_path: Path) -> Path:
        """Write a minimal official-format phenotype-to-gene table."""
        associations_path = tmp_path / "phenotype_to_genes.txt"
        associations_path.write_text(
            (
                "hpo_id\thpo_name\tncbi_gene_id\tgene_symbol"
                "\tdisease_id\n"
                "HP:0001250\tSeizure\t6323\tSCN1A"
                "\tOMIM:607208\n"
                "HP:0001250\tSeizure\t6323\tSCN1A"
                "\tORPHA:33069\n"
                "HP:0001250\tSeizure\t6326\tSCN2A"
                "\tOMIM:613721\n"
            ),
            encoding="utf-8",
        )
        return associations_path

    @staticmethod
    def _hpo_gene_release(
        *,
        hpo_id: str = "HP:0001250",
        hpo_name: str = "Seizure",
        ncbi_gene_id: str = "6323",
        gene_symbol: str = "SCN1A",
    ) -> bytes:
        """Build a minimal phenotype-to-gene release asset."""
        return (
            "hpo_id\thpo_name\tncbi_gene_id\tgene_symbol"
            "\tdisease_id\n"
            f"{hpo_id}\t{hpo_name}\t{ncbi_gene_id}"
            f"\t{gene_symbol}\tOMIM:607208\n"
        ).encode("utf-8")

    @staticmethod
    def _write_hpo_disease_fixture(tmp_path: Path) -> Path:
        """Write a minimal official-format HPO disease table."""
        annotations_path = tmp_path / "phenotype.hpoa"
        header = (
            "database_id\tdisease_name\tqualifier\thpo_id"
            "\treference\tevidence\tonset\tfrequency\tsex"
            "\tmodifier\taspect\tbiocuration"
        )
        rows = [
            [
                "OMIM:607208",
                "Developmental and epileptic encephalopathy 6B",
                "",
                "HP:0001250",
                "PMID:12345678",
                "PCS",
                "",
                "",
                "",
                "",
                "P",
                "HPO:test[2026-01-01]",
            ],
            [
                "ORPHA:33069",
                "Dravet syndrome",
                "",
                "HP:0001250",
                "ORPHA:33069",
                "TAS",
                "",
                "",
                "",
                "",
                "P",
                "HPO:test[2026-01-01]",
            ],
            [
                "OMIM:607208",
                "Developmental and epileptic encephalopathy 6B",
                "",
                "HP:0001250",
                "OMIM:607208",
                "TAS",
                "",
                "",
                "",
                "",
                "P",
                "HPO:test[2026-01-02]",
            ],
            [
                "OMIM:607208",
                "DEE 6B",
                "",
                "HP:0001250",
                "OMIM:607208",
                "TAS",
                "",
                "",
                "",
                "",
                "P",
                "HPO:test[2025-01-01]",
            ],
            [
                "ORPHA:999999",
                "Negated example disease",
                "NOT",
                "HP:0001250",
                "ORPHA:999999",
                "TAS",
                "",
                "",
                "",
                "",
                "P",
                "HPO:test[2026-01-01]",
            ],
            [
                "OMIM:999999",
                "Inheritance-only example",
                "",
                "HP:0001250",
                "OMIM:999999",
                "TAS",
                "",
                "",
                "",
                "",
                "I",
                "HPO:test[2026-01-01]",
            ],
        ]
        annotations_path.write_text(
            "\n".join(
                (
                    '#version: "2026-01-01"',
                    header,
                    *("\t".join(row) for row in rows),
                    "",
                )
            ),
            encoding="utf-8",
        )
        return annotations_path

    @staticmethod
    def _hpo_disease_release(
        *,
        hpo_id: str = "HP:0001250",
        disease_id: str = "OMIM:607208",
        disease_name: str = (
            "Developmental and epileptic encephalopathy 6B"
        ),
    ) -> bytes:
        """Build a minimal HPO disease release asset."""
        return (
            "#version: 2026-01-01\n"
            "database_id\tdisease_name\tqualifier\thpo_id"
            "\treference\tevidence\tonset\tfrequency\tsex"
            "\tmodifier\taspect\tbiocuration\n"
            f"{disease_id}\t{disease_name}\t\t{hpo_id}"
            "\tPMID:12345678\tPCS\t\t\t\t\tP"
            "\tHPO:test[2026-01-01]\n"
        ).encode("utf-8")

    @pytest.mark.parametrize(
        ("hpo_id", "expected"),
        [
            ("HP:0001250", "HP:0001250"),
            ("  HP:0001263  ", "HP:0001263"),
        ],
    )
    def test_valid_hpo_id_is_returned_in_canonical_form(
        self,
        hpo_id: str,
        expected: str,
    ) -> None:
        assert validate_hpo_id(hpo_id) == expected

    @pytest.mark.parametrize(
        "hpo_id",
        [
            "",
            "   ",
            "hp:0001250",
            "HP:001250",
            "HP:00012500",
            "HP0001250",
            "HP:00012A0",
            "MP:0001250",
            None,
            1250,
        ],
    )
    def test_invalid_hpo_id_is_rejected(self, hpo_id: object) -> None:
        with pytest.raises(PhenotypeError):
            validate_hpo_id(hpo_id)  # type: ignore[arg-type]

    def test_existing_hpo_term_is_returned(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)

        assert lookup_hpo_term(
            "HP:0001250",
            ontology_path=ontology_path,
        ) == {
            "id": "HP:0001250",
            "name": "Seizure",
        }

    def test_alternate_hpo_id_resolves_to_canonical_term(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)

        assert lookup_hpo_term(
            "HP:0001275",
            ontology_path=ontology_path,
        ) == {
            "id": "HP:0001250",
            "name": "Seizure",
        }

    @pytest.mark.parametrize(
        "hpo_id",
        ["HP:0008888", "HP:0009999"],
    )
    def test_unknown_or_obsolete_hpo_term_is_rejected(
        self,
        tmp_path: Path,
        hpo_id: str,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)

        with pytest.raises(PhenotypeError, match="not found"):
            lookup_hpo_term(
                hpo_id,
                ontology_path=ontology_path,
            )

    def test_missing_hpo_ontology_is_reported(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(HPODataError, match="Unable to open"):
            lookup_hpo_term(
                "HP:0001250",
                ontology_path=tmp_path / "missing.obo",
            )

    def test_malformed_hpo_ontology_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = tmp_path / "hp.obo"
        ontology_path.write_text(
            "[Term]\nid: HP:0001250\n",
            encoding="utf-8",
        )

        with pytest.raises(HPODataError, match="has no name"):
            lookup_hpo_term(
                "HP:0001250",
                ontology_path=ontology_path,
            )

    def test_hpo_gene_lookup_is_deduplicated_and_sorted(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        associations_path = self._write_hpo_gene_fixture(tmp_path)

        assert get_genes_for_hpo(
            "HP:0001250",
            ontology_path=ontology_path,
            associations_path=associations_path,
        ) == {
            "hpo_term": {
                "id": "HP:0001250",
                "name": "Seizure",
            },
            "genes": ["SCN1A", "SCN2A"],
            "gene_count": 2,
        }

    def test_valid_hpo_without_gene_association_returns_empty_list(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        associations_path = self._write_hpo_gene_fixture(tmp_path)

        assert get_genes_for_hpo(
            "HP:0001263",
            ontology_path=ontology_path,
            associations_path=associations_path,
        ) == {
            "hpo_term": {
                "id": "HP:0001263",
                "name": "Global developmental delay",
            },
            "genes": [],
            "gene_count": 0,
        }

    def test_unknown_hpo_is_rejected_before_gene_lookup(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        associations_path = self._write_hpo_gene_fixture(tmp_path)

        with pytest.raises(PhenotypeError, match="not found"):
            get_genes_for_hpo(
                "HP:9999999",
                ontology_path=ontology_path,
                associations_path=associations_path,
            )

    def test_missing_hpo_gene_associations_are_reported(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)

        with pytest.raises(HPODataError, match="Unable to open"):
            get_genes_for_hpo(
                "HP:0001250",
                ontology_path=ontology_path,
                associations_path=tmp_path / "missing.txt",
            )

    @pytest.mark.parametrize(
        "content",
        [
            "unexpected\theader\n",
            (
                "hpo_id\thpo_name\tncbi_gene_id\tgene_symbol"
                "\tdisease_id\n"
                "HP:0001250\tSeizure\t6323\tSCN1A\n"
            ),
        ],
    )
    def test_malformed_hpo_gene_associations_are_rejected(
        self,
        tmp_path: Path,
        content: str,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        associations_path = tmp_path / "phenotype_to_genes.txt"
        associations_path.write_text(content, encoding="utf-8")

        with pytest.raises(HPODataError):
            get_genes_for_hpo(
                "HP:0001250",
                ontology_path=ontology_path,
                associations_path=associations_path,
            )

    def test_hpo_disease_lookup_excludes_negated_and_nonphenotypic_rows(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        annotations_path = self._write_hpo_disease_fixture(tmp_path)

        assert get_diseases_for_hpo(
            "HP:0001250",
            ontology_path=ontology_path,
            annotations_path=annotations_path,
        ) == {
            "hpo_term": {
                "id": "HP:0001250",
                "name": "Seizure",
            },
            "diseases": [
                {
                    "id": "OMIM:607208",
                    "name": (
                        "Developmental and epileptic "
                        "encephalopathy 6B"
                    ),
                },
                {
                    "id": "ORPHA:33069",
                    "name": "Dravet syndrome",
                },
            ],
            "disease_count": 2,
        }

    def test_valid_hpo_without_disease_association_returns_empty_list(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        annotations_path = self._write_hpo_disease_fixture(tmp_path)

        assert get_diseases_for_hpo(
            "HP:0001263",
            ontology_path=ontology_path,
            annotations_path=annotations_path,
        ) == {
            "hpo_term": {
                "id": "HP:0001263",
                "name": "Global developmental delay",
            },
            "diseases": [],
            "disease_count": 0,
        }

    def test_unknown_hpo_is_rejected_before_disease_lookup(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        annotations_path = self._write_hpo_disease_fixture(tmp_path)

        with pytest.raises(PhenotypeError, match="not found"):
            get_diseases_for_hpo(
                "HP:9999999",
                ontology_path=ontology_path,
                annotations_path=annotations_path,
            )

    def test_missing_hpo_disease_annotations_are_reported(
        self,
        tmp_path: Path,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)

        with pytest.raises(HPODataError, match="Unable to open"):
            get_diseases_for_hpo(
                "HP:0001250",
                ontology_path=ontology_path,
                annotations_path=tmp_path / "missing.hpoa",
            )

    @pytest.mark.parametrize(
        "content",
        [
            "unexpected\theader\n",
            (
                "database_id\tdisease_name\tqualifier\thpo_id"
                "\treference\tevidence\tonset\tfrequency\tsex"
                "\tmodifier\taspect\tbiocuration\n"
                "OMIM:607208\tExample disease\tMAYBE"
                "\tHP:0001250\tPMID:1\tPCS\t\t\t\t\tP"
                "\tHPO:test[2026-01-01]\n"
            ),
        ],
    )
    def test_malformed_hpo_disease_annotations_are_rejected(
        self,
        tmp_path: Path,
        content: str,
    ) -> None:
        ontology_path = self._write_hpo_fixture(tmp_path)
        annotations_path = tmp_path / "phenotype.hpoa"
        annotations_path.write_text(content, encoding="utf-8")

        with pytest.raises(HPODataError):
            get_diseases_for_hpo(
                "HP:0001250",
                ontology_path=ontology_path,
                annotations_path=annotations_path,
            )

    def test_hpo_update_installs_newer_valid_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        previous_content = self._hpo_release("2026-01-01")
        ontology_path.write_bytes(previous_content)
        assert lookup_hpo_term(
            "HP:0001250",
            ontology_path=ontology_path,
        )["name"] == "Seizure"

        response = FakeDownloadResponse(
            200,
            self._hpo_release(
                "2026-02-01",
                hpo_id="HP:0001263",
                name="Global developmental delay",
            ),
        )
        session = FakeDownloadSession(response)

        result = update_hpo_ontology(
            ontology_path=ontology_path,
            source_url="https://example.test/hp.obo",
            session=session,  # type: ignore[arg-type]
        )

        assert result == {
            "status": "updated",
            "previous_version": "hp/releases/2026-01-01",
            "current_version": "hp/releases/2026-02-01",
            "active_term_count": 1,
            "lookup_id_count": 1,
            "backup_path": str(
                ontology_path.with_suffix(".previous.obo")
            ),
        }
        assert ontology_path.with_suffix(
            ".previous.obo"
        ).read_bytes() == previous_content
        assert lookup_hpo_term(
            "HP:0001263",
            ontology_path=ontology_path,
        )["name"] == "Global developmental delay"
        with pytest.raises(PhenotypeError, match="not found"):
            lookup_hpo_term(
                "HP:0001250",
                ontology_path=ontology_path,
            )
        assert response.closed is True
        assert session.calls[0] == {
            "url": "https://example.test/hp.obo",
            "headers": {"Accept": "text/plain"},
            "stream": True,
            "timeout": settings.REQUEST_TIMEOUT,
        }

    def test_hpo_update_skips_installed_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        installed_content = self._hpo_release("2026-02-01")
        ontology_path.write_bytes(installed_content)
        session = FakeDownloadSession(
            FakeDownloadResponse(
                200,
                self._hpo_release(
                    "2026-02-01",
                    hpo_id="HP:0001263",
                    name="Global developmental delay",
                ),
            )
        )

        result = update_hpo_ontology(
            ontology_path=ontology_path,
            source_url="https://example.test/hp.obo",
            session=session,  # type: ignore[arg-type]
        )

        assert result["status"] == "unchanged"
        assert result["previous_version"] == (
            "hp/releases/2026-02-01"
        )
        assert ontology_path.read_bytes() == installed_content
        assert not ontology_path.with_suffix(".previous.obo").exists()

    @pytest.mark.parametrize(
        "response",
        [
            FakeDownloadResponse(503, b"service unavailable"),
            FakeDownloadResponse(
                200,
                (
                    b"format-version: 1.2\n"
                    b"data-version: hp/releases/2026-02-01\n"
                    b"\n[Term]\nid: HP:0001263\n"
                ),
            ),
        ],
    )
    def test_failed_hpo_update_preserves_installed_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        response: FakeDownloadResponse,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        installed_content = self._hpo_release("2026-01-01")
        ontology_path.write_bytes(installed_content)
        session = FakeDownloadSession(response)

        with pytest.raises(HPODataError):
            update_hpo_ontology(
                ontology_path=ontology_path,
                source_url="https://example.test/hp.obo",
                session=session,  # type: ignore[arg-type]
            )

        assert ontology_path.read_bytes() == installed_content
        assert not ontology_path.with_suffix(".previous.obo").exists()
        assert list(tmp_path.glob("*.download")) == []

    def test_hpo_update_rejects_release_downgrade(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        installed_content = self._hpo_release("2026-02-01")
        ontology_path.write_bytes(installed_content)
        session = FakeDownloadSession(
            FakeDownloadResponse(
                200,
                self._hpo_release("2026-01-01"),
            )
        )

        with pytest.raises(HPODataError, match="older"):
            update_hpo_ontology(
                ontology_path=ontology_path,
                source_url="https://example.test/hp.obo",
                session=session,  # type: ignore[arg-type]
            )

        assert ontology_path.read_bytes() == installed_content

    @pytest.mark.parametrize(
        "response",
        [
            requests.Timeout("temporary timeout"),
            FakeDownloadResponse(
                200,
                b"oversized",
                headers={"Content-Length": str(51 * 1024 * 1024)},
            ),
        ],
    )
    def test_hpo_download_failure_leaves_installed_file_unchanged(
        self,
        tmp_path: Path,
        response: FakeDownloadResponse | requests.RequestException,
    ) -> None:
        ontology_path = tmp_path / "hp.obo"
        installed_content = self._hpo_release("2026-02-01")
        ontology_path.write_bytes(installed_content)
        session = FakeDownloadSession(response)

        with pytest.raises(HPODataError):
            update_hpo_ontology(
                ontology_path=ontology_path,
                source_url="https://example.test/hp.obo",
                session=session,  # type: ignore[arg-type]
            )

        assert ontology_path.read_bytes() == installed_content
        assert list(tmp_path.glob("*.download")) == []

    def test_hpo_update_requires_https(self, tmp_path: Path) -> None:
        with pytest.raises(HPODataError, match="HTTPS"):
            update_hpo_ontology(
                ontology_path=tmp_path / "hp.obo",
                source_url="http://example.test/hp.obo",
            )

    def test_coordinated_hpo_data_update_installs_matching_release(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_GENE_ASSOCIATION_TERMS",
            1,
        )
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_DISEASE_ANNOTATION_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        associations_path = tmp_path / "phenotype_to_genes.txt"
        disease_annotations_path = tmp_path / "phenotype.hpoa"
        old_ontology = self._hpo_release("2026-01-01")
        old_associations = self._hpo_gene_release()
        old_disease_annotations = self._hpo_disease_release()
        ontology_path.write_bytes(old_ontology)
        associations_path.write_bytes(old_associations)
        disease_annotations_path.write_bytes(old_disease_annotations)
        assert get_genes_for_hpo(
            "HP:0001250",
            ontology_path=ontology_path,
            associations_path=associations_path,
        )["genes"] == ["SCN1A"]
        assert get_diseases_for_hpo(
            "HP:0001250",
            ontology_path=ontology_path,
            annotations_path=disease_annotations_path,
        )["disease_count"] == 1

        new_ontology = self._hpo_release(
            "2026-02-01",
            hpo_id="HP:0001263",
            name="Global developmental delay",
        )
        new_associations = self._hpo_gene_release(
            hpo_id="HP:0001263",
            hpo_name="Global developmental delay",
            ncbi_gene_id="1654",
            gene_symbol="DDX3X",
        )
        new_disease_annotations = self._hpo_disease_release(
            hpo_id="HP:0001263",
            disease_id="OMIM:300958",
            disease_name="Snijders Blok-Campeau syndrome",
        )
        session = FakeDownloadSession(
            [
                FakeDownloadResponse(200, new_ontology),
                FakeDownloadResponse(200, new_associations),
                FakeDownloadResponse(200, new_disease_annotations),
            ]
        )

        result = update_hpo_data(
            ontology_path=ontology_path,
            associations_path=associations_path,
            disease_annotations_path=disease_annotations_path,
            ontology_source_url="https://example.test/hp.obo",
            association_url_template=(
                "https://example.test/v{release}/"
                "phenotype_to_genes.txt"
            ),
            disease_url_template=(
                "https://example.test/v{release}/phenotype.hpoa"
            ),
            session=session,  # type: ignore[arg-type]
        )

        assert result == {
            "status": "updated",
            "previous_version": "hp/releases/2026-01-01",
            "current_version": "hp/releases/2026-02-01",
            "active_term_count": 1,
            "ontology_lookup_id_count": 1,
            "association_term_count": 1,
            "associated_gene_count": 1,
            "disease_annotation_term_count": 1,
            "associated_disease_count": 1,
            "ontology_backup_path": str(
                ontology_path.with_suffix(".previous.obo")
            ),
            "associations_backup_path": str(
                associations_path.with_suffix(".previous.txt")
            ),
            "disease_annotations_backup_path": str(
                disease_annotations_path.with_suffix(".previous.hpoa")
            ),
        }
        assert ontology_path.with_suffix(
            ".previous.obo"
        ).read_bytes() == old_ontology
        assert associations_path.with_suffix(
            ".previous.txt"
        ).read_bytes() == old_associations
        assert disease_annotations_path.with_suffix(
            ".previous.hpoa"
        ).read_bytes() == old_disease_annotations
        assert get_genes_for_hpo(
            "HP:0001263",
            ontology_path=ontology_path,
            associations_path=associations_path,
        )["genes"] == ["DDX3X"]
        assert get_diseases_for_hpo(
            "HP:0001263",
            ontology_path=ontology_path,
            annotations_path=disease_annotations_path,
        )["diseases"] == [
            {
                "id": "OMIM:300958",
                "name": "Snijders Blok-Campeau syndrome",
            }
        ]
        with pytest.raises(PhenotypeError, match="not found"):
            get_genes_for_hpo(
                "HP:0001250",
                ontology_path=ontology_path,
                associations_path=associations_path,
            )
        assert session.calls[1]["url"] == (
            "https://example.test/v2026-02-01/"
            "phenotype_to_genes.txt"
        )
        assert session.calls[2]["url"] == (
            "https://example.test/v2026-02-01/phenotype.hpoa"
        )

    def test_coordinated_hpo_data_update_detects_no_changes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_GENE_ASSOCIATION_TERMS",
            1,
        )
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_DISEASE_ANNOTATION_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        associations_path = tmp_path / "phenotype_to_genes.txt"
        disease_annotations_path = tmp_path / "phenotype.hpoa"
        ontology_content = self._hpo_release("2026-02-01")
        association_content = self._hpo_gene_release()
        disease_annotation_content = self._hpo_disease_release()
        ontology_path.write_bytes(ontology_content)
        associations_path.write_bytes(association_content)
        disease_annotations_path.write_bytes(
            disease_annotation_content
        )
        session = FakeDownloadSession(
            [
                FakeDownloadResponse(200, ontology_content),
                FakeDownloadResponse(200, association_content),
                FakeDownloadResponse(
                    200,
                    disease_annotation_content,
                ),
            ]
        )

        result = update_hpo_data(
            ontology_path=ontology_path,
            associations_path=associations_path,
            disease_annotations_path=disease_annotations_path,
            ontology_source_url="https://example.test/hp.obo",
            association_url_template=(
                "https://example.test/v{release}/"
                "phenotype_to_genes.txt"
            ),
            disease_url_template=(
                "https://example.test/v{release}/phenotype.hpoa"
            ),
            session=session,  # type: ignore[arg-type]
        )

        assert result["status"] == "unchanged"
        assert result["ontology_backup_path"] is None
        assert result["associations_backup_path"] is None
        assert result["disease_annotations_backup_path"] is None

    @pytest.mark.parametrize(
        "association_response",
        [
            FakeDownloadResponse(503, b"service unavailable"),
            FakeDownloadResponse(
                200,
                (
                    b"hpo_id\thpo_name\tncbi_gene_id\tgene_symbol"
                    b"\tdisease_id\n"
                    b"HP:0001263\tGlobal developmental delay"
                    b"\t1654\tDDX3X\tOMIM:300958\n"
                ),
            ),
        ],
    )
    def test_failed_coordinated_update_preserves_all_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        association_response: FakeDownloadResponse,
    ) -> None:
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_ACTIVE_TERMS",
            1,
        )
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_GENE_ASSOCIATION_TERMS",
            1,
        )
        monkeypatch.setattr(
            "backend.phenotype.MIN_HPO_DISEASE_ANNOTATION_TERMS",
            1,
        )
        ontology_path = tmp_path / "hp.obo"
        associations_path = tmp_path / "phenotype_to_genes.txt"
        disease_annotations_path = tmp_path / "phenotype.hpoa"
        old_ontology = self._hpo_release("2026-01-01")
        old_associations = self._hpo_gene_release()
        old_disease_annotations = self._hpo_disease_release()
        ontology_path.write_bytes(old_ontology)
        associations_path.write_bytes(old_associations)
        disease_annotations_path.write_bytes(old_disease_annotations)
        session = FakeDownloadSession(
            [
                FakeDownloadResponse(
                    200,
                    self._hpo_release("2026-02-01"),
                ),
                association_response,
                FakeDownloadResponse(200, old_disease_annotations),
            ]
        )

        with pytest.raises(HPODataError):
            update_hpo_data(
                ontology_path=ontology_path,
                associations_path=associations_path,
                disease_annotations_path=disease_annotations_path,
                ontology_source_url="https://example.test/hp.obo",
                association_url_template=(
                    "https://example.test/v{release}/"
                    "phenotype_to_genes.txt"
                ),
                disease_url_template=(
                    "https://example.test/v{release}/phenotype.hpoa"
                ),
                session=session,  # type: ignore[arg-type]
            )

        assert ontology_path.read_bytes() == old_ontology
        assert associations_path.read_bytes() == old_associations
        assert (
            disease_annotations_path.read_bytes()
            == old_disease_annotations
        )
        assert list(tmp_path.glob("*.download")) == []

    def test_coordinated_hpo_update_requires_release_template(
        self,
        tmp_path: Path,
    ) -> None:
        with pytest.raises(HPODataError, match=r"\{release\}"):
            update_hpo_data(
                ontology_path=tmp_path / "hp.obo",
                associations_path=(
                    tmp_path / "phenotype_to_genes.txt"
                ),
                ontology_source_url="https://example.test/hp.obo",
                association_url_template=(
                    "https://example.test/phenotype_to_genes.txt"
                ),
            )


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

    @staticmethod
    def _myvariant_response(
        variant_id: str = "chr1:g.100A>G",
    ) -> dict[str, object]:
        """Build an exact MyVariant response with population evidence."""
        return {
            "_id": variant_id,
            "dbsnp": {
                "rsid": "rs123",
                "gene": {"symbol": "GENE1"},
                "alleles": [
                    {
                        "allele": "A",
                        "freq": {"gnomad": 0.996},
                    },
                    {
                        "allele": "G",
                        "freq": {
                            "1000g": 0.004,
                            "gnomad": 0.003,
                        },
                    },
                ],
            },
            "gnomad_exome": {"af": {"af": 0.001}},
            "gnomad_genome": {"af": 0.002},
            "exac": {"af": 0.0005},
        }

    @staticmethod
    def _clinvar_search_response(
        identifiers: list[str] | None = None,
    ) -> dict[str, object]:
        """Build an NCBI ClinVar ESearch response."""
        result_ids = identifiers if identifiers is not None else ["123"]
        return {
            "esearchresult": {
                "count": str(len(result_ids)),
                "idlist": result_ids,
            }
        }

    @staticmethod
    def _clinvar_summary_response(
        *,
        variation_id: str = "123",
        assembly: str = "GRCh38",
        spdi: str = "NC_000001.11:99:A:G",
    ) -> dict[str, object]:
        """Build a standardized ClinVar ESummary response."""
        summary = {
            "uid": variation_id,
            "obj_type": "single nucleotide variant",
            "accession": "VCV000000123",
            "accession_version": "VCV000000123.4",
            "title": "NM_000001.1(GENE1):c.100A>G",
            "variation_set": [
                {
                    "canonical_spdi": spdi,
                    "variation_loc": [
                        {
                            "assembly_name": assembly,
                            "chr": "1",
                            "start": "100",
                            "stop": "100",
                        }
                    ],
                }
            ],
            "supporting_submissions": {
                "scv": ["SCV000000001", "SCV000000002"],
                "rcv": ["RCV000000001"],
            },
            "germline_classification": {
                "description": "Pathogenic",
                "last_evaluated": "2025/01/02 00:00",
                "review_status": "reviewed by expert panel",
                "trait_set": [
                    {
                        "trait_name": "Example disease",
                        "trait_xrefs": [
                            {
                                "db_source": "MedGen",
                                "db_id": "C0000001",
                            }
                        ],
                    }
                ],
            },
            "gene_sort": "GENE1",
            "genes": [
                {
                    "symbol": "GENE1",
                    "geneid": "1",
                }
            ],
        }
        return {
            "result": {
                "uids": [variation_id],
                variation_id: summary,
            }
        }

    @staticmethod
    def _clingen_response(
        *,
        gene: str = "GENE1",
        submitter: str = "ClinGen",
        genome: str = "hg38",
        chrom: str = "chr1",
        records: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """Build a UCSC GenCC track response."""
        gencc_records = records
        if gencc_records is None:
            gencc_records = [
                {
                    "chrom": chrom,
                    "chromStart": 50,
                    "chromEnd": 150,
                    "sgc_id": "SGC-000001",
                    "gene_curie": "HGNC:1",
                    "gene_symbol": gene,
                    "disease_curie": "MONDO:0000001",
                    "disease_title": "Example disease",
                    "classification_curie": "GENCC:100001",
                    "classification_title": "Definitive",
                    "moi_curie": "HP:0000006",
                    "moi_title": "Autosomal dominant inheritance",
                    "submitter_curie": "GENCC:000102",
                    "submitter_title": submitter,
                    "sub_date": "2025-01-02T00:00:00.000000Z",
                    "sub_public_report_url": (
                        "https://search.clinicalgenome.org/kb/"
                        "gene-validity/CGGV:assertion_example"
                    ),
                    "sub_assertion_criteria_url": (
                        "https://clinicalgenome.org/docs/example-sop/"
                    ),
                    "sub_submission_id": (
                        "11111111-2222-3333-4444-555555555555"
                    ),
                    "sub_pmids": "12345678, 23456789",
                }
            ]

        return {
            "genome": genome,
            "track": "genCC",
            "chrom": chrom,
            "genCC": gencc_records,
            "itemsReturned": len(gencc_records),
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
        assert session.post_calls[0]["params"] == {
            "canonical": 1,
            "hgvs": 1,
            "pick_allele_gene": 1,
            "protein": 1,
            "mane": 1,
        }

    def test_successful_myvariant_response_is_standardized(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        myvariant = annotation["sources"]["myvariant"]
        assert myvariant == {
            "status": "success",
            "variant_id": "chr1:g.100A>G",
            "rsid": "rs123",
            "gene": "GENE1",
            "population_frequencies": {
                "gnomad_exome": 0.001,
                "gnomad_genome": 0.002,
                "exac": 0.0005,
                "dbsnp_1000g": 0.004,
                "dbsnp_gnomad": 0.003,
            },
            "max_population_frequency": 0.004,
        }
        assert annotation["population_frequency"] == 0.004
        assert "dbsnp" not in myvariant
        assert session.myvariant_get_calls[0]["params"] == {
            "assembly": "hg38",
            "fields": (
                "_id,dbsnp.rsid,dbsnp.gene.symbol,dbsnp.alleles,"
                "dbnsfp.genename,cadd.gene.genename,gnomad_exome.af,"
                "gnomad_genome.af,exac.af"
            ),
        }
        assert session.myvariant_get_calls[0]["url"].endswith(
            "/variant/chr1%3Ag.100A%3EG"
        )
        assert annotation["references"][-1]["source"] == "MyVariant.info"
        assert annotation["references"][-1]["url"].endswith(
            "chr1%3Ag.100A%3EG?assembly=hg38"
        )

    def test_myvariant_uses_explicit_grch37_assembly(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.annotation.settings.GENOME_ASSEMBLY",
            "GRCh37",
        )
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [self._vep_response(assembly="GRCh37")],
                )
            ],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["assembly"] == "GRCh37"
        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert (
            session.myvariant_get_calls[0]["params"]["assembly"]
            == "hg19"
        )

    def test_myvariant_rejects_non_exact_record(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(
                    200,
                    self._myvariant_response("chr1:g.100A>T"),
                )
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "error"
        assert annotation["population_frequency"] is None
        assert any(
            "does not exactly match" in warning
            for warning in annotation["warnings"]
        )

    @pytest.mark.parametrize(
        ("payload", "expected_warning"),
        [
            (ValueError("invalid JSON"), "invalid JSON"),
            ([], "unexpected response structure"),
        ],
    )
    def test_myvariant_invalid_response_is_isolated(
        self,
        payload: object,
        expected_warning: str,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[FakeResponse(200, payload)],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "error"
        assert any(
            expected_warning in warning
            for warning in annotation["warnings"]
        )

    def test_myvariant_timeout_is_retried_then_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                requests.Timeout("temporary timeout"),
                FakeResponse(200, self._myvariant_response()),
            ],
        )
        delays: list[float] = []
        monkeypatch.setattr(
            "backend.annotation.time.sleep",
            delays.append,
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=1,
        )[0]

        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert len(session.myvariant_get_calls) == 2
        assert delays == [1.0]

    def test_myvariant_http_500_preserves_vep_evidence(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(500, {"error": "temporary failure"})
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["gene"] == "GENE1"
        assert annotation["sources"]["myvariant"]["status"] == "error"
        assert any(
            "HTTP 500" in warning
            for warning in annotation["warnings"]
        )

    def test_myvariant_404_is_marked_not_found(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(404, {"error": "ID not found"})
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "not_found"
        assert annotation["population_frequency"] is None

    def test_myvariant_builds_exact_deletion_hgvs(self) -> None:
        variant = self._variant()
        variant["ref"] = "AT"
        variant["alt"] = "A"
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(
                    200,
                    self._myvariant_response("chr1:g.101del"),
                )
            ],
        )

        annotation = annotate_variants(
            [variant],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert session.myvariant_get_calls[0]["url"].endswith(
            "/variant/chr1%3Ag.101del"
        )

    def test_successful_direct_clinvar_response_is_standardized(
        self,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(200, self._clinvar_summary_response()),
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        clinvar = annotation["sources"]["clinvar"]
        assert annotation["sources"]["myvariant"]["status"] == "not_found"
        assert clinvar == {
            "status": "success",
            "query_hgvs": "NC_000001.11:g.100A>G",
            "variation_id": "123",
            "accession": "VCV000000123",
            "accession_version": "VCV000000123.4",
            "gene": "GENE1",
            "clinical_significance": "Pathogenic",
            "review_status": "reviewed by expert panel",
            "last_evaluated": "2025/01/02 00:00",
            "conditions": [
                {
                    "name": "Example disease",
                    "identifiers": [
                        {
                            "source": "MedGen",
                            "id": "C0000001",
                        }
                    ],
                }
            ],
            "condition_count": 1,
            "conditions_truncated": False,
            "scv_accessions": [
                "SCV000000001",
                "SCV000000002",
            ],
            "rcv_accessions": ["RCV000000001"],
        }
        assert "germline_classification" not in clinvar
        assert session.clinvar_get_calls[0]["params"] == {
            "tool": "clinical_variant_app",
            "db": "clinvar",
            "term": '"NC_000001.11:g.100A>G"[varnam]',
            "retmode": "json",
            "retmax": 20,
        }
        assert session.clinvar_get_calls[1]["params"] == {
            "tool": "clinical_variant_app",
            "db": "clinvar",
            "id": "123",
            "retmode": "json",
            "version": "2.0",
        }
        assert annotation["references"][-1] == {
            "source": "NCBI ClinVar",
            "url": (
                "https://www.ncbi.nlm.nih.gov/clinvar/"
                "variation/123/"
            ),
        }

    def test_clinvar_uses_explicit_grch37_hgvs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.annotation.settings.GENOME_ASSEMBLY",
            "GRCh37",
        )
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [self._vep_response(assembly="GRCh37")],
                )
            ],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(
                    200,
                    self._clinvar_summary_response(
                        assembly="GRCh37",
                    ),
                ),
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["clinvar"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["query_hgvs"] == (
            "NC_000001.10:g.100A>G"
        )
        assert session.clinvar_get_calls[0]["params"]["term"] == (
            '"NC_000001.10:g.100A>G"[varnam]'
        )

    def test_clinvar_rejects_non_exact_summary(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(
                    200,
                    self._clinvar_summary_response(
                        spdi="NC_000001.11:99:A:T",
                    ),
                ),
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["status"] == "error"
        assert any(
            "did not return exactly one record" in warning
            for warning in annotation["warnings"]
        )

    @pytest.mark.parametrize(
        ("payload", "expected_warning"),
        [
            (ValueError("invalid JSON"), "invalid JSON"),
            ([], "unexpected response structure"),
        ],
    )
    def test_clinvar_invalid_response_is_isolated(
        self,
        payload: object,
        expected_warning: str,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clinvar_responses=[FakeResponse(200, payload)],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["status"] == "error"
        assert any(
            expected_warning in warning
            for warning in annotation["warnings"]
        )

    def test_clinvar_timeout_is_retried_then_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clinvar_responses=[
                requests.Timeout("temporary timeout"),
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(200, self._clinvar_summary_response()),
            ],
        )
        delays: list[float] = []
        monkeypatch.setattr(
            "backend.annotation.time.sleep",
            delays.append,
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=1,
        )[0]

        assert annotation["sources"]["clinvar"]["status"] == "success"
        assert len(session.clinvar_get_calls) == 3
        assert delays == [1.0]

    def test_clinvar_http_500_preserves_other_sources(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
            clinvar_responses=[
                FakeResponse(500, {"error": "temporary failure"})
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["status"] == "error"
        assert any(
            "HTTP 500" in warning
            for warning in annotation["warnings"]
        )

    def test_clinvar_empty_search_is_marked_not_found(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clinvar_responses=[
                FakeResponse(
                    200,
                    self._clinvar_search_response([]),
                )
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["clinvar"]["status"] == "not_found"
        assert len(session.clinvar_get_calls) == 1

    def test_successful_clingen_response_is_standardized(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clingen_responses=[
                FakeResponse(200, self._clingen_response())
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        clingen = annotation["sources"]["clingen"]
        assert clingen["status"] == "success"
        assert clingen["data_provider"] == "UCSC GenCC"
        assert clingen["query_gene"] == "GENE1"
        assert clingen["gene"] == "GENE1"
        assert clingen["gene_id"] == "HGNC:1"
        assert clingen["curation_count"] == 1
        assert clingen["curations_truncated"] is False
        assert clingen["curations"] == [
            {
                "curation_id": "SGC-000001",
                "disease": "Example disease",
                "disease_id": "MONDO:0000001",
                "classification": "Definitive",
                "classification_id": "GENCC:100001",
                "mode_of_inheritance": (
                    "Autosomal dominant inheritance"
                ),
                "mode_of_inheritance_id": "HP:0000006",
                "classification_date": (
                    "2025-01-02T00:00:00.000000Z"
                ),
                "submitter": "ClinGen",
                "criteria_url": (
                    "https://clinicalgenome.org/docs/example-sop/"
                ),
                "submission_id": (
                    "11111111-2222-3333-4444-555555555555"
                ),
                "pmids": ["12345678", "23456789"],
                "report_url": (
                    "https://search.clinicalgenome.org/kb/"
                    "gene-validity/CGGV:assertion_example"
                ),
            }
        ]
        assert session.clingen_get_calls[0]["params"] == {
            "genome": "hg38",
            "track": "genCC",
            "chrom": "chr1",
            "start": 99,
            "end": 100,
        }
        assert "raw" not in clingen
        assert annotation["references"][-1]["source"] == (
            "ClinGen Gene-Disease Validity via UCSC GenCC"
        )

    def test_clingen_uses_explicit_grch37_coordinates(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "backend.annotation.settings.GENOME_ASSEMBLY",
            "GRCh37",
        )
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    [self._vep_response(assembly="GRCh37")],
                )
            ],
            clingen_responses=[
                FakeResponse(
                    200,
                    self._clingen_response(genome="hg19"),
                )
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["clingen"]["status"] == "success"
        assert session.clingen_get_calls[0]["params"] == {
            "genome": "hg19",
            "track": "genCC",
            "chrom": "chr1",
            "start": 99,
            "end": 100,
        }

    def test_clingen_rejects_non_exact_gene_match(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clingen_responses=[
                FakeResponse(
                    200,
                    self._clingen_response(gene="GENE10"),
                )
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["clingen"]["status"] == "not_found"
        assert annotation["sources"]["clingen"]["curations"] == []

    def test_clingen_rejects_other_gencc_submitters(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clingen_responses=[
                FakeResponse(
                    200,
                    self._clingen_response(submitter="PanelApp Australia"),
                )
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["clingen"]["status"] == "not_found"
        assert annotation["sources"]["clingen"]["curations"] == []

    @pytest.mark.parametrize(
        ("payload", "expected_warning"),
        [
            (ValueError("invalid JSON"), "invalid JSON"),
            ({"track": "genCC", "genCC": []}, "unexpected response structure"),
            (
                {"error": "track not found"},
                "API error",
            ),
        ],
    )
    def test_clingen_invalid_response_is_isolated(
        self,
        payload: object,
        expected_warning: str,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(200, self._clinvar_summary_response()),
            ],
            clingen_responses=[FakeResponse(200, payload)],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["status"] == "success"
        assert annotation["sources"]["clingen"]["status"] == "error"
        assert any(
            expected_warning in warning
            for warning in annotation["warnings"]
        )

    def test_clingen_timeout_is_retried_then_succeeds(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            clingen_responses=[
                requests.Timeout("temporary timeout"),
                FakeResponse(200, self._clingen_response()),
            ],
        )
        delays: list[float] = []
        monkeypatch.setattr(
            "backend.annotation.time.sleep",
            delays.append,
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=1,
        )[0]

        assert annotation["sources"]["clingen"]["status"] == "success"
        assert len(session.clingen_get_calls) == 2
        assert delays == [1.0]

    def test_clingen_http_500_preserves_other_sources(self) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(200, self._clinvar_summary_response()),
            ],
            clingen_responses=[
                FakeResponse(500, {"error": "temporary failure"})
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "success"
        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["status"] == "success"
        assert annotation["sources"]["clingen"]["status"] == "error"
        assert any(
            "HTTP 500" in warning
            for warning in annotation["warnings"]
        )

    def test_clingen_is_not_queried_without_a_gene(self) -> None:
        vep_response = self._vep_response()
        vep_response["transcript_consequences"] = []
        session = FakeSession(
            [FakeResponse(200, [vep_response])],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["clingen"]["status"] == (
            "not_applicable"
        )
        assert session.clingen_get_calls == []

    def test_unified_multi_source_annotation_is_complete_and_clean(
        self,
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, [self._vep_response()])],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(200, self._clinvar_summary_response()),
            ],
            clingen_responses=[
                FakeResponse(200, self._clingen_response())
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert set(annotation) == {
            "variant",
            "assembly",
            "gene",
            "gene_id",
            "transcript",
            "consequence",
            "impact",
            "protein_change",
            "population_frequency",
            "sources",
            "references",
            "warnings",
        }
        assert {
            source: evidence["status"]
            for source, evidence in annotation["sources"].items()
        } == {
            "vep": "success",
            "myvariant": "success",
            "clinvar": "success",
            "clingen": "success",
        }
        assert annotation["gene"] == "GENE1"
        assert annotation["consequence"] == "missense_variant"
        assert annotation["population_frequency"] == 0.004
        assert (
            annotation["sources"]["clinvar"]["clinical_significance"]
            == "Pathogenic"
        )
        assert (
            annotation["sources"]["clinvar"]["review_status"]
            == "reviewed by expert panel"
        )
        assert (
            annotation["sources"]["clingen"]["curations"][0][
                "classification"
            ]
            == "Definitive"
        )
        assert {
            reference["source"]
            for reference in annotation["references"]
        } == {
            "Ensembl VEP",
            "MyVariant.info",
            "NCBI ClinVar",
            "ClinGen Gene-Disease Validity via UCSC GenCC",
        }
        assert annotation["warnings"] == []

        assert "input" not in annotation["sources"]["vep"]
        assert "dbsnp" not in annotation["sources"]["myvariant"]
        assert (
            "germline_classification"
            not in annotation["sources"]["clinvar"]
        )
        assert "genCC" not in annotation["sources"]["clingen"]

    def test_vep_failure_preserves_other_source_evidence(self) -> None:
        session = FakeSession(
            [FakeResponse(500, {"error": "temporary failure"})],
            get_responses=[
                FakeResponse(200, self._myvariant_response())
            ],
            clinvar_responses=[
                FakeResponse(200, self._clinvar_search_response()),
                FakeResponse(200, self._clinvar_summary_response()),
            ],
            clingen_responses=[
                FakeResponse(200, self._clingen_response())
            ],
        )

        annotation = annotate_variants(
            [self._variant()],
            session=session,  # type: ignore[arg-type]
            max_retries=0,
        )[0]

        assert annotation["sources"]["vep"]["status"] == "error"
        assert annotation["sources"]["myvariant"]["status"] == "success"
        assert annotation["sources"]["clinvar"]["status"] == "success"
        assert annotation["sources"]["clingen"]["status"] == "success"
        assert annotation["population_frequency"] == 0.004
        assert (
            annotation["sources"]["clinvar"]["clinical_significance"]
            == "Pathogenic"
        )
        assert (
            annotation["sources"]["clingen"]["curation_count"]
            == 1
        )
        assert any(
            "Ensembl VEP returned HTTP 500" in warning
            for warning in annotation["warnings"]
        )

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
        assert len(session.post_calls) == 1
        assert len(session.myvariant_get_calls) == 2
        assert len(session.clinvar_get_calls) == 2
        assert len(session.clingen_get_calls) == 2
        request_json = session.post_calls[0]["json"]
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
        assert len(session.post_calls) == 2

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
        assert len(session.post_calls) == 2
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
