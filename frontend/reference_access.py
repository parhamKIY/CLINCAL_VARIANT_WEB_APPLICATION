"""Native Streamlit access to external links and bounded API previews."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence

import streamlit as st

from backend.reference_content import (
    LiteratureReferenceContent,
    fetch_literature_reference_content,
)
from backend.references import (
    CanonicalReferenceError,
    validate_canonical_reference,
    validated_reference_url,
)


ReferenceContentLoader = Callable[
    [Mapping[str, object]],
    Mapping[str, object],
]


@st.cache_data(ttl="1h", max_entries=128, show_spinner=False)
def _cached_reference_content(
    reference: Mapping[str, object],
) -> LiteratureReferenceContent:
    return fetch_literature_reference_content(reference)


def _widget_suffix(report_id: str, *values: object) -> str:
    material = "|".join([report_id, *(str(value) for value in values)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _safe_text(value: object, *, maximum: int = 12_000) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    if not normalized or any(ord(character) < 32 for character in normalized):
        return None
    return normalized[:maximum]


def _render_loaded_content(content: Mapping[str, object]) -> None:
    status = content.get("status")
    if status != "available":
        messages = {
            "no_match": "The official literature API found no matching record.",
            "unavailable": (
                "The official literature API is temporarily unavailable. "
                "The retained citation remains unchanged."
            ),
            "invalid_response": (
                "The provider returned an unusable record. Nothing from that "
                "response was displayed."
            ),
            "unsupported": (
                "An in-app preview is not supported for this reference type."
            ),
        }
        st.warning(messages.get(str(status), "Reference details are unavailable."))
        return

    title = _safe_text(content.get("title"), maximum=1_000)
    if title:
        st.caption("Title")
        st.write(title)
    authors = content.get("authors")
    retained_authors = (
        [
            name
            for item in authors[:20]
            if (name := _safe_text(item, maximum=200)) is not None
        ]
        if isinstance(authors, list)
        else []
    )
    metadata = [
        value
        for value in (
            ", ".join(retained_authors) if retained_authors else None,
            _safe_text(content.get("journal"), maximum=500),
            _safe_text(content.get("publication_date"), maximum=100),
        )
        if value
    ]
    if metadata:
        st.caption(" · ".join(metadata))
    abstract = _safe_text(content.get("abstract"))
    if abstract:
        st.caption("Abstract")
        st.write(abstract)
    else:
        st.caption("The official API did not provide an abstract for this record.")
    identifiers = [
        f"{label}: {value}"
        for label, field in (("PMID", "pmid"), ("PMCID", "pmcid"), ("DOI", "doi"))
        if (value := _safe_text(content.get(field), maximum=300))
    ]
    if identifiers:
        st.caption(" · ".join(identifiers))


def render_reference_access_panel(
    literature_references: Sequence[Mapping[str, object]],
    data_sources: Sequence[Mapping[str, object]],
    *,
    report_id: str,
    content_loader: ReferenceContentLoader = _cached_reference_content,
) -> None:
    """Render reliable native links plus an API-backed literature preview."""

    with st.expander("Reference access", expanded=True):
        st.caption(
            "External pages and provider APIs are separate services. If a "
            "literature page is unavailable, load its public record details "
            "inside the app through the official API."
        )
        if not literature_references and not data_sources:
            st.caption("No reference or data-source link is available.")
            return

        if literature_references:
            st.markdown("**Literature references**")
        for raw_reference in literature_references:
            try:
                reference = validate_canonical_reference(raw_reference)
            except CanonicalReferenceError:
                st.warning("One retained reference could not be opened safely.")
                continue
            reference_id = reference["reference_id"]
            identifier_type = reference["identifier_type"] or "Reference"
            identifier = reference["identifier"] or "Identifier unavailable"
            st.write(f"[{reference_id}] {identifier_type}: {identifier}")
            suffix = _widget_suffix(
                report_id,
                reference_id,
                identifier_type,
                identifier,
            )
            with st.container(horizontal=True):
                target = validated_reference_url(reference["canonical_url"])
                if target is not None and reference["url_status"] == "validated":
                    st.link_button(
                        "Open source page",
                        target,
                        key=f"reference_external_{suffix}",
                        icon=":material/open_in_new:",
                    )
                load_clicked = st.button(
                    "Load details here",
                    key=f"reference_load_{suffix}",
                    icon=":material/article:",
                    disabled=reference["identifier_type"] not in {"PMID", "PMCID", "DOI"},
                )
            state_key = f"reference_content_{suffix}"
            if load_clicked:
                with st.spinner("Loading public reference details..."):
                    st.session_state[state_key] = dict(content_loader(reference))
            retained = st.session_state.get(state_key)
            if isinstance(retained, Mapping):
                with st.container(border=True):
                    _render_loaded_content(retained)

        linked_sources = [
            (source, validated_reference_url(source.get("human_url")))
            for source in data_sources
            if isinstance(source, Mapping)
        ]
        linked_sources = [
            (source, target)
            for source, target in linked_sources
            if target is not None and source.get("link_status") == "validated"
        ]
        if linked_sources:
            st.markdown("**Provider records**")
        for source, target in linked_sources:
            source_name = _safe_text(source.get("source"), maximum=200) or "Provider"
            record_identifier = _safe_text(
                source.get("record_identifier"),
                maximum=500,
            )
            label = (
                f"{source_name} — {record_identifier}"
                if record_identifier
                else source_name
            )
            st.link_button(
                f"Open {label}",
                str(target),
                key=f"provider_external_{_widget_suffix(report_id, source_name, target)}",
                icon=":material/open_in_new:",
            )


__all__ = ["render_reference_access_panel"]
