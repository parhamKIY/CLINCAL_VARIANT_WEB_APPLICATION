"""Bounded input/output checks; these do not adjudicate ACMG criteria."""
from collections.abc import Mapping
import math
import re

from backend.conflict_auditor import normalize_source_classification_label


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _text(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() not in {
        "", "unknown", "unavailable", "not_available", "not_reported", "none", "n/a",
    }


def _predictor_signal(value: object) -> bool:
    for key, item in _mapping(value).items():
        if key == "compaction":
            continue
        if key.endswith("score") and _number(item):
            return True
        if key.endswith("prediction") and _text(item):
            return True
        if isinstance(item, Mapping) and _predictor_signal(item):
            return True
    return False


def _frequency_signal(value: object) -> bool:
    for key, item in _mapping(value).items():
        if key in {"population_frequency", "allele_frequency", "af", "max_af",
                   "global_maf", "popmax_frequency"} and _number(item):
            return True
        if isinstance(item, Mapping) and _frequency_signal(item):
            return True
    return False


def has_assessment_material(evidence: Mapping) -> bool:
    """Exclude identity/context-only input, not sparse assessable variants.

    Presence permits evaluation, never proves that a particular class is justified.
    The model may still abstain when the meaning/quality of this material is inadequate.
    This operates only on the sanitized semantic projection used for the prompt.
    """
    pathogenicity = _mapping(evidence.get("pathogenicity"))
    if any(normalize_source_classification_label(pathogenicity.get(field)) for field in (
        "automated_acmg_classification", "clinvar_classification",
    )):
        return True
    if pathogenicity.get("acmg_criteria"):
        return True
    expert = _mapping(pathogenicity.get("expert_curated_variant_context"))
    if any(
        normalize_source_classification_label(_mapping(record).get("classification"))
        or _mapping(record).get("met_codes")
        for record in expert.get("records", [])
    ):
        return True
    if _text(evidence.get("consequence")) or _number(evidence.get("population_frequency")):
        return True
    annotations = _mapping(evidence.get("annotations"))
    vep = _mapping(annotations.get("vep"))
    resolution = _mapping(vep.get("gene_identity_resolution"))
    verified_by = _mapping(resolution.get("provenance")).get("verified_by")
    verification = _mapping(vep.get("gene_identity_verification"))
    for protein in (
        _mapping(evidence.get("variant_context")).get("hgvs_p"),
        resolution.get("hgvs_p") if resolution.get("status") == "resolved" else None,
        verification.get("validated_protein_hgvs")
        if resolution.get("status") == "resolved" and verified_by == "VariantValidator"
        else None,
    ):
        if isinstance(protein, str) and re.search(r"p\.\(?[A-Za-z*]+\d+", protein):
            return True
    if verified_by == "GeneBe" and _text(_mapping(annotations.get("genebe")).get("effect")):
        return True
    population_annotations = _mapping(annotations.get("population"))
    derived = _mapping(population_annotations.get("clinvar_derived"))
    if normalize_source_classification_label(derived.get("clinical_significance")):
        return True
    if _predictor_signal(annotations.get("predictors")):
        return True
    enrichment = _mapping(evidence.get("conditional_enrichment"))
    population = _mapping(enrichment.get("population_frequency"))
    if _frequency_signal(population) or _frequency_signal(population_annotations):
        return True
    literature = _mapping(enrichment.get("literature"))
    # Titles, PMIDs, availability states and counts alone are not scientific findings.
    return any(
        _text(_mapping(article).get(field))
        for article in literature.get("articles", [])
        for field in ("abstract", "evidence_summary")
    )


_CLASS = r"(?P<label>likely pathogenic|likely benign|pathogenic|benign|uncertain significance|VUS)"
_CONCLUSION = re.compile(
    r"^(?:(?:overall|therefore|in summary)[,:]?\s+)?(?:"
    r"(?:the|this) variant (?:is|remains) (?:classified as |considered |a variant of )?"
    r"|(?:our |the )?(?:draft |overall )?classification (?:is|:)\s*)"
    + _CLASS + r"\b", re.IGNORECASE,
)
_ATTRIBUTION = re.compile(
    r"\b(?:according to|reported by|classified by|listed in|in) "
    r"(?:ClinVar|GeneBe|ERepo|(?:the )?(?:source|provider))\b",
    re.IGNORECASE,
)
_UNASSESSABLE = re.compile(
    r"^(?:the (?:available|supplied) evidence is insufficient for "
    r"(?:any|a meaningful) classification|"
    r"there is insufficient evidence to meaningfully classify (?:this|the) variant)\b",
    re.IGNORECASE,
)


def core_output_issue(classification: str | None, interpretation: str) -> str | None:
    """Catch empty prose and explicit conflicting conclusions, not arbitrary claims."""
    prose = re.sub(r"\[R[1-9][0-9]*\]", "", interpretation)
    # No arbitrary word-count minimum: short clinical prose may be meaningful.
    if not re.search(r"[^\W\d_]", prose, flags=re.UNICODE) or (
        prose.strip().casefold() in {"n/a", "unknown", "not available"}
    ) or normalize_source_classification_label(prose.strip().rstrip(".")):
        return "non_substantive_interpretation"
    if classification is None:
        return None
    for sentence in re.split(r"[.!?\n]+", prose):
        sentence = sentence.strip()
        if _UNASSESSABLE.match(sentence):
            return "classification_narrative_mismatch"
        match = _CONCLUSION.match(sentence)
        if match and not _ATTRIBUTION.search(sentence):
            label = match["label"].casefold()
            if label == "vus":
                label = "uncertain significance"
            if label != classification.casefold():
                return "classification_narrative_mismatch"
    return None
