"""Validation and parsing for pre-filtered VCF variant tables."""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator, Mapping, Sequence
from math import isfinite
from pathlib import Path

import vcfpy

from config import settings


SUPPORTED_VCF_SUFFIXES = (".vcf", ".vcf.gz")
MAX_FILTERED_VCF_ROWS = 5
STANDARD_PRIMARY_CHROMOSOMES = (
    *(str(chromosome) for chromosome in range(1, 23)),
    "X",
    "Y",
    "MT",
)
# Nuclear lengths follow the NCBI Genome Reference Consortium assembly
# tables. MT uses the 16,569-base NC_012920.1 reference sequence.
PRIMARY_CHROMOSOME_LENGTHS = {
    "GRCh37": {
        "1": 249_250_621,
        "2": 243_199_373,
        "3": 198_022_430,
        "4": 191_154_276,
        "5": 180_915_260,
        "6": 171_115_067,
        "7": 159_138_663,
        "8": 146_364_022,
        "9": 141_213_431,
        "10": 135_534_747,
        "11": 135_006_516,
        "12": 133_851_895,
        "13": 115_169_878,
        "14": 107_349_540,
        "15": 102_531_392,
        "16": 90_354_753,
        "17": 81_195_210,
        "18": 78_077_248,
        "19": 59_128_983,
        "20": 63_025_520,
        "21": 48_129_895,
        "22": 51_304_566,
        "X": 155_270_560,
        "Y": 59_373_566,
        "MT": 16_569,
    },
    "GRCh38": {
        "1": 248_956_422,
        "2": 242_193_529,
        "3": 198_295_559,
        "4": 190_214_555,
        "5": 181_538_259,
        "6": 170_805_979,
        "7": 159_345_973,
        "8": 145_138_636,
        "9": 138_394_717,
        "10": 133_797_422,
        "11": 135_086_622,
        "12": 133_275_309,
        "13": 114_364_328,
        "14": 107_043_718,
        "15": 101_991_189,
        "16": 90_338_345,
        "17": 83_257_441,
        "18": 80_373_285,
        "19": 58_617_616,
        "20": 64_444_167,
        "21": 46_709_983,
        "22": 50_818_468,
        "X": 156_040_895,
        "Y": 57_227_415,
        "MT": 16_569,
    },
}
MANUAL_VARIANT_FIELDS = frozenset(
    {"chrom", "pos", "ref", "alt", "qual", "filter"}
)
MANUAL_ALLELE_PATTERN = re.compile(
    r"^[ACGTNRYKMSWBDHV*]+$",
    re.IGNORECASE,
)
SYMBOLIC_ALT_PATTERN = re.compile(
    r"^<[A-Z0-9_.:-]+>$",
    re.IGNORECASE,
)
VariantData = dict[str, str | int | float | None]


class VCFProcessingError(ValueError):
    """Raised when filtered VCF input cannot be processed safely."""


def _validate_vcf_path(vcf_path: str | Path) -> Path:
    """Validate that a supported VCF file exists."""

    path = Path(vcf_path).expanduser().resolve()
    if not path.exists():
        raise VCFProcessingError(f"VCF file does not exist: {path}")
    if not path.is_file():
        raise VCFProcessingError(f"VCF path is not a file: {path}")
    if not path.name.lower().endswith(SUPPORTED_VCF_SUFFIXES):
        raise VCFProcessingError(
            "VCF file must use the .vcf or .vcf.gz extension."
        )
    return path


def _open_reader(path: Path) -> vcfpy.Reader:
    """Open a VCF reader and convert parser errors to project errors."""

    try:
        return vcfpy.Reader.from_path(str(path))
    except (OSError, ValueError, vcfpy.exceptions.VCFPyException) as exc:
        raise VCFProcessingError(
            f"Could not open VCF file '{path.name}': {exc}"
        ) from exc


def _validate_header(reader: vcfpy.Reader, path: Path) -> None:
    """Require a valid VCF fileformat declaration."""

    fileformat_lines = [
        line
        for line in reader.header.lines
        if line.key == "fileformat"
    ]
    if (
        not fileformat_lines
        or not str(fileformat_lines[0].value).startswith("VCFv")
    ):
        raise VCFProcessingError(
            f"VCF file '{path.name}' has no valid fileformat header."
        )


def validate_vcf(vcf_path: str | Path) -> Path:
    """Validate a VCF path and its required header."""

    path = _validate_vcf_path(vcf_path)
    reader = _open_reader(path)
    try:
        _validate_header(reader, path)
    finally:
        reader.close()
    return path


def _normalize_chromosome(chromosome: str) -> str:
    """Normalize common chromosome prefixes without changing assembly."""

    value = chromosome.strip()
    if value.lower().startswith("chr"):
        value = value[3:]
    if value.upper() == "M":
        return "MT"
    if not value:
        raise VCFProcessingError("Chromosome cannot be empty.")
    return value


def normalize_primary_chromosome(chromosome: str) -> str:
    """Return one canonical standard human chromosome label."""

    normalized = _normalize_chromosome(chromosome).upper()
    if normalized not in STANDARD_PRIMARY_CHROMOSOMES:
        raise VCFProcessingError(
            "CHROM must be one of 1-22, X, Y, or MT."
        )
    return normalized


def get_primary_chromosome_length(
    chromosome: str,
    assembly: str,
) -> int:
    """Return the configured assembly length of a primary chromosome."""

    lengths = PRIMARY_CHROMOSOME_LENGTHS.get(assembly)
    if lengths is None:
        raise VCFProcessingError(
            "Genome assembly must be GRCh37 or GRCh38."
        )
    return lengths[normalize_primary_chromosome(chromosome)]


def _format_filter(filters: list[str]) -> str | None:
    """Convert VCF FILTER values to one stable output value."""

    cleaned_filters = [
        value
        for value in filters
        if value and value != "."
    ]
    return ";".join(cleaned_filters) if cleaned_filters else None


def _record_to_variants(
    record: vcfpy.Record,
) -> Iterator[VariantData]:
    """Split one filtered VCF row into one object per ALT allele."""

    if not record.ALT:
        raise VCFProcessingError(
            f"Variant at {record.CHROM}:{record.POS} has no ALT allele."
        )
    quality = None if record.QUAL is None else float(record.QUAL)
    filter_value = _format_filter(record.FILTER)
    for alternate in record.ALT:
        alt_value = str(alternate.value).strip()
        if not alt_value or alt_value == ".":
            raise VCFProcessingError(
                f"Variant at {record.CHROM}:{record.POS} "
                "has an invalid ALT allele."
            )
        yield {
            "chrom": _normalize_chromosome(record.CHROM),
            "pos": int(record.POS),
            "ref": record.REF.upper(),
            "alt": alt_value.upper(),
            "qual": quality,
            "filter": filter_value,
        }


def iter_vcf_variants(
    vcf_path: str | Path,
) -> Iterator[VariantData]:
    """Read every allele from a filtered VCF of at most five rows."""

    path = _validate_vcf_path(vcf_path)
    reader = _open_reader(path)
    record_count = 0
    try:
        _validate_header(reader, path)
        record_iterator = iter(reader)
        while True:
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        category=vcfpy.exceptions.FieldInfoNotFound,
                    )
                    record = next(record_iterator)
            except StopIteration:
                break
            record_count += 1
            if record_count > MAX_FILTERED_VCF_ROWS:
                raise VCFProcessingError(
                    "Filtered VCF input cannot contain more than "
                    f"{MAX_FILTERED_VCF_ROWS} data rows."
                )
            yield from _record_to_variants(record)
    except VCFProcessingError:
        raise
    except (OSError, ValueError, vcfpy.exceptions.VCFPyException) as exc:
        raise VCFProcessingError(
            f"Could not parse VCF file '{path.name}': {exc}"
        ) from exc
    finally:
        reader.close()


def parse_vcf(vcf_path: str | Path) -> list[VariantData]:
    """Return all standardized variants from one filtered VCF."""

    return list(iter_vcf_variants(vcf_path))


def _is_supported_manual_allele(
    allele: str,
    *,
    allow_symbolic: bool,
) -> bool:
    """Validate a manual REF or ALT allele."""

    if MANUAL_ALLELE_PATTERN.fullmatch(allele):
        return True
    return bool(
        allow_symbolic
        and SYMBOLIC_ALT_PATTERN.fullmatch(allele)
    )


def _manual_text(value: object, field: str, row_index: int) -> str:
    """Return one required manual-table text value."""

    if not isinstance(value, str) or not value.strip():
        raise VCFProcessingError(
            f"Manual row {row_index + 1} {field} is required."
        )
    return value.strip()


def _manual_quality(value: object, row_index: int) -> float | None:
    """Validate an optional manual-table QUAL value."""

    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VCFProcessingError(
            f"Manual row {row_index + 1} QUAL must be numeric."
        )
    quality = float(value)
    if not isfinite(quality) or quality < 0:
        raise VCFProcessingError(
            f"Manual row {row_index + 1} QUAL must be finite and "
            "non-negative."
        )
    return quality


def _parse_manual_row(
    row: Mapping[str, object],
    row_index: int,
) -> list[VariantData]:
    """Validate and split one manually entered VCF-style row."""

    if set(row) != MANUAL_VARIANT_FIELDS:
        raise VCFProcessingError(
            f"Manual row {row_index + 1} has invalid columns."
        )
    chromosome = _manual_text(row["chrom"], "CHROM", row_index)
    raw_position = row["pos"]
    if (
        isinstance(raw_position, bool)
        or not isinstance(raw_position, int)
    ):
        raise VCFProcessingError(
            f"Manual row {row_index + 1} POS must be an integer."
        )
    if raw_position <= 0:
        raise VCFProcessingError(
            f"Manual row {row_index + 1} POS must be greater than zero."
        )
    try:
        chromosome = normalize_primary_chromosome(chromosome)
    except VCFProcessingError as exc:
        raise VCFProcessingError(
            f"Manual row {row_index + 1} {exc}"
        ) from exc
    chromosome_length = get_primary_chromosome_length(
        chromosome,
        settings.GENOME_ASSEMBLY,
    )
    if raw_position > chromosome_length:
        raise VCFProcessingError(
            f"Manual row {row_index + 1} POS must be between 1 and "
            f"{chromosome_length:,} for chromosome {chromosome} in "
            f"{settings.GENOME_ASSEMBLY}."
        )
    reference = _manual_text(
        row["ref"],
        "REF",
        row_index,
    ).upper()
    if not _is_supported_manual_allele(
        reference,
        allow_symbolic=False,
    ):
        raise VCFProcessingError(
            f"Manual row {row_index + 1} REF allele is invalid."
        )
    alternates = [
        alternate.strip().upper()
        for alternate in _manual_text(
            row["alt"],
            "ALT",
            row_index,
        ).split(",")
    ]
    if not alternates or any(
        not _is_supported_manual_allele(
            alternate,
            allow_symbolic=True,
        )
        for alternate in alternates
    ):
        raise VCFProcessingError(
            f"Manual row {row_index + 1} ALT allele is invalid."
        )
    quality = _manual_quality(row["qual"], row_index)
    raw_filter = row["filter"]
    if raw_filter is not None and not isinstance(raw_filter, str):
        raise VCFProcessingError(
            f"Manual row {row_index + 1} FILTER must be text."
        )
    filter_value = (
        raw_filter.strip()
        if isinstance(raw_filter, str) and raw_filter.strip()
        else None
    )
    return [
        {
            "chrom": chromosome,
            "pos": raw_position,
            "ref": reference,
            "alt": alternate,
            "qual": quality,
            "filter": filter_value,
        }
        for alternate in alternates
    ]


def parse_manual_variants(
    rows: Sequence[Mapping[str, object]],
) -> list[VariantData]:
    """Validate one to five manually entered VCF-style rows."""

    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise VCFProcessingError(
            "Manual variants must be provided as table rows."
        )
    if not rows:
        raise VCFProcessingError(
            "At least one manual variant row is required."
        )
    if len(rows) > MAX_FILTERED_VCF_ROWS:
        raise VCFProcessingError(
            "Manual input cannot contain more than "
            f"{MAX_FILTERED_VCF_ROWS} rows."
        )
    variants: list[VariantData] = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise VCFProcessingError(
                f"Manual row {row_index + 1} must be a mapping."
            )
        variants.extend(_parse_manual_row(row, row_index))
    return variants


def process_vcf(
    vcf_path: str | Path | None = None,
    manual_variants: Sequence[Mapping[str, object]] | None = None,
) -> Iterator[VariantData]:
    """Process one mutually exclusive pre-filtered input source."""

    has_vcf = vcf_path is not None
    has_manual_variants = manual_variants is not None
    if has_vcf == has_manual_variants:
        raise VCFProcessingError(
            "Provide exactly one of vcf_path or manual_variants."
        )
    if has_vcf:
        return iter_vcf_variants(vcf_path)
    return iter(parse_manual_variants(manual_variants or ()))


__all__ = [
    "MANUAL_VARIANT_FIELDS",
    "MAX_FILTERED_VCF_ROWS",
    "PRIMARY_CHROMOSOME_LENGTHS",
    "STANDARD_PRIMARY_CHROMOSOMES",
    "SUPPORTED_VCF_SUFFIXES",
    "VCFProcessingError",
    "VariantData",
    "get_primary_chromosome_length",
    "iter_vcf_variants",
    "normalize_primary_chromosome",
    "parse_manual_variants",
    "parse_vcf",
    "process_vcf",
    "validate_vcf",
]
