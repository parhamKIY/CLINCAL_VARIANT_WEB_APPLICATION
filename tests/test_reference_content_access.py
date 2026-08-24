"""Regression coverage for bounded in-app literature reference access."""

from __future__ import annotations

import json

import pytest
from streamlit.testing.v1 import AppTest

from backend.reference_content import fetch_literature_reference_content
from backend.references import canonicalize_reference
from frontend.reference_access import render_reference_access_panel


class _Response:
    def __init__(
        self,
        *,
        content: bytes = b"",
        json_payload: object | None = None,
        status_code: int = 200,
        content_type: str = "application/xml",
    ) -> None:
        self.content = content
        self._json_payload = json_payload
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def json(self) -> object:
        if self._json_payload is None:
            raise ValueError("No JSON payload")
        return self._json_payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def close(self) -> None:
        return None


def _pubmed_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>8876179</PMID>
      <Article>
        <ArticleTitle>UV-induced ubiquitination of RNA polymerase II.</ArticleTitle>
        <Abstract><AbstractText>Bounded public abstract text.</AbstractText></Abstract>
        <AuthorList>
          <Author><LastName>Bregman</LastName><Initials>DB</Initials></Author>
        </AuthorList>
        <Journal>
          <Title>Proceedings of the National Academy of Sciences</Title>
          <JournalIssue><PubDate><Year>1996</Year><Month>Oct</Month></PubDate></JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">8876179</ArticleId>
      <ArticleId IdType="pmc">PMC38101</ArticleId>
      <ArticleId IdType="doi">10.1073/pnas.93.21.11586</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


def test_pubmed_reference_content_loads_through_official_api() -> None:
    reference = canonicalize_reference(source="PubMed", identifier="8876179")
    session = _Session([_Response(content=_pubmed_xml())])

    content = fetch_literature_reference_content(reference, session=session)

    assert content["status"] == "available"
    assert content["title"] == "UV-induced ubiquitination of RNA polymerase II."
    assert content["abstract"] == "Bounded public abstract text."
    assert content["authors"] == ["Bregman DB"]
    assert content["pmid"] == "8876179"
    assert content["pmcid"] == "PMC38101"
    assert content["doi"] == "10.1073/pnas.93.21.11586"
    assert len(session.calls) == 1
    assert session.calls[0]["params"] == {
        "db": "pubmed",
        "id": "8876179",
        "retmode": "xml",
        "tool": "clinical_variant_app",
    }


@pytest.mark.parametrize(
    ("source", "identifier", "query"),
    [
        ("PubMed Central", "PMC38101", "PMC38101[PMCID]"),
        ("DOI", "10.1073/pnas.93.21.11586", '"10.1073/pnas.93.21.11586"[AID]'),
    ],
)
def test_pmcid_and_doi_resolve_to_exact_pubmed_record(
    source: str,
    identifier: str,
    query: str,
) -> None:
    reference = canonicalize_reference(source=source, identifier=identifier)
    search = {
        "esearchresult": {
            "count": "1",
            "idlist": ["8876179"],
        }
    }
    session = _Session(
        [
            _Response(
                json_payload=search,
                content=json.dumps(search).encode("utf-8"),
                content_type="application/json",
            ),
            _Response(content=_pubmed_xml()),
        ]
    )

    content = fetch_literature_reference_content(reference, session=session)

    assert content["status"] == "available"
    assert content["pmid"] == "8876179"
    assert session.calls[0]["params"] == {
        "db": "pubmed",
        "retmode": "json",
        "retmax": 1,
        "term": query,
        "tool": "clinical_variant_app",
    }


def test_reference_content_failure_is_bounded_and_safe() -> None:
    reference = canonicalize_reference(source="PubMed", identifier="8876179")
    session = _Session(
        [_Response(status_code=503), _Response(status_code=503)]
    )

    content = fetch_literature_reference_content(
        reference,
        session=session,
        sleep=lambda _seconds: None,
    )

    assert content["status"] == "unavailable"
    assert content["failure_category"] == "provider_unavailable"
    assert content["title"] is None
    assert "503" not in str(content)


def test_malformed_provider_payload_is_not_rendered_as_reference_content() -> None:
    reference = canonicalize_reference(source="PubMed", identifier="8876179")
    session = _Session([_Response(content=b"<not-pubmed />")])

    content = fetch_literature_reference_content(reference, session=session)

    assert content["status"] == "invalid_response"
    assert content["failure_category"] == "invalid_provider_response"
    assert content["abstract"] is None


def test_resolved_reference_identifier_must_match_requested_doi() -> None:
    reference = canonicalize_reference(
        source="DOI",
        identifier="10.1073/pnas.93.21.99999",
    )
    search = {"esearchresult": {"count": "1", "idlist": ["8876179"]}}
    session = _Session(
        [
            _Response(
                json_payload=search,
                content=json.dumps(search).encode("utf-8"),
                content_type="application/json",
            ),
            _Response(content=_pubmed_xml()),
        ]
    )

    content = fetch_literature_reference_content(reference, session=session)

    assert content["status"] == "invalid_response"
    assert content["failure_category"] == "identifier_mismatch"
    assert content["title"] is None


def _reference_access_app() -> None:
    from backend.references import canonicalize_reference
    from frontend.reference_access import render_reference_access_panel

    def available_content(_reference):
        return {
            "status": "available",
            "failure_category": None,
            "provider": "PubMed E-utilities",
            "identifier_type": "PMID",
            "identifier": "8876179",
            "pmid": "8876179",
            "pmcid": "PMC38101",
            "doi": "10.1073/pnas.93.21.11586",
            "title": "UV-induced ubiquitination of RNA polymerase II.",
            "authors": ["Bregman DB"],
            "journal": "Proceedings of the National Academy of Sciences",
            "publication_date": "1996 Oct",
            "abstract": "Bounded public abstract text.",
        }

    reference = canonicalize_reference(source="PubMed", identifier="8876179")
    render_reference_access_panel(
        [reference],
        [],
        report_id="dvr-0-test",
        content_loader=available_content,
    )


def test_reference_panel_has_native_link_and_in_app_api_content() -> None:
    app = AppTest.from_function(_reference_access_app).run(timeout=10)

    assert not app.exception
    assert len(app.get("link_button")) == 1
    assert app.get("link_button")[0].url == (
        "https://pubmed.ncbi.nlm.nih.gov/8876179/"
    )
    load_button = next(
        button for button in app.button if button.label == "Load details here"
    )

    load_button.click().run(timeout=10)

    assert not app.exception
    rendered = "\n".join(item.value for item in app.markdown)
    assert "UV-induced ubiquitination of RNA polymerase II." in rendered
    assert "Bounded public abstract text." in rendered
