# ClinGen ERepo Exact-Identity Contract

**Status:** COMPLETE. **Review:** PENDING. Frozen on 2026-08-16. This is a
contract and fixture specification only. It authorizes no ERepo production
client, EvidenceObject field, persistence change, readiness change, or LLM
payload change.

## 1. Provider role and fixed endpoint surface

ClinGen Evidence Repository (ERepo) contributes source-attributed
expert_curated_variant_context. It is not a GeneBe replacement, automated
ACMG classifier, ClinVar replacement, or independent biological vote.

    GET https://erepo.genome.network/evrepo/api/summary/classifications
    GET https://erepo.genome.network/evrepo/api/summary/classification/{uuid}/doc/sepio/version/{docVersion}

The summary endpoint performs candidate discovery. The detailed endpoint is
allowed only after an accepted summary candidate identifies its uuid and full
semantic docVersion. No raw ERepo response, including a SEPIO document, may
be sent to the interpretation LLM.

## 2. Bounded discovery identity

Discovery locates candidate records; it is never acceptance. Every discovery
identifier must originate from a validated VariantIdentifierBundle, retain its
identifier provenance, run once per unique value, and return at most 10
candidates. Candidate classifications and evidence remain unusable until
Section 4 succeeds.

### 2.1 Preferred — exact assembly-specific genomic HGVS

    GET /evrepo/api/summary/classifications?
      columns=hgvs&
      values={percent-encoded genomic HGVS}&
      matchTypes=exact&
      matchMode=and&
      pg=1&
      pgSize=10

Use a RefSeq genomic NC_...:g.... value only when it represents the declared
input assembly, chromosome, position, REF, and ALT and has allele-scope
deterministic_normalization or provider_exact_allele provenance.

### 2.2 Secondary — exact ClinVar Variation ID

When an unused clinvar_variation_ids value has allele-scope
provider_exact_allele provenance, query columns=cvId with the same exact,
bounded parameters. Use at most three stable, de-duplicated values per variant.
A matching cvId discovers a record only: it can be shared, derived, or stale
and cannot prove the ERepo candidate's allele.

### 2.3 Secondary — exact transcript HGVS

When an unused transcript_hgvs value has transcript-scope
provider_transcript_annotation provenance and a versioned RefSeq or Ensembl
transcript accession, query columns=hgvs with matchTypes=exact. Use at most
three stable, de-duplicated transcript HGVS values per variant. Transcript,
gene, protein, condition, or assertion agreement is discovery only and cannot
prove the genomic allele.

### 2.4 Future only — validated CAID

A columns=caId query is prohibited until the project stores a CAID in a
versioned, allele-scope provider_exact_allele identifier field. The current
bundle has no CAID field, so Stage 2 must not invent, scrape, or infer one. A
future returned CAID must equal the stored CAID and still pass Section 4.

GRCh37 and GRCh38 are never substituted. Gene, rsID, condition, MONDO,
protein-HGVS, and free-text searching are not approved discovery strategies.

## 3. Summary and detailed response contract

Successful summary discovery requires:

    data: non-empty list
    metadata: object
    status: { code: 200, name: "OK" }

Each candidate considered for acceptance requires bounded non-empty uuid,
caId, docVersion, boolean retracted, and list-of-string hgvs. Preserve
available classification, condition, mondoId, moi, ep, approvedDate,
publishedDate, metCodes, unMetCodes, cvId, preferredVarTitle, and summaryDesc.
Missing an identity/document-locator field rejects that candidate as malformed.

The detailed request must use the accepted summary's full-semantic docVersion,
not a shortened major/minor version. Its response must be an object (not a
version-list array) with data.uuid, identity-bearing variant, condition,
statementOutcome, assertionMethod, and metadata.

data.metadata.version is the deterministic returned detailed-document version.
It must equal summary docVersion exactly. data.@id must end in
/doc/sepio/version/{data.metadata.version} and data.uuid must equal summary
uuid; these are redundant consistency checks. The retrieval timestamp,
assertionMethod.version, and versionsList do not prove document version.
Missing or unequal data.metadata.version is deterministically
detail_version_mismatch.

## 4. Exact acceptance identity

A discovered candidate has exactly one outcome:

    EXACT_MATCH
    EXACT_MATCH_EQUIVALENT_REPRESENTATION
    REJECTED

EXACT_MATCH requires the candidate to contain the selected build-specific
expected genomic HGVS after whitespace trimming and ASCII case normalization,
with no normalized CPRA representation change.

EXACT_MATCH_EQUIVALENT_REPRESENTATION is valid only when existing
deterministic normalization proves the same normalized allele. It covers, for
example, shared VCF padding removed by backend.annotation._normalize_variant_edit()
and formatted by _format_genomic_hgvs() when all values below match exactly:

    assembly
    normalized chromosome
    normalized position
    normalized REF
    normalized ALT
    candidate genomic HGVS == deterministic normalized genomic HGVS

Record the normalized CPRA tuple and normalizer provenance in this state. This
does not permit a left-shift inference: an indel with
left_normalization_status=unverified_without_reference may use only the
deterministically trimmed representation. A different left-aligned or other
alternate representation is representation_unresolved until a reference-aware
normalizer proves the same normalized tuple.

Both accepted states additionally require that the candidate is not retracted,
has valid uuid, caId, and docVersion, and that its detailed document has the
accepted uuid and verified data.metadata.version. The accepted records remain
condition-separated evidence; never select the first candidate.

Reject and retain a bounded reason when any condition below applies:

    missing_identity_field
    malformed_field_type
    retracted_record
    assembly_mismatch
    coordinate_mismatch
    reference_mismatch
    alternate_mismatch
    hgvs_mismatch
    representation_unresolved
    detail_uuid_mismatch
    detail_version_mismatch
    malformed_detail_document

Gene, condition, MONDO, ClinVar Variation ID, assertion outcome, or expert
panel agreement can corroborate a candidate but cannot repair an identity
mismatch. Explicit shared upstream identifiers are required before declaring
correlation or independence.

## 5. Per-strategy outcomes and final no_match

Every eligible strategy preserves independently: strategy ID, query identifier
and provenance, endpoint, timestamp, HTTP/retrieval state, candidate count,
candidate-rejection reasons, and whether it produced an accepted record.

One strategy's valid no-match is not provider-level final no_match. Continue
through every remaining eligible approved strategy unless one produces an
accepted record.

Final provider-level no_match is valid only if every eligible approved strategy
completes with this structured response and none has an accepted record:

    {"status":{"code":404,"name":"Not Found","msg":"No records were found for given query"}}

A malformed 200, unexpected status, timeout, network failure, 403, 429,
unattempted strategy, or rejected candidate is not final no_match. If all
discovered candidates are rejected, retain identity_mismatch or
representation_unresolved rather than converting it to no_match.

## 6. Frozen fixtures and deterministic tests

| Case | Fixture / payload | Required outcome |
|---|---|---|
| Exact GRCh38 discovery and acceptance | summary_classification_exact_success_ca229507.json | EXACT_MATCH for NC_000012.12:g.102894804T>A. |
| Detailed-document version verification | detail_classification_exact_success_ca229507.json | data.uuid and data.metadata.version match summary uuid/docVersion. |
| Detailed-document version mismatch | matrix fixture | Reject through data.metadata.version, not guideline or retrieval metadata. |
| One strategy no-match | matrix fixture | Continue eligible discovery. |
| All eligible strategies no-match | no-match fixture plus matrix | Final no_match, never operational failure. |
| CPRA/assembly mismatch | matrix fixture | Reject; do not expose classification. |
| Deterministic padded-indel equivalent | matrix fixture | EXACT_MATCH_EQUIVALENT_REPRESENTATION, without left-shift inference. |
| Identity-incomplete input | matrix fixture | unattempted / identity_unverifiable; no provider call. |

Fixtures are deterministic test inputs, not current clinical evidence.

## 7. Bounded live verification evidence

On 2026-08-16, all requests completed within 30 seconds:

    200: hgvs=NC_000012.12:g.102894804T>A -> one record, CA229507
    200: hgvs=NM_000277.3:c.283A>T -> one record, CA229507
    200: cvId=102645 -> one record, CA229507
    404: hgvs=NC_000001.11:g.1A>G -> structured valid no-match
    200: detailed UUID c61fa227-893e-4be0-9e02-2d7c1494af20 -> data.metadata.version=1.0.0

Unit tests use fixtures only. Stage 2 must first pass deterministic tests, then
repeat one bounded exact record and expected no-match check after approval.

## 8. Stage 2 entry criteria

Stage 2 requires explicit review approval. Its deterministic tests must cover
every matrix case plus timeout, 403, 429, and UUID/version mismatch through
data.uuid and data.metadata.version. It must use existing bounded resilience,
source, readiness, persistence, conflict, and LLM-sanitization contracts.
