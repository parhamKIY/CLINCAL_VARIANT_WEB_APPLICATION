"""External variant annotation and evidence standardization."""

import math
import re
import time
from collections.abc import Iterable, Iterator
from threading import Lock
from typing import Any
from urllib.parse import quote

import requests

from backend.logging_config import get_logger
from backend.vcf_processing import VariantData
from config import settings


LOGGER = get_logger("annotation")

# Ensembl documents a maximum of 200 variants per POST request.
MAX_VEP_BATCH_SIZE = 200
MAX_STORED_TRANSCRIPTS = 10
MAX_CLINVAR_SEARCH_RESULTS = 20
MAX_CLINVAR_CONDITIONS = 10
MAX_CLINVAR_ACCESSIONS = 20
MAX_CLINGEN_CURATIONS = 25
MAX_CLINGEN_PMIDS = 50
CLINVAR_REQUEST_INTERVAL = 0.34
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
REQUIRED_VARIANT_FIELDS = {"chrom", "pos", "ref", "alt"}
MYVARIANT_ASSEMBLIES = {
    "GRCh37": "hg19",
    "GRCh38": "hg38",
}
MYVARIANT_FIELDS = ",".join(
    (
        "_id",
        "dbsnp.rsid",
        "dbsnp.gene.symbol",
        "dbsnp.alleles",
        "dbnsfp.genename",
        "cadd.gene.genename",
        "gnomad_exome.af",
        "gnomad_genome.af",
        "exac.af",
    )
)
CLINVAR_REFSEQ_BASES = {
    **{
        str(chromosome): f"NC_{chromosome:06d}"
        for chromosome in range(1, 23)
    },
    "X": "NC_000023",
    "Y": "NC_000024",
    "MT": "NC_012920",
}
CLINVAR_REFSEQ_VERSIONS = {
    "GRCh37": {
        "1": 10,
        "2": 11,
        "3": 11,
        "4": 11,
        "5": 9,
        "6": 11,
        "7": 13,
        "8": 10,
        "9": 11,
        "10": 10,
        "11": 9,
        "12": 11,
        "13": 10,
        "14": 8,
        "15": 9,
        "16": 9,
        "17": 10,
        "18": 9,
        "19": 9,
        "20": 10,
        "21": 8,
        "22": 10,
        "X": 10,
        "Y": 9,
        "MT": 1,
    },
    "GRCh38": {
        "1": 11,
        "2": 12,
        "3": 12,
        "4": 12,
        "5": 10,
        "6": 12,
        "7": 14,
        "8": 11,
        "9": 12,
        "10": 11,
        "11": 10,
        "12": 12,
        "13": 11,
        "14": 9,
        "15": 10,
        "16": 10,
        "17": 11,
        "18": 10,
        "19": 10,
        "20": 11,
        "21": 9,
        "22": 11,
        "X": 11,
        "Y": 10,
        "MT": 1,
    },
}
CLINGEN_GENE_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
CLINGEN_UCSC_ASSEMBLIES = {
    "GRCh37": "hg19",
    "GRCh38": "hg38",
}
IMPACT_PRIORITY = {
    "HIGH": 4,
    "MODERATE": 3,
    "LOW": 2,
    "MODIFIER": 1,
}

AnnotationData = dict[str, Any]
_CLINVAR_RATE_LOCK = Lock()
_LAST_CLINVAR_REQUEST_AT = 0.0


class AnnotationError(ValueError):
    """Raised when annotation input or configuration is invalid."""


class AnnotationServiceError(RuntimeError):
    """Raised when an external annotation service cannot be used."""


# ---------------------------------------------------------------------------
# Input validation and batching
# ---------------------------------------------------------------------------

def _validate_variant(
    variant: VariantData,
    index: int,
) -> None:
    """Validate the minimum coordinate and allele fields for VEP."""
    if not isinstance(variant, dict):
        raise AnnotationError(
            f"Variant at index {index} must be a dictionary."
        )

    missing_fields = REQUIRED_VARIANT_FIELDS.difference(variant)
    if missing_fields:
        missing_text = ", ".join(sorted(missing_fields))
        raise AnnotationError(
            f"Variant at index {index} is missing: {missing_text}."
        )

    if not isinstance(variant["pos"], int) or variant["pos"] <= 0:
        raise AnnotationError(
            f"Variant at index {index} has an invalid position."
        )

    for allele_name in ("ref", "alt"):
        allele = variant[allele_name]
        if not isinstance(allele, str) or not allele.strip():
            raise AnnotationError(
                f"Variant at index {index} has an invalid "
                f"{allele_name.upper()} allele."
            )


def _resolve_batch_size(batch_size: int | None) -> int:
    """Resolve the configured VEP batch size within Ensembl's limit."""
    resolved_size = (
        settings.VEP_BATCH_SIZE
        if batch_size is None
        else batch_size
    )

    if (
        isinstance(resolved_size, bool)
        or not isinstance(resolved_size, int)
        or resolved_size <= 0
        or resolved_size > MAX_VEP_BATCH_SIZE
    ):
        raise AnnotationError(
            "batch_size must be an integer between 1 and 200."
        )

    return resolved_size


def _resolve_retries(max_retries: int | None) -> int:
    """Resolve the number of retry attempts for transient failures."""
    resolved_retries = (
        settings.ANNOTATION_MAX_RETRIES
        if max_retries is None
        else max_retries
    )

    if (
        isinstance(resolved_retries, bool)
        or not isinstance(resolved_retries, int)
        or resolved_retries < 0
    ):
        raise AnnotationError(
            "max_retries must be a non-negative integer."
        )

    return resolved_retries


def _iter_batches(
    variants: Iterable[VariantData],
    batch_size: int,
) -> Iterator[list[tuple[str, VariantData]]]:
    """Yield bounded batches while preserving streaming input."""
    batch: list[tuple[str, VariantData]] = []

    for index, variant in enumerate(variants):
        _validate_variant(variant, index)

        # The temporary identifier lets responses map back to their exact
        # input variant even when coordinates or alleles are duplicated.
        token = f"cv_{index}"
        batch.append((token, variant))

        if len(batch) == batch_size:
            yield batch
            batch = []

    if batch:
        yield batch


def _to_vep_input(token: str, variant: VariantData) -> str:
    """Convert a standardized variant to Ensembl's VCF-like input format."""
    return (
        f"{variant['chrom']} {variant['pos']} {token} "
        f"{variant['ref']} {variant['alt']} . . ."
    )


# ---------------------------------------------------------------------------
# Ensembl VEP HTTP client
# ---------------------------------------------------------------------------

def _retry_delay(
    attempt: int,
    response: requests.Response | None = None,
) -> float:
    """Calculate a short retry delay and respect numeric Retry-After."""
    if response is not None:
        retry_after = response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return min(float(retry_after), 10.0)

    return min(float(2**attempt), 5.0)


def _post_vep_batch(
    session: requests.Session,
    vep_inputs: list[str],
    max_retries: int,
) -> list[dict[str, Any]]:
    """Send one VEP batch with timeout, retry, and response validation."""
    endpoint = f"{settings.VEP_BASE_URL}/vep/homo_sapiens/region"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    params = {
        "canonical": 1,
        "hgvs": 1,
        "pick_allele_gene": 1,
        "protein": 1,
    }

    # MANE annotations are only available for GRCh38.
    if settings.GENOME_ASSEMBLY == "GRCh38":
        params["mane"] = 1

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None

        try:
            response = session.post(
                endpoint,
                headers=headers,
                params=params,
                json={"variants": vep_inputs},
                timeout=settings.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "Ensembl VEP request failed because the service "
                    "was unavailable."
                ) from exc

            LOGGER.warning(
                "Retrying Ensembl VEP after a connection error."
            )
            time.sleep(_retry_delay(attempt))
            continue

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                LOGGER.warning(
                    "Retrying Ensembl VEP after HTTP %s.",
                    response.status_code,
                )
                time.sleep(_retry_delay(attempt, response))
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "Ensembl VEP returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "Ensembl VEP returned invalid JSON."
            ) from exc

        if not isinstance(payload, list) or any(
            not isinstance(item, dict)
            for item in payload
        ):
            raise AnnotationServiceError(
                "Ensembl VEP returned an unexpected response structure."
            )

        return payload

    raise AnnotationServiceError(
        "Ensembl VEP retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# MyVariant.info HTTP client
# ---------------------------------------------------------------------------

def _normalize_chromosome(chromosome: Any) -> str | None:
    """Convert a VCF chromosome to a standard human chromosome name."""
    normalized = str(chromosome).strip()
    if normalized.lower().startswith("chr"):
        normalized = normalized[3:]

    normalized = normalized.upper()
    if normalized == "M":
        normalized = "MT"

    if normalized in {"X", "Y", "MT"}:
        return normalized

    if normalized.isdigit() and 1 <= int(normalized) <= 22:
        return str(int(normalized))

    return None


def _normalize_variant_edit(
    variant: VariantData,
) -> tuple[str, int, str, str] | None:
    """Remove shared VCF padding from one exact small-variant edit."""
    chromosome = _normalize_chromosome(variant["chrom"])
    reference = str(variant["ref"]).strip().upper()
    alternate = str(variant["alt"]).strip().upper()

    if (
        chromosome is None
        or not reference
        or not alternate
        or not set(reference).issubset({"A", "C", "G", "T"})
        or not set(alternate).issubset({"A", "C", "G", "T"})
        or reference == alternate
    ):
        return None

    start = int(variant["pos"])

    # Remove shared VCF padding so indels use HGVS coordinates and alleles.
    while reference and alternate and reference[0] == alternate[0]:
        reference = reference[1:]
        alternate = alternate[1:]
        start += 1

    while reference and alternate and reference[-1] == alternate[-1]:
        reference = reference[:-1]
        alternate = alternate[:-1]

    return chromosome, start, reference, alternate


def _format_genomic_hgvs(
    sequence: str,
    start: int,
    reference: str,
    alternate: str,
) -> str:
    """Format one normalized small-variant edit as genomic HGVS."""
    prefix = f"{sequence}:g."

    if not reference and alternate:
        return f"{prefix}{start - 1}_{start}ins{alternate}"

    if reference and not alternate:
        end = start + len(reference) - 1
        location = str(start) if start == end else f"{start}_{end}"
        return f"{prefix}{location}del"

    if len(reference) == 1 and len(alternate) == 1:
        return f"{prefix}{start}{reference}>{alternate}"

    end = start + len(reference) - 1
    location = str(start) if start == end else f"{start}_{end}"
    return f"{prefix}{location}delins{alternate}"


def _to_myvariant_hgvs(variant: VariantData) -> str | None:
    """Convert normalized VCF alleles to a MyVariant genomic HGVS ID."""
    edit = _normalize_variant_edit(variant)
    if edit is None:
        return None

    chromosome, start, reference, alternate = edit
    return _format_genomic_hgvs(
        f"chr{chromosome}",
        start,
        reference,
        alternate,
    )


def _get_myvariant(
    session: requests.Session,
    variant: VariantData,
    max_retries: int,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Retrieve one exact MyVariant.info record with bounded retries."""
    variant_id = _to_myvariant_hgvs(variant)
    if variant_id is None:
        return None, None, (
            "MyVariant.info does not support this chromosome or allele "
            "representation."
        )

    encoded_id = quote(variant_id, safe="")
    endpoint = f"{settings.MYVARIANT_BASE_URL}/variant/{encoded_id}"
    params = {
        "assembly": MYVARIANT_ASSEMBLIES[settings.GENOME_ASSEMBLY],
        "fields": MYVARIANT_FIELDS,
    }

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None

        try:
            response = session.get(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
                timeout=settings.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "MyVariant.info request failed because the service "
                    "was unavailable."
                ) from exc

            LOGGER.warning(
                "Retrying MyVariant.info after a connection error."
            )
            time.sleep(_retry_delay(attempt))
            continue

        if response.status_code == 404:
            return None, variant_id, None

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                LOGGER.warning(
                    "Retrying MyVariant.info after HTTP %s.",
                    response.status_code,
                )
                time.sleep(_retry_delay(attempt, response))
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "MyVariant.info returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "MyVariant.info returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise AnnotationServiceError(
                "MyVariant.info returned an unexpected response structure."
            )

        response_id = payload.get("_id")
        if (
            not isinstance(response_id, str)
            or response_id.upper() != variant_id.upper()
        ):
            raise AnnotationServiceError(
                "MyVariant.info returned a record that does not exactly "
                "match the requested assembly, chromosome, position, "
                "REF, and ALT."
            )

        return payload, variant_id, None

    raise AnnotationServiceError(
        "MyVariant.info retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# NCBI ClinVar HTTP client
# ---------------------------------------------------------------------------

def _to_clinvar_identifiers(
    variant: VariantData,
) -> tuple[str, str, str, int] | None:
    """Build exact assembly-specific HGVS and SPDI identifiers."""
    edit = _normalize_variant_edit(variant)
    if edit is None:
        return None

    chromosome, start, reference, alternate = edit
    accession_base = CLINVAR_REFSEQ_BASES[chromosome]
    accession_version = CLINVAR_REFSEQ_VERSIONS[
        settings.GENOME_ASSEMBLY
    ][chromosome]
    accession = f"{accession_base}.{accession_version}"
    hgvs = _format_genomic_hgvs(
        accession,
        start,
        reference,
        alternate,
    )
    spdi = f"{accession}:{start - 1}:{reference}:{alternate}"
    return hgvs, spdi, chromosome, start


def _wait_for_clinvar_request_slot(
    session: requests.Session,
) -> None:
    """Keep real NCBI E-utility traffic below three requests per second."""
    if not isinstance(session, requests.Session):
        return

    global _LAST_CLINVAR_REQUEST_AT

    with _CLINVAR_RATE_LOCK:
        elapsed = time.monotonic() - _LAST_CLINVAR_REQUEST_AT
        remaining = CLINVAR_REQUEST_INTERVAL - elapsed
        if remaining > 0:
            time.sleep(remaining)
        _LAST_CLINVAR_REQUEST_AT = time.monotonic()


def _get_clinvar_json(
    session: requests.Session,
    endpoint_name: str,
    params: dict[str, Any],
    max_retries: int,
) -> dict[str, Any]:
    """Call one ClinVar E-utility endpoint with bounded retries."""
    endpoint = f"{settings.CLINVAR_BASE_URL}/{endpoint_name}"
    request_params = {
        "tool": "clinical_variant_app",
        **params,
    }

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None

        try:
            _wait_for_clinvar_request_slot(session)
            response = session.get(
                endpoint,
                params=request_params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "ClinicalVariantInterpretation/0.1",
                },
                timeout=settings.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "NCBI ClinVar request failed because the service "
                    "was unavailable."
                ) from exc

            LOGGER.warning(
                "Retrying NCBI ClinVar after a connection error."
            )
            time.sleep(_retry_delay(attempt))
            continue

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                LOGGER.warning(
                    "Retrying NCBI ClinVar after HTTP %s.",
                    response.status_code,
                )
                time.sleep(_retry_delay(attempt, response))
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "NCBI ClinVar returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "NCBI ClinVar returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise AnnotationServiceError(
                "NCBI ClinVar returned an unexpected response structure."
            )

        if payload.get("error"):
            if attempt < max_retries:
                LOGGER.warning(
                    "Retrying NCBI ClinVar after an API error."
                )
                time.sleep(_retry_delay(attempt, response))
                continue

            raise AnnotationServiceError(
                "NCBI ClinVar returned an API error."
            )

        return payload

    raise AnnotationServiceError(
        "NCBI ClinVar retry loop ended unexpectedly."
    )


def _search_clinvar_ids(
    session: requests.Session,
    hgvs: str,
    max_retries: int,
) -> list[str]:
    """Find ClinVar Variation IDs using one exact HGVS variant name."""
    payload = _get_clinvar_json(
        session,
        "esearch.fcgi",
        {
            "db": "clinvar",
            "term": f'"{hgvs}"[varnam]',
            "retmode": "json",
            "retmax": MAX_CLINVAR_SEARCH_RESULTS,
        },
        max_retries,
    )
    result = payload.get("esearchresult")
    if not isinstance(result, dict):
        raise AnnotationServiceError(
            "NCBI ClinVar search returned an unexpected response structure."
        )

    raw_count = result.get("count")
    identifiers = result.get("idlist")
    try:
        count = int(raw_count)
    except (TypeError, ValueError) as exc:
        raise AnnotationServiceError(
            "NCBI ClinVar search returned an invalid result count."
        ) from exc

    if count == 0:
        return []

    if count > MAX_CLINVAR_SEARCH_RESULTS:
        raise AnnotationServiceError(
            "NCBI ClinVar returned too many records for one exact variant."
        )

    if (
        not isinstance(identifiers, list)
        or len(identifiers) != count
        or any(
            not isinstance(identifier, str) or not identifier
            for identifier in identifiers
        )
    ):
        raise AnnotationServiceError(
            "NCBI ClinVar search returned invalid Variation IDs."
        )

    return identifiers


def _get_clinvar_summaries(
    session: requests.Session,
    identifiers: list[str],
    max_retries: int,
) -> list[dict[str, Any]]:
    """Retrieve bounded ClinVar document summaries for exact search hits."""
    payload = _get_clinvar_json(
        session,
        "esummary.fcgi",
        {
            "db": "clinvar",
            "id": ",".join(identifiers),
            "retmode": "json",
            "version": "2.0",
        },
        max_retries,
    )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise AnnotationServiceError(
            "NCBI ClinVar summary returned an unexpected response structure."
        )

    summaries: list[dict[str, Any]] = []
    for identifier in identifiers:
        summary = result.get(identifier)
        if (
            not isinstance(summary, dict)
            or str(summary.get("uid", "")) != identifier
        ):
            raise AnnotationServiceError(
                "NCBI ClinVar summary omitted a requested Variation ID."
            )
        summaries.append(summary)

    return summaries


def _clinvar_location_matches(
    measure: dict[str, Any],
    chromosome: str,
    start: int,
) -> bool:
    """Validate the configured assembly, chromosome, and start coordinate."""
    locations = measure.get("variation_loc")
    if not isinstance(locations, list):
        return False

    return any(
        isinstance(location, dict)
        and location.get("assembly_name") == settings.GENOME_ASSEMBLY
        and str(location.get("chr", "")).upper() == chromosome
        and str(location.get("start", "")) == str(start)
        for location in locations
    )


def _select_exact_clinvar_record(
    summaries: list[dict[str, Any]],
    expected_spdi: str,
    chromosome: str,
    start: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select exactly one summary measure matching the requested variant."""
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for summary in summaries:
        measures = summary.get("variation_set")
        if not isinstance(measures, list):
            continue

        for measure in measures:
            if (
                not isinstance(measure, dict)
                or not _clinvar_location_matches(
                    measure,
                    chromosome,
                    start,
                )
            ):
                continue

            # ClinVar reports canonical SPDI on GRCh38. GRCh37 allele
            # exactness is guaranteed by the exact assembly-specific HGVS
            # ESearch term and coordinate validation above.
            if (
                settings.GENOME_ASSEMBLY == "GRCh38"
                and measure.get("canonical_spdi") != expected_spdi
            ):
                continue

            matches.append((summary, measure))

    if len(matches) != 1:
        raise AnnotationServiceError(
            "NCBI ClinVar did not return exactly one record matching the "
            "requested assembly, chromosome, position, REF, and ALT."
        )

    return matches[0]


def _get_clinvar(
    session: requests.Session,
    variant: VariantData,
    max_retries: int,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    str | None,
    str | None,
]:
    """Retrieve one exact direct ClinVar record and its matching measure."""
    identifiers = _to_clinvar_identifiers(variant)
    if identifiers is None:
        return None, None, None, (
            "NCBI ClinVar does not support this chromosome or allele "
            "representation."
        )

    hgvs, spdi, chromosome, start = identifiers
    variation_ids = _search_clinvar_ids(
        session,
        hgvs,
        max_retries,
    )
    if not variation_ids:
        return None, None, hgvs, None

    summaries = _get_clinvar_summaries(
        session,
        variation_ids,
        max_retries,
    )
    summary, measure = _select_exact_clinvar_record(
        summaries,
        spdi,
        chromosome,
        start,
    )
    return summary, measure, hgvs, None


# ---------------------------------------------------------------------------
# ClinGen evidence through the UCSC GenCC track
# ---------------------------------------------------------------------------

def _select_clingen_gene(annotation: AnnotationData) -> str | None:
    """Select one unambiguous gene symbol from standardized source fields."""
    candidates = (
        annotation.get("gene"),
        annotation.get("sources", {}).get("myvariant", {}).get("gene"),
        annotation.get("sources", {}).get("clinvar", {}).get("gene"),
    )

    for candidate in candidates:
        if not isinstance(candidate, str):
            continue

        symbol = candidate.strip()
        if CLINGEN_GENE_SYMBOL_PATTERN.fullmatch(symbol):
            return symbol

    return None


def _to_clingen_ucsc_region(
    variant: VariantData,
) -> tuple[str, int, int] | None:
    """Convert a 1-based VCF variant into a bounded UCSC query interval."""
    chromosome = _normalize_chromosome(variant.get("chrom"))
    position = variant.get("pos")
    reference = variant.get("ref")

    if (
        chromosome is None
        or not isinstance(position, int)
        or isinstance(position, bool)
        or position < 1
        or not isinstance(reference, str)
        or not reference.strip()
    ):
        return None

    ucsc_chromosome = "chrM" if chromosome == "MT" else f"chr{chromosome}"
    start = position - 1
    end = start + max(len(reference.strip()), 1)
    return ucsc_chromosome, start, end


def _get_clingen_gene_validity(
    session: requests.Session,
    variant: VariantData,
    gene: str,
    max_retries: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Retrieve exact ClinGen-submitted claims from UCSC's GenCC track."""
    region = _to_clingen_ucsc_region(variant)
    if region is None:
        return None, (
            "ClinGen evidence does not support this chromosome or "
            "coordinate representation."
        )

    chromosome, start, end = region
    assembly = CLINGEN_UCSC_ASSEMBLIES[settings.GENOME_ASSEMBLY]
    endpoint = f"{settings.CLINGEN_BASE_URL}/getData/track"
    params = {
        "genome": assembly,
        "track": "genCC",
        "chrom": chromosome,
        "start": start,
        "end": end,
    }

    for attempt in range(max_retries + 1):
        response: requests.Response | None = None

        try:
            response = session.get(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
                timeout=settings.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                raise AnnotationServiceError(
                    "UCSC GenCC request failed because the service "
                    "was unavailable."
                ) from exc

            LOGGER.warning(
                "Retrying UCSC GenCC after a connection error."
            )
            time.sleep(_retry_delay(attempt))
            continue

        if response.status_code in TRANSIENT_HTTP_STATUSES:
            if attempt < max_retries:
                LOGGER.warning(
                    "Retrying UCSC GenCC after HTTP %s.",
                    response.status_code,
                )
                time.sleep(_retry_delay(attempt, response))
                continue

        if not 200 <= response.status_code < 300:
            raise AnnotationServiceError(
                "UCSC GenCC returned HTTP "
                f"{response.status_code}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AnnotationServiceError(
                "UCSC GenCC returned invalid JSON."
            ) from exc

        if not isinstance(payload, dict):
            raise AnnotationServiceError(
                "UCSC GenCC returned an unexpected response structure."
            )

        if payload.get("error"):
            raise AnnotationServiceError(
                "UCSC GenCC returned an API error."
            )

        records = payload.get("genCC")
        if (
            payload.get("genome") != assembly
            or payload.get("track") != "genCC"
            or payload.get("chrom") != chromosome
            or not isinstance(records, list)
            or any(
                not isinstance(item, dict)
                for item in records
            )
        ):
            raise AnnotationServiceError(
                "UCSC GenCC returned an unexpected response structure."
            )

        exact_matches = [
            item
            for item in records
            if isinstance(item.get("gene_symbol"), str)
            and item["gene_symbol"].strip().casefold() == gene.casefold()
            and isinstance(item.get("submitter_title"), str)
            and item["submitter_title"].strip().casefold() == "clingen"
        ]
        return exact_matches, None

    raise AnnotationServiceError(
        "UCSC GenCC retry loop ended unexpectedly."
    )


# ---------------------------------------------------------------------------
# Response cleaning and unified evidence output
# ---------------------------------------------------------------------------

def _extract_response_token(response: dict[str, Any]) -> str | None:
    """Read the temporary input identifier returned by VEP."""
    raw_input = response.get("input")
    if not isinstance(raw_input, str):
        return None

    fields = raw_input.split()
    if len(fields) < 3 or not fields[2].startswith("cv_"):
        return None

    return fields[2]


def _clean_transcript(
    transcript: dict[str, Any],
) -> dict[str, Any]:
    """Keep only transcript evidence used by later pipeline stages."""
    consequence_terms = transcript.get("consequence_terms")
    if not isinstance(consequence_terms, list):
        consequence_terms = []

    return {
        "gene_symbol": transcript.get("gene_symbol"),
        "gene_id": transcript.get("gene_id"),
        "transcript_id": transcript.get("transcript_id"),
        "biotype": transcript.get("biotype"),
        "consequence_terms": consequence_terms,
        "impact": transcript.get("impact"),
        "hgvsc": transcript.get("hgvsc"),
        "hgvsp": transcript.get("hgvsp"),
        "canonical": transcript.get("canonical") == 1,
        "mane_select": transcript.get("mane_select"),
    }


def _select_representative_transcript(
    transcripts: list[dict[str, Any]],
    most_severe_consequence: str | None,
) -> dict[str, Any] | None:
    """Choose one useful transcript while retaining all cleaned evidence."""
    if not transcripts:
        return None

    severe_matches = [
        transcript
        for transcript in transcripts
        if most_severe_consequence
        in transcript.get("consequence_terms", [])
    ]
    candidates = severe_matches or transcripts

    # Prefer MANE Select and canonical protein-coding transcripts. Impact is
    # only a final tie-breaker; the complete transcript list is still retained.
    return max(
        candidates,
        key=_transcript_priority,
    )


def _transcript_priority(
    transcript: dict[str, Any],
) -> tuple[bool, bool, bool, int]:
    """Rank cleaned transcripts using stable clinical metadata."""
    return (
        bool(transcript.get("mane_select")),
        bool(transcript.get("canonical")),
        transcript.get("biotype") == "protein_coding",
        IMPACT_PRIORITY.get(
            str(transcript.get("impact")),
            0,
        ),
    )


def _limit_transcripts(
    transcripts: list[dict[str, Any]],
    most_severe_consequence: str | None,
) -> list[dict[str, Any]]:
    """Keep a bounded set of the most relevant cleaned transcripts."""
    severe_matches = [
        transcript
        for transcript in transcripts
        if most_severe_consequence
        in transcript.get("consequence_terms", [])
    ]
    candidates = severe_matches or transcripts

    return sorted(
        candidates,
        key=_transcript_priority,
        reverse=True,
    )[:MAX_STORED_TRANSCRIPTS]


def _base_annotation(
    variant: VariantData,
    *,
    status: str,
    warning: str | None = None,
) -> AnnotationData:
    """Build a stable annotation object for success or failure states."""
    warnings_list = [warning] if warning else []

    return {
        "variant": dict(variant),
        "assembly": settings.GENOME_ASSEMBLY,
        "gene": None,
        "gene_id": None,
        "transcript": None,
        "consequence": None,
        "impact": None,
        "protein_change": None,
        "population_frequency": None,
        "sources": {
            "vep": {
                "status": status,
                "most_severe_consequence": None,
                "transcript_consequences": [],
                "total_transcript_consequences": 0,
                "transcripts_truncated": False,
            },
            "myvariant": {
                "status": "pending",
                "variant_id": None,
                "rsid": None,
                "gene": None,
                "population_frequencies": {},
                "max_population_frequency": None,
            },
            "clinvar": {
                "status": "pending",
                "query_hgvs": None,
                "variation_id": None,
                "accession": None,
                "accession_version": None,
                "gene": None,
                "clinical_significance": None,
                "review_status": None,
                "last_evaluated": None,
                "conditions": [],
                "condition_count": 0,
                "conditions_truncated": False,
                "scv_accessions": [],
                "rcv_accessions": [],
            },
            "clingen": {
                "status": "pending",
                "data_provider": "UCSC GenCC",
                "query_gene": None,
                "gene": None,
                "gene_id": None,
                "curations": [],
                "curation_count": 0,
                "curations_truncated": False,
            }
        },
        "references": [
            {
                "source": "Ensembl VEP",
                "url": (
                    f"{settings.VEP_BASE_URL}"
                    "/vep/homo_sapiens/region"
                ),
            }
        ],
        "warnings": warnings_list,
    }


def _standardize_vep_response(
    variant: VariantData,
    response: dict[str, Any],
) -> AnnotationData:
    """Convert raw VEP JSON into the project's unified evidence structure."""
    annotation = _base_annotation(variant, status="success")
    response_assembly = response.get("assembly_name")

    if response_assembly != settings.GENOME_ASSEMBLY:
        message = (
            "Ensembl VEP assembly mismatch: expected "
            f"{settings.GENOME_ASSEMBLY}, received "
            f"{response_assembly or 'unknown'}."
        )
        annotation["sources"]["vep"]["status"] = "error"
        annotation["warnings"].append(message)
        return annotation

    raw_transcripts = response.get("transcript_consequences", [])
    if not isinstance(raw_transcripts, list):
        raw_transcripts = []

    cleaned_transcripts = [
        _clean_transcript(transcript)
        for transcript in raw_transcripts
        if isinstance(transcript, dict)
    ]
    most_severe = response.get("most_severe_consequence")
    if not isinstance(most_severe, str):
        most_severe = None

    representative = _select_representative_transcript(
        cleaned_transcripts,
        most_severe,
    )
    stored_transcripts = _limit_transcripts(
        cleaned_transcripts,
        most_severe,
    )

    annotation["consequence"] = most_severe
    annotation["sources"]["vep"].update(
        {
            "most_severe_consequence": most_severe,
            "transcript_consequences": stored_transcripts,
            "total_transcript_consequences": len(
                cleaned_transcripts
            ),
            "transcripts_truncated": (
                len(stored_transcripts)
                < len(cleaned_transcripts)
            ),
        }
    )

    if representative is None:
        annotation["warnings"].append(
            "Ensembl VEP returned no transcript consequences."
        )
        return annotation

    annotation.update(
        {
            "gene": (
                representative.get("gene_symbol")
                or representative.get("gene_id")
            ),
            "gene_id": representative.get("gene_id"),
            "transcript": representative.get("transcript_id"),
            "impact": representative.get("impact"),
            "protein_change": representative.get("hgvsp"),
        }
    )
    return annotation


def _iter_source_records(value: Any) -> Iterator[dict[str, Any]]:
    """Yield dictionary records from a single or repeated source field."""
    if isinstance(value, dict):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                yield item


def _first_nested_string(
    records: Any,
    *path: str,
) -> str | None:
    """Read the first non-empty string at a nested source path."""
    for record in _iter_source_records(records):
        value: Any = record
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _valid_frequency(value: Any) -> float | None:
    """Return a finite allele frequency in the inclusive range zero to one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    frequency = float(value)
    if not math.isfinite(frequency) or not 0.0 <= frequency <= 1.0:
        return None

    return frequency


def _extract_global_frequency(source: Any) -> float | None:
    """Extract the largest global AF from one MyVariant population source."""
    frequencies: list[float] = []

    for record in _iter_source_records(source):
        raw_frequency = record.get("af")
        if isinstance(raw_frequency, dict):
            raw_frequency = raw_frequency.get("af")

        frequency = _valid_frequency(raw_frequency)
        if frequency is not None:
            frequencies.append(frequency)

    return max(frequencies) if frequencies else None


def _extract_dbsnp_frequencies(
    dbsnp: Any,
    alternate: str,
) -> dict[str, float]:
    """Extract exact-ALT population frequencies from dbSNP aggregation."""
    frequencies: dict[str, float] = {}

    for record in _iter_source_records(dbsnp):
        alleles = record.get("alleles")
        if not isinstance(alleles, list):
            continue

        for allele in alleles:
            if (
                not isinstance(allele, dict)
                or str(allele.get("allele", "")).upper() != alternate
                or not isinstance(allele.get("freq"), dict)
            ):
                continue

            for source_name, raw_frequency in allele["freq"].items():
                frequency = _valid_frequency(raw_frequency)
                if frequency is not None:
                    frequencies[f"dbsnp_{source_name}"] = frequency

    return frequencies


def _standardize_myvariant_response(
    annotation: AnnotationData,
    payload: dict[str, Any],
    variant_id: str,
) -> None:
    """Add bounded MyVariant evidence without retaining its raw payload."""
    dbsnp = payload.get("dbsnp")
    population_frequencies: dict[str, float] = {}

    for source_name in ("gnomad_exome", "gnomad_genome", "exac"):
        frequency = _extract_global_frequency(payload.get(source_name))
        if frequency is not None:
            population_frequencies[source_name] = frequency

    alternate = str(annotation["variant"]["alt"]).strip().upper()
    population_frequencies.update(
        _extract_dbsnp_frequencies(dbsnp, alternate)
    )

    max_frequency = (
        max(population_frequencies.values())
        if population_frequencies
        else None
    )
    gene = (
        _first_nested_string(dbsnp, "gene", "symbol")
        or _first_nested_string(payload.get("dbnsfp"), "genename")
        or _first_nested_string(
            payload.get("cadd"),
            "gene",
            "genename",
        )
    )

    annotation["population_frequency"] = max_frequency
    annotation["sources"]["myvariant"].update(
        {
            "status": "success",
            "variant_id": variant_id,
            "rsid": _first_nested_string(dbsnp, "rsid"),
            "gene": gene,
            "population_frequencies": population_frequencies,
            "max_population_frequency": max_frequency,
        }
    )
    annotation["references"].append(
        {
            "source": "MyVariant.info",
            "url": (
                f"{settings.MYVARIANT_BASE_URL}/variant/"
                f"{quote(variant_id, safe='')}"
                f"?assembly={MYVARIANT_ASSEMBLIES[settings.GENOME_ASSEMBLY]}"
            ),
        }
    )


def _annotate_with_myvariant(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add isolated MyVariant evidence to one existing VEP annotation."""
    try:
        payload, variant_id, unsupported_warning = _get_myvariant(
            session,
            annotation["variant"],
            max_retries,
        )
    except AnnotationServiceError as exc:
        LOGGER.error("MyVariant.info request failed: %s", exc)
        annotation["sources"]["myvariant"]["status"] = "error"
        annotation["warnings"].append(str(exc))
        return

    if unsupported_warning is not None:
        annotation["sources"]["myvariant"]["status"] = "unsupported"
        annotation["warnings"].append(unsupported_warning)
        return

    if payload is None or variant_id is None:
        annotation["sources"]["myvariant"]["status"] = "not_found"
        annotation["warnings"].append(
            "MyVariant.info returned no exact result for this variant."
        )
        return

    _standardize_myvariant_response(
        annotation,
        payload,
        variant_id,
    )


def _bounded_string_list(
    value: Any,
    maximum: int,
) -> list[str]:
    """Keep a bounded, ordered list of unique non-empty strings."""
    if not isinstance(value, list):
        return []

    cleaned: list[str] = []
    for item in value:
        if (
            isinstance(item, str)
            and item.strip()
            and item.strip() not in cleaned
        ):
            cleaned.append(item.strip())

        if len(cleaned) == maximum:
            break

    return cleaned


def _clean_clinvar_conditions(
    classification: Any,
) -> tuple[list[dict[str, Any]], int]:
    """Standardize a bounded set of ClinVar germline conditions."""
    if not isinstance(classification, dict):
        return [], 0

    raw_traits = classification.get("trait_set")
    if not isinstance(raw_traits, list):
        return [], 0

    conditions: list[dict[str, Any]] = []
    total_conditions = 0

    for trait in raw_traits:
        if not isinstance(trait, dict):
            continue

        name = trait.get("trait_name")
        if not isinstance(name, str) or not name.strip():
            continue

        total_conditions += 1
        if len(conditions) >= MAX_CLINVAR_CONDITIONS:
            continue

        identifiers: list[dict[str, str]] = []
        raw_xrefs = trait.get("trait_xrefs")
        if isinstance(raw_xrefs, list):
            for xref in raw_xrefs:
                if not isinstance(xref, dict):
                    continue

                source = xref.get("db_source")
                identifier = xref.get("db_id")
                if (
                    isinstance(source, str)
                    and source.strip()
                    and isinstance(identifier, str)
                    and identifier.strip()
                ):
                    identifiers.append(
                        {
                            "source": source.strip(),
                            "id": identifier.strip(),
                        }
                    )

        conditions.append(
            {
                "name": name.strip(),
                "identifiers": identifiers,
            }
        )

    return conditions, total_conditions


def _standardize_clinvar_response(
    annotation: AnnotationData,
    summary: dict[str, Any],
    hgvs: str,
) -> None:
    """Add bounded direct ClinVar evidence without retaining raw JSON."""
    classification = summary.get("germline_classification")
    if not isinstance(classification, dict):
        classification = {}

    conditions, condition_count = _clean_clinvar_conditions(
        classification
    )
    supporting = summary.get("supporting_submissions")
    if not isinstance(supporting, dict):
        supporting = {}

    gene = summary.get("gene_sort")
    if not isinstance(gene, str) or not gene.strip():
        gene = _first_nested_string(summary.get("genes"), "symbol")

    significance = classification.get("description")
    if not isinstance(significance, str) or not significance.strip():
        significance = None

    review_status = classification.get("review_status")
    if not isinstance(review_status, str) or not review_status.strip():
        review_status = None

    last_evaluated = classification.get("last_evaluated")
    if not isinstance(last_evaluated, str) or not last_evaluated.strip():
        last_evaluated = None

    variation_id = str(summary.get("uid", "")).strip() or None
    accession = summary.get("accession")
    if not isinstance(accession, str) or not accession.strip():
        accession = None

    accession_version = summary.get("accession_version")
    if (
        not isinstance(accession_version, str)
        or not accession_version.strip()
    ):
        accession_version = None

    annotation["sources"]["clinvar"].update(
        {
            "status": "success",
            "query_hgvs": hgvs,
            "variation_id": variation_id,
            "accession": accession,
            "accession_version": accession_version,
            "gene": gene.strip() if isinstance(gene, str) else None,
            "clinical_significance": significance,
            "review_status": review_status,
            "last_evaluated": last_evaluated,
            "conditions": conditions,
            "condition_count": condition_count,
            "conditions_truncated": condition_count > len(conditions),
            "scv_accessions": _bounded_string_list(
                supporting.get("scv"),
                MAX_CLINVAR_ACCESSIONS,
            ),
            "rcv_accessions": _bounded_string_list(
                supporting.get("rcv"),
                MAX_CLINVAR_ACCESSIONS,
            ),
        }
    )

    if variation_id is not None:
        annotation["references"].append(
            {
                "source": "NCBI ClinVar",
                "url": (
                    "https://www.ncbi.nlm.nih.gov/clinvar/variation/"
                    f"{variation_id}/"
                ),
            }
        )


def _annotate_with_clinvar(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add isolated direct ClinVar evidence to one annotation."""
    try:
        summary, _, hgvs, unsupported_warning = _get_clinvar(
            session,
            annotation["variant"],
            max_retries,
        )
    except AnnotationServiceError as exc:
        LOGGER.error("NCBI ClinVar request failed: %s", exc)
        annotation["sources"]["clinvar"]["status"] = "error"
        annotation["warnings"].append(str(exc))
        return

    if unsupported_warning is not None:
        annotation["sources"]["clinvar"]["status"] = "unsupported"
        annotation["warnings"].append(unsupported_warning)
        return

    if summary is None or hgvs is None:
        annotation["sources"]["clinvar"]["status"] = "not_found"
        annotation["warnings"].append(
            "NCBI ClinVar returned no exact result for this variant."
        )
        return

    _standardize_clinvar_response(
        annotation,
        summary,
        hgvs,
    )


def _optional_clingen_string(value: Any) -> str | None:
    """Return a stripped ClinGen scalar or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _standardize_clingen_response(
    annotation: AnnotationData,
    records: list[dict[str, Any]],
    query_gene: str,
) -> None:
    """Add bounded ClinGen-submitted GenCC validity evidence."""
    curations: list[dict[str, Any]] = []
    reference_urls: list[str] = []

    for record in records[:MAX_CLINGEN_CURATIONS]:
        gene = _optional_clingen_string(record.get("gene_symbol"))
        submitter = _optional_clingen_string(record.get("submitter_title"))
        classification = _optional_clingen_string(
            record.get("classification_title")
        )
        disease = _optional_clingen_string(record.get("disease_title"))
        if (
            gene is None
            or gene.casefold() != query_gene.casefold()
            or submitter is None
            or submitter.casefold() != "clingen"
            or classification is None
            or disease is None
        ):
            raise AnnotationServiceError(
                "UCSC GenCC returned an incomplete validity claim."
            )

        curation_id = (
            _optional_clingen_string(record.get("sgc_id"))
            or _optional_clingen_string(record.get("sub_submission_id"))
        )
        report_url = _optional_clingen_string(
            record.get("sub_public_report_url")
        )
        raw_pmids = _optional_clingen_string(record.get("sub_pmids"))
        pmids = list(
            dict.fromkeys(
                value.strip()
                for value in (raw_pmids or "").split(",")
                if value.strip().isdigit()
            )
        )[:MAX_CLINGEN_PMIDS]
        curations.append(
            {
                "curation_id": curation_id,
                "disease": disease,
                "disease_id": _optional_clingen_string(
                    record.get("disease_curie")
                ),
                "classification": classification,
                "classification_id": _optional_clingen_string(
                    record.get("classification_curie")
                ),
                "mode_of_inheritance": _optional_clingen_string(
                    record.get("moi_title")
                ),
                "mode_of_inheritance_id": _optional_clingen_string(
                    record.get("moi_curie")
                ),
                "classification_date": _optional_clingen_string(
                    record.get("sub_date")
                ),
                "submitter": submitter,
                "criteria_url": _optional_clingen_string(
                    record.get("sub_assertion_criteria_url")
                ),
                "submission_id": _optional_clingen_string(
                    record.get("sub_submission_id")
                ),
                "pmids": pmids,
                "report_url": report_url,
            }
        )
        if report_url is not None:
            reference_urls.append(report_url)

    clingen = annotation["sources"]["clingen"]
    clingen.update(
        {
            "status": "success",
            "query_gene": query_gene,
            "gene": _optional_clingen_string(records[0].get("gene_symbol")),
            "gene_id": _optional_clingen_string(
                records[0].get("gene_curie")
            ),
            "curations": curations,
            "curation_count": len(records),
            "curations_truncated": len(records) > len(curations),
        }
    )

    for report_url in reference_urls:
        annotation["references"].append(
            {
                "source": (
                    "ClinGen Gene-Disease Validity via UCSC GenCC"
                ),
                "url": report_url,
            }
        )


def _annotate_with_clingen(
    annotation: AnnotationData,
    session: requests.Session,
    max_retries: int,
) -> None:
    """Add isolated ClinGen gene-disease validity evidence."""
    gene = _select_clingen_gene(annotation)
    if gene is None:
        annotation["sources"]["clingen"]["status"] = "not_applicable"
        annotation["warnings"].append(
            "ClinGen was not queried because no unambiguous gene symbol "
            "was available."
        )
        return

    annotation["sources"]["clingen"]["query_gene"] = gene

    try:
        records, unsupported_warning = _get_clingen_gene_validity(
            session,
            annotation["variant"],
            gene,
            max_retries,
        )
        if unsupported_warning is not None:
            annotation["sources"]["clingen"]["status"] = "unsupported"
            annotation["warnings"].append(unsupported_warning)
            return

        if records is None or not records:
            annotation["sources"]["clingen"]["status"] = "not_found"
            annotation["warnings"].append(
                "UCSC GenCC returned no exact ClinGen validity claims "
                f"for {gene} at this locus."
            )
            return

        _standardize_clingen_response(
            annotation,
            records,
            gene,
        )
    except AnnotationServiceError as exc:
        LOGGER.error("UCSC GenCC request failed: %s", exc)
        annotation["sources"]["clingen"]["status"] = "error"
        annotation["warnings"].append(str(exc))


# ---------------------------------------------------------------------------
# Public annotation entry point
# ---------------------------------------------------------------------------

def annotate_variants(
    variants: Iterable[VariantData],
    *,
    batch_size: int | None = None,
    max_retries: int | None = None,
    session: requests.Session | None = None,
) -> list[AnnotationData]:
    """Annotate with VEP, MyVariant, ClinVar, and ClinGen evidence.

    Each external source fails independently and raw source payloads are not
    retained in the returned standardized evidence.
    """
    resolved_batch_size = _resolve_batch_size(batch_size)
    resolved_retries = _resolve_retries(max_retries)
    active_session = session or requests.Session()
    owns_session = session is None
    annotations: list[AnnotationData] = []

    try:
        for batch in _iter_batches(variants, resolved_batch_size):
            vep_inputs = [
                _to_vep_input(token, variant)
                for token, variant in batch
            ]

            try:
                responses = _post_vep_batch(
                    active_session,
                    vep_inputs,
                    resolved_retries,
                )
            except AnnotationServiceError as exc:
                LOGGER.error("Ensembl VEP batch failed: %s", exc)
                annotations.extend(
                    _base_annotation(
                        variant,
                        status="error",
                        warning=str(exc),
                    )
                    for _, variant in batch
                )
                continue

            responses_by_token = {
                token: response
                for response in responses
                if (token := _extract_response_token(response))
            }

            for token, variant in batch:
                response = responses_by_token.get(token)

                if response is None:
                    annotations.append(
                        _base_annotation(
                            variant,
                            status="not_found",
                            warning=(
                                "Ensembl VEP returned no result for "
                                "this variant."
                            ),
                        )
                    )
                    continue

                annotations.append(
                    _standardize_vep_response(
                        variant,
                        response,
                    )
                )

        for annotation in annotations:
            _annotate_with_myvariant(
                annotation,
                active_session,
                resolved_retries,
            )
            _annotate_with_clinvar(
                annotation,
                active_session,
                resolved_retries,
            )
            _annotate_with_clingen(
                annotation,
                active_session,
                resolved_retries,
            )
    finally:
        if owns_session:
            active_session.close()

    return annotations
