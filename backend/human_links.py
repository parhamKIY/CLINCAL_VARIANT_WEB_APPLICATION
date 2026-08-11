"""Stage 93 deterministic human-facing link resolution policy."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import quote, urlsplit


PMID_PATTERN = re.compile(r"[1-9][0-9]{0,8}")
PMCID_PATTERN = re.compile(r"PMC[1-9][0-9]*", re.I)
DOI_PATTERN = re.compile(r"10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+", re.I)
CLINVAR_PATTERN = re.compile(
    r"(?:VCV|RCV|SCV)[0-9]{9}(?:\.[0-9]+)?",
    re.I,
)
RSID_PATTERN = re.compile(r"rs[1-9][0-9]*", re.I)
CHROMOSOME_PATTERN = re.compile(r"(?:[1-9]|1[0-9]|2[0-2]|X|Y|MT)", re.I)
ALLELE_PATTERN = re.compile(r"[ACGT]+", re.I)


def resolve_pubmed_human_url(identifier: object) -> str | None:
    value = str(identifier).strip() if identifier is not None else ""
    value = re.sub(r"^PMID\s*:\s*", "", value, flags=re.I)
    match = PMID_PATTERN.fullmatch(value)
    return (
        f"https://pubmed.ncbi.nlm.nih.gov/{match.group(0)}/"
        if match is not None
        else None
    )


def resolve_pmc_human_url(identifier: object) -> str | None:
    value = str(identifier).strip() if identifier is not None else ""
    match = PMCID_PATTERN.fullmatch(value)
    return (
        f"https://pmc.ncbi.nlm.nih.gov/articles/{match.group(0).upper()}/"
        if match is not None
        else None
    )


def resolve_doi_human_url(identifier: object) -> str | None:
    value = str(identifier).strip() if identifier is not None else ""
    if value.casefold().startswith("https://doi.org/"):
        value = value[len("https://doi.org/") :]
    match = DOI_PATTERN.fullmatch(value)
    return (
        f"https://doi.org/{quote(match.group(0), safe='/-._;():')}"
        if match is not None
        else None
    )


def resolve_clinvar_human_url(accession: object) -> str | None:
    value = str(accession).strip().upper() if accession is not None else ""
    if CLINVAR_PATTERN.fullmatch(value) is None:
        return None
    if value.startswith("VCV"):
        variation_id = str(int(value[3:].split(".", 1)[0]))
        return f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{variation_id}/"
    return f"https://www.ncbi.nlm.nih.gov/clinvar/?term={quote(value, safe='')}"


def resolve_genebe_human_url(
    *,
    assembly: object,
    variant: object,
) -> tuple[str, str] | None:
    """Return a GeneBe page and stable path identifier for an exact allele."""

    build = (
        {"GRCh38": "hg38", "GRCh37": "hg19"}.get(assembly)
        if isinstance(assembly, str)
        else None
    )
    if build is None or not isinstance(variant, Mapping):
        return None
    chrom = str(variant.get("chrom", "")).removeprefix("chr").upper()
    pos = variant.get("pos")
    ref = str(variant.get("ref", "")).upper()
    alt = str(variant.get("alt", "")).upper()
    if (
        CHROMOSOME_PATTERN.fullmatch(chrom) is None
        or isinstance(pos, bool)
        or not isinstance(pos, int)
        or pos < 1
        or ALLELE_PATTERN.fullmatch(ref) is None
        or ALLELE_PATTERN.fullmatch(alt) is None
    ):
        return None
    identifier = f"{chrom}-{pos}-{ref}-{alt}"
    return (
        f"https://genebe.net/variant/{build}/{quote(identifier, safe='-')}",
        identifier,
    )


def resolve_ensembl_human_url(
    *,
    assembly: object,
    identifier: object,
) -> str | None:
    value = str(identifier).strip() if identifier is not None else ""
    if RSID_PATTERN.fullmatch(value) is None:
        return None
    host = (
        "grch37.ensembl.org"
        if assembly == "GRCh37"
        else "www.ensembl.org"
        if assembly == "GRCh38"
        else None
    )
    if host is None:
        return None
    return (
        f"https://{host}/Homo_sapiens/Variation/Explore?"
        f"v={quote(value, safe='')}"
    )


def is_machine_readable_url(source: object, url: object) -> bool:
    """Identify API/raw-data targets that must not look like normal records."""

    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return True
    host = (parsed.hostname or "").casefold().rstrip(".")
    path = parsed.path.casefold().rstrip("/")
    source_name = str(source or "").casefold()
    if host in {"api.genebe.net", "rest.ensembl.org"}:
        return True
    if host in {"genebe.net", "www.genebe.net"} and path.startswith(
        "/api-public/"
    ):
        return True
    if host in {"myvariant.info", "www.myvariant.info"}:
        return True
    if host == "eutils.ncbi.nlm.nih.gov" or "/entrez/eutils" in path:
        return True
    return (
        "ensembl vep" in source_name
        and path.endswith("/vep/homo_sapiens/region")
    )


__all__ = [
    "is_machine_readable_url",
    "resolve_clinvar_human_url",
    "resolve_doi_human_url",
    "resolve_ensembl_human_url",
    "resolve_genebe_human_url",
    "resolve_pmc_human_url",
    "resolve_pubmed_human_url",
]
