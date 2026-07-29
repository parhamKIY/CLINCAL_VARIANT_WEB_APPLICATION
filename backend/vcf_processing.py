"""VCF validation, parsing, normalization, and manual variant input."""

import re
import shutil
import subprocess
import warnings
from collections.abc import Iterator
from pathlib import Path

import vcfpy


# ---------------------------------------------------------------------------
# Shared constants and output types
# ---------------------------------------------------------------------------

# Only plain VCF and gzip-compressed VCF files are accepted.
SUPPORTED_VCF_SUFFIXES = (".vcf", ".vcf.gz")

# Manual input supports nucleotide/IUPAC alleles and symbolic ALT values such
# as <DEL>. A symbolic value is not allowed for the reference allele.
MANUAL_ALLELE_PATTERN = re.compile(r"^[ACGTNRYKMSWBDHV*]+$", re.IGNORECASE)
SYMBOLIC_ALT_PATTERN = re.compile(r"^<[A-Z0-9_.:-]+>$", re.IGNORECASE)

# All downstream stages receive this simple, JSON-compatible structure.
VariantData = dict[str, str | int | float | None]


# ---------------------------------------------------------------------------
# Project-specific errors
# ---------------------------------------------------------------------------

class VCFProcessingError(ValueError):
    """Raised when VCF input cannot be validated or processed safely."""


# ---------------------------------------------------------------------------
# File and header validation
# ---------------------------------------------------------------------------

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
    """Require a valid VCF fileformat declaration and column header."""
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

    # Close the file even when header validation raises an exception.
    try:
        _validate_header(reader, path)
    finally:
        reader.close()

    return path


# ---------------------------------------------------------------------------
# Core field normalization
# ---------------------------------------------------------------------------

def _normalize_chromosome(chromosome: str) -> str:
    """Normalize common chromosome prefixes without changing the assembly."""
    value = chromosome.strip()

    # Annotation services commonly expect "17" instead of "chr17".
    if value.lower().startswith("chr"):
        value = value[3:]

    # Ensembl and most VCF resources use MT for mitochondrial variants.
    if value.upper() == "M":
        return "MT"

    if not value:
        raise VCFProcessingError("Chromosome cannot be empty.")

    return value


def _select_sample(
    sample_names: list[str],
    requested_sample: str | None,
) -> str | None:
    """Select the requested sample or the first available sample."""
    # Reference/sites-only VCFs legitimately have no patient sample columns.
    if not sample_names:
        if requested_sample is not None:
            raise VCFProcessingError(
                f"Sample '{requested_sample}' was requested, "
                "but the VCF has no sample columns."
            )
        return None

    # Patient VCFs usually contain one sample. Multi-sample callers should
    # explicitly provide sample_name when the first sample is not intended.
    if requested_sample is None:
        return sample_names[0]

    if requested_sample not in sample_names:
        raise VCFProcessingError(
            f"Sample '{requested_sample}' was not found in the VCF."
        )

    return requested_sample


def _extract_genotype(
    record: vcfpy.Record,
    sample_name: str | None,
) -> str | None:
    """Extract the GT value for one selected sample."""
    # No selected sample means genotype data is unavailable by design.
    if sample_name is None:
        return None

    for call in record.calls:
        if call.sample != sample_name:
            continue

        genotype = call.data.get("GT")
        if genotype in (None, "", "."):
            return None

        return str(genotype)

    return None


def _format_filter(filters: list[str]) -> str | None:
    """Convert VCF FILTER values to one stable output value."""
    # vcfpy represents a missing "." FILTER as an empty list. Returning None
    # prevents an unfiltered record from being incorrectly labelled PASS.
    cleaned_filters = [
        value
        for value in filters
        if value and value != "."
    ]

    if not cleaned_filters:
        return None

    return ";".join(cleaned_filters)


def _record_to_variants(
    record: vcfpy.Record,
    sample_name: str | None,
) -> Iterator[VariantData]:
    """Split one VCF record into one output object per ALT allele."""
    if not record.ALT:
        raise VCFProcessingError(
            f"Variant at {record.CHROM}:{record.POS} has no ALT allele."
        )

    genotype = _extract_genotype(record, sample_name)
    quality = (
        None
        if record.QUAL is None
        else float(record.QUAL)
    )
    filter_value = _format_filter(record.FILTER)

    # Emit one object per ALT so each REF/ALT pair can be annotated separately.
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
            "genotype": genotype,
        }


# ---------------------------------------------------------------------------
# Streaming VCF parsing
# ---------------------------------------------------------------------------

def iter_vcf_variants(
    vcf_path: str | Path,
    sample_name: str | None = None,
    max_variants: int | None = None,
) -> Iterator[VariantData]:
    """Stream standardized variants from a .vcf or .vcf.gz file."""
    if max_variants is not None and max_variants <= 0:
        raise VCFProcessingError(
            "max_variants must be greater than zero."
        )

    path = _validate_vcf_path(vcf_path)
    reader = _open_reader(path)
    emitted = 0

    try:
        _validate_header(reader, path)
        selected_sample = _select_sample(
            list(reader.header.samples.names),
            sample_name,
        )

        record_iterator = iter(reader)

        # Read one record at a time so large VCF files are not loaded into RAM.
        while True:
            try:
                # Some public VCFs contain undeclared INFO fields. They do not
                # affect the core fields extracted here, so suppress only this
                # specific parser warning while reading the current record.
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        category=vcfpy.exceptions.FieldInfoNotFound,
                    )
                    record = next(record_iterator)
            except StopIteration:
                break

            for variant in _record_to_variants(
                record,
                selected_sample,
            ):
                yield variant
                emitted += 1

                # This limit supports previews and tests without reading the
                # remainder of a large input file.
                if (
                    max_variants is not None
                    and emitted >= max_variants
                ):
                    return
    except VCFProcessingError:
        raise
    except (OSError, ValueError, vcfpy.exceptions.VCFPyException) as exc:
        raise VCFProcessingError(
            f"Could not parse VCF file '{path.name}': {exc}"
        ) from exc
    finally:
        reader.close()


def parse_vcf(
    vcf_path: str | Path,
    sample_name: str | None = None,
    max_variants: int | None = None,
) -> list[VariantData]:
    """Return standardized variants from a VCF file."""
    return list(
        iter_vcf_variants(
            vcf_path=vcf_path,
            sample_name=sample_name,
            max_variants=max_variants,
        )
    )


# ---------------------------------------------------------------------------
# Manual variant input
# ---------------------------------------------------------------------------

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


def parse_manual_variant(
    value: str,
    genotype: str | None = None,
) -> list[VariantData]:
    """Parse CHROM:POS:REF:ALT input into standardized variant objects."""
    # Keep the coordinate format strict and predictable for the MVP.
    parts = [part.strip() for part in value.split(":")]

    if len(parts) != 4:
        raise VCFProcessingError(
            "Manual variant must use CHROM:POS:REF:ALT format."
        )

    chromosome, raw_position, reference, raw_alternates = parts

    try:
        position = int(raw_position)
    except ValueError as exc:
        raise VCFProcessingError(
            "Manual variant position must be an integer."
        ) from exc

    if position <= 0:
        raise VCFProcessingError(
            "Manual variant position must be greater than zero."
        )

    reference = reference.upper()
    if not _is_supported_manual_allele(
        reference,
        allow_symbolic=False,
    ):
        raise VCFProcessingError(
            "Manual variant REF allele is invalid."
        )

    alternates = [
        alternate.strip().upper()
        for alternate in raw_alternates.split(",")
    ]

    if not alternates or any(
        not _is_supported_manual_allele(
            alternate,
            allow_symbolic=True,
        )
        for alternate in alternates
    ):
        raise VCFProcessingError(
            "Manual variant ALT allele is invalid."
        )

    normalized_chromosome = _normalize_chromosome(chromosome)

    # Comma-separated ALT values use the same one-object-per-ALT contract.
    return [
        {
            "chrom": normalized_chromosome,
            "pos": position,
            "ref": reference,
            "alt": alternate,
            "qual": None,
            "filter": "PASS",
            "genotype": genotype,
        }
        for alternate in alternates
    ]


# ---------------------------------------------------------------------------
# Reference-aware normalization with bcftools
# ---------------------------------------------------------------------------

def normalize_vcf(
    vcf_path: str | Path,
    output_path: str | Path,
    reference_fasta: str | Path,
    bcftools_executable: str = "bcftools",
) -> Path:
    """Normalize and split a VCF with bcftools and a reference FASTA."""
    input_path = validate_vcf(vcf_path)
    reference_path = Path(reference_fasta).expanduser().resolve()
    normalized_path = Path(output_path).expanduser().resolve()

    if not reference_path.is_file():
        raise VCFProcessingError(
            f"Reference FASTA does not exist: {reference_path}"
        )

    if not normalized_path.name.lower().endswith(".vcf.gz"):
        raise VCFProcessingError(
            "Normalized output must use the .vcf.gz extension."
        )

    if normalized_path.exists():
        raise VCFProcessingError(
            f"Normalized output already exists: {normalized_path}"
        )

    executable = shutil.which(bcftools_executable)
    if executable is None:
        raise VCFProcessingError(
            "bcftools is not installed or is not available on PATH."
        )

    normalized_path.parent.mkdir(parents=True, exist_ok=True)

    # The reference FASTA enables REF validation and left alignment. Splitting
    # multi-allelic records here makes later annotation deterministic.
    command = [
        executable,
        "norm",
        "--check-ref",
        "e",
        "--fasta-ref",
        str(reference_path),
        "--multiallelics",
        "-any",
        "--output-type",
        "z",
        "--output",
        str(normalized_path),
        str(input_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise VCFProcessingError(
            f"Could not start bcftools: {exc}"
        ) from exc

    if result.returncode != 0:
        error_message = result.stderr.strip() or "Unknown bcftools error."
        raise VCFProcessingError(
            f"bcftools normalization failed: {error_message}"
        )

    if not normalized_path.is_file():
        raise VCFProcessingError(
            "bcftools completed without creating the normalized VCF."
        )

    return normalized_path


# ---------------------------------------------------------------------------
# Public entry point used by the pipeline
# ---------------------------------------------------------------------------

def process_vcf(
    vcf_path: str | Path | None = None,
    manual_variant: str | None = None,
    sample_name: str | None = None,
    max_variants: int | None = None,
    genotype: str | None = None,
    normalize: bool = False,
    normalized_output_path: str | Path | None = None,
    reference_fasta: str | Path | None = None,
    bcftools_executable: str = "bcftools",
) -> Iterator[VariantData]:
    """Process one input source and return a streaming variant iterator.

    Normalization is optional for MVP parsing. When enabled, callers must
    provide a matching reference FASTA and a path for the normalized output.
    """
    has_vcf = vcf_path is not None
    has_manual_variant = bool(
        manual_variant
        and manual_variant.strip()
    )

    # Upload and manual coordinate entry are mutually exclusive input modes.
    if has_vcf == has_manual_variant:
        raise VCFProcessingError(
            "Provide exactly one of vcf_path or manual_variant."
        )

    if has_vcf:
        source_path = vcf_path

        # Normalization is deliberately opt-in because bcftools is not a
        # native dependency of the Windows MVP environment. It must be enabled
        # before clinical annotation when variants require reference-aware
        # left alignment or multi-allelic normalization.
        if normalize:
            if normalized_output_path is None or reference_fasta is None:
                raise VCFProcessingError(
                    "normalized_output_path and reference_fasta are "
                    "required when normalization is enabled."
                )

            source_path = normalize_vcf(
                vcf_path=vcf_path,
                output_path=normalized_output_path,
                reference_fasta=reference_fasta,
                bcftools_executable=bcftools_executable,
            )

        # Return the generator itself so later prioritization can consume a
        # large VCF without first materializing every variant in memory.
        return iter_vcf_variants(
            vcf_path=source_path,
            sample_name=sample_name,
            max_variants=max_variants,
        )

    if normalize:
        raise VCFProcessingError(
            "bcftools normalization is only available for VCF file input."
        )

    # Manual input is small, but expose it through the same iterator contract
    # used by uploaded VCF files.
    return iter(
        parse_manual_variant(
            manual_variant or "",
            genotype=genotype,
        )
    )
