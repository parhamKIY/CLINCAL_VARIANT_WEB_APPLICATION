"""Optional Stage 94 live reachability checks, excluded from offline CI."""

from __future__ import annotations

import os

import pytest
import requests

from backend.human_links import resolve_genebe_human_url
from backend.references import canonicalize_reference


pytestmark = [
    pytest.mark.stage94_reference_validation,
    pytest.mark.live_provider,
]


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_PROVIDER_TESTS") != "1",
    reason="Set RUN_LIVE_PROVIDER_TESTS=1 to probe human reference pages.",
)
def test_representative_human_reference_pages_are_reachable() -> None:
    genebe = resolve_genebe_human_url(
        assembly="GRCh38",
        variant={"chrom": "13", "pos": 52152671, "ref": "T", "alt": "C"},
    )
    assert genebe is not None
    urls = [
        canonicalize_reference(
            source="PubMed",
            identifier="25741868",
        )["canonical_url"],
        canonicalize_reference(
            source="PubMed Central",
            identifier="PMC4382204",
        )["canonical_url"],
        canonicalize_reference(
            source="DOI",
            identifier="10.1038/nature12373",
        )["canonical_url"],
        canonicalize_reference(
            source="NCBI ClinVar",
            identifier="VCV000033448.1",
        )["canonical_url"],
        genebe[0],
    ]

    with requests.Session() as session:
        session.headers["User-Agent"] = "clinical-variant-stage94-validation/1.0"
        for url in urls:
            assert url is not None
            response = session.get(
                url,
                allow_redirects=True,
                stream=True,
                timeout=15,
            )
            try:
                assert response.status_code < 400, url
                assert "application/json" not in response.headers.get(
                    "Content-Type", ""
                ).casefold(), url
            finally:
                response.close()
