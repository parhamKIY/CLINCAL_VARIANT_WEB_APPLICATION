# Professor Report Template Specification

**Stage:** 80 only  
**Status:** Implementation-ready design specification  
**Authoritative visual and structural reference:** `docs/TS-Final Report.pdf`  
**Reference inspected:** all four rendered pages, 2026-08-11  
**Reference SHA-256:** `41DFE4F49925CE86E42E881897EC8E7A8662DA71EE693101DAB681333390CACC`  
**Structured style companion:** `docs/report_style_spec.yaml`

## 1. Authority and scope

The professor PDF is the authoritative visual and structural reference for the
per-variant user-facing report. This specification is not loose inspiration. The
future output must belong to the same clinical Word-report family: a composed page,
prominent result block, central structured evidence table, integrated interpretation,
classification context, references, and restrained typography.

The default report must not resemble Streamlit cards, a generic form, Markdown,
JSON, a developer dashboard, or an unstyled DOCX.

Stage 80 defines presentation and content-mapping rules only. It does not define the
Stage 81 production schema, create the Stage 82 DOCX template, generate documents,
change the UI, alter interpretation behavior, or implement link resolution.

One accepted allele produces one independent report. Reports retain input order and
allele-level identity. Two variants in the same gene remain separate reports.

### Specification truth labels

- **Observed:** directly visible in, or geometrically/font-extracted from, the PDF.
- **Target approximation:** an implementation value selected where the PDF does not
  expose an exact Word setting. It must be visually checked against the PDF later.
- **Project adaptation:** a necessary change that preserves the visual structure while
  honoring the application's actual evidence, privacy, and decision-support scope.

## 2. Observed professor-report anatomy

### Page 1 - Result overview

Observed top-to-bottom order:

1. Centered italic/bold `NGS Result Report` title.
2. Two-column identity/referral metadata followed by a centered sample-type line and
   two-column sample/report metadata between thin horizontal rules.
3. `Patient's Clinical Features` as a bold inline label followed by narrative text.
4. A short inline `Method` statement.
5. `Conclusive Result(S)` heading.
6. A large centered result block with a dark red rectangular border:
   - gene plus coding/protein HGVS and zygosity in blue;
   - classification in large gold italic text with a footnote marker;
   - a small black italic/bold completion phrase.
7. `Brief Interpretation(s)` heading and a short summary paragraph.
8. Recommendations, notice text, analyst/signatory material, and bottom footnotes.

### Page 2 - Main finding and interpretation start

Observed top-to-bottom order:

1. `Main Finding(s) in Detail` heading.
2. Full-width, dual-band evidence table:
   - first header/value band: gene/transcript, variant, grouped population frequencies,
     associated disease/OMIM, and zygosity;
   - second header/value band: computational predictors, stable variant ID,
     classification, and inheritance;
   - white header cells, light-gray value cells, centered content, and gray grid lines.
3. Numbered legends immediately below the table plus a predictor footnote.
4. `Variant interpretation` heading.
5. Underlined allele display line.
6. Long, justified narrative with italics for gene symbols, selective bold emphasis,
   underlined phrases, and bracketed literature citations.

### Page 3 - Interpretation continuation and carrier material

Observed top-to-bottom order:

1. Narrative continuation without repeating the `Variant interpretation` heading.
2. `Carrier Status Results` narrative.
3. Two smaller carrier-result tables using the same white-header/gray-value style.
4. Short numbered legends below those tables.

The carrier section demonstrates continuation and repeated table styling. It is not
part of the automatic per-variant target because the application must not combine
unrelated variants or generate carrier counseling claims.

### Page 4 - Classification, method, comments, and references

Observed top-to-bottom order:

1. `Variant(s) classification` heading.
2. Classification-system explanation followed by a variant-specific classification
   sentence.
3. `Method` heading and dense technical paragraph.
4. `Comments` heading and bulleted limitations/recommendations.
5. `References` label followed by small, numbered bibliography entries.

### Material layout elements

- The report uses a stable overview -> detailed finding -> interpretation ->
  classification/method/comments -> references progression.
- The main evidence table and interpretation narrative are the dominant content.
- Table legends materially explain abbreviations and missingness.
- Long narrative continues naturally across a page boundary.
- No repeating page header, footer, page number, logo, or decorative background is
  visible.

## 3. Target per-variant report anatomy

The target preserves the professor hierarchy while removing content the application
cannot safely or truthfully generate.

### Section A - Result overview

1. **NGS Result Report** title.
2. **Privacy-safe report metadata strip** using only available non-PHI values:
   report identifier, input index, genome assembly, report state, and generation date.
   Omit unavailable items; never leave patient/sample placeholders.
3. **Clinical Features** containing accepted HPO identifiers/labels and an optional
   bounded de-identified summary already approved for reporting. If phenotype evidence
   is unavailable, state that explicitly.
4. **Method / scope line** describing the actual filtered-variant evidence collection
   and interpretation workflow. Do not claim that the application performed WES or a
   laboratory assay.
5. **Conclusive Result(s)** heading and prominent bordered result block containing:
   gene, coding HGVS, protein HGVS when available, zygosity when supported, and a
   source-attributed classification context.
6. **Brief Interpretation(s)** with a concise evidence-grounded summary.
7. **Decision-support notice** in restrained small text.

### Section B - Detailed finding

Begin on a new page unless a later visual gate proves that an overview plus table can
fit without reducing the professor-report density or hierarchy.

1. **Main Finding(s) in Detail** heading.
2. **Professor-style main finding table** specified in Section 6.
3. **Table legends** generated only for abbreviations/statuses present in the table.
4. **Variant interpretation** heading.
5. Underlined allele display line.
6. Long-form evidence-grounded interpretation specified in Section 7.

### Section C - Classification, methods, provenance, and citations

1. **Variant(s) classification** with source-attributed classification summary.
2. **Method** describing only the application's actual processing and evidence limits.
3. **Comments / scope** containing missingness, conflicts, degraded-source notices,
   limitations, and the non-diagnostic disclaimer.
4. **References** containing literature only.
5. **Data Sources** containing database/tool provenance separately.

Section C may follow the narrative on the same page when space permits. If it would
leave an orphan heading or cramped block, start it on a new page.

## 4. Content excluded from automatic generation

The following professor-report content is structurally informative but out of scope:

- name, national ID, medical record ID, referral identity, date of birth, gender,
  contact details, sample identifiers, and other direct identifiers;
- sample type, sample quality, sampling date, or laboratory reporting metadata not
  actually collected by this application;
- definitive diagnosis or statements that the subject suffers from a disease;
- treatment, clinical management, confirmatory-test, Sanger, co-segregation,
  prenatal-diagnosis, PGD, reproductive, or genetic-counseling recommendations;
- analyst names, signatures, professional titles, laboratory sign-off, and claims of
  regulatory or clinical-laboratory authorization;
- generic carrier-status education or aggregation of additional carrier variants;
- claims that WES/NGS was performed by this software;
- independent ACMG/AMP classification or application of CSpec rules when the project
  has not implemented that adjudication.

When an excluded block has no safe target equivalent, remove the block and close the
layout gap. Do not render an empty demographic form or placeholder signature area.

## 5. Visual and layout specification

Exact structured tokens and their evidence basis are in
`docs/report_style_spec.yaml`.

### Page and margins

- **Observed:** US Letter portrait, `612 x 792 pt` (`8.5 x 11 in`).
- **Observed:** main content begins at approximately `72 pt` and ends near `540 pt`,
  corresponding to about one-inch left/right margins.
- **Target approximation:** 1-inch margins on all sides. A later DOCX fidelity gate
  may adjust top/bottom margins by no more than 0.15 inch without changing the content
  width or visual family.

### Typography

- **Observed:** Times New Roman is the dominant embedded family.
- **Observed:** title is 18 pt bold italic; major headings are predominantly 12 pt
  bold; narrative is 11-12 pt; table text is 9-10 pt; references are 7 pt.
- **Target approximation:** use Times New Roman throughout, with Arial only as a
  fallback for missing glyphs. Do not silently substitute a modern sans-serif theme.
- **Observed:** gene symbols and parts of the allele display use italics; important
  result values and selected narrative phrases use bold; the allele display before the
  long interpretation is underlined.

### Paragraph behavior

- **Observed:** long narrative is justified; headings and short labels are left
  aligned; the result block and table contents are centered.
- **Target approximation:** narrative line spacing 1.05-1.10, 0 pt before/after body
  paragraphs, and 6-10 pt separation around headings. Avoid large web-style gaps.
- Keep headings with the following paragraph/table. Do not leave a heading at the
  bottom of a page.

### Color and emphasis

- **Observed/extracted:** result border approximately `#C00000`.
- **Observed/extracted:** allele/zygosity line approximately `#2F5496`.
- **Observed/extracted:** classification emphasis approximately `#FFC000`.
- **Observed/extracted:** table value rows approximately `#D9D9D9`, with borders near
  `#A5A5A5`.
- Use black for all normal headings and narrative. Color never replaces a textual
  label, source attribution, or missingness state.
- The gold classification treatment is reserved for a supported classification
  context. If classification is unavailable or conflicting, retain the same central
  placement but use black/dark gray text and explicit wording; do not imply certainty
  through color.

### Result block

- **Observed:** approximately `443 x 65 pt`, centered, with a roughly 2 pt dark red
  rectangular border and no fill.
- **Target approximation:** minimum internal padding 6 pt vertical and 10 pt
  horizontal; allow height to expand for wrapped HGVS text.
- Never shrink HGVS text below 10 pt. Prefer wrapping at semantic boundaries between
  gene, coding HGVS, protein HGVS, and zygosity.

### Headers and footers

- **Observed:** no repeating header/footer or page number.
- Target V1 should preserve that absence. Artifact metadata belongs in the page-one
  metadata strip, not a developer footer.

## 6. Main Finding(s) table specification

The table is the central visual object after the result block. It is a full-width,
dual-band table with white header cells and light-gray value cells.

### Geometry and hierarchy

- **Observed:** page-two table spans approximately `477.6 pt` (`6.63 in`) from
  `x=72.0` to `x=549.6`.
- **Observed:** first-band column proportions are approximately 15.9%, 17.0%, 30.2%
  for the grouped population columns, 23.6%, and 13.2%.
- **Observed:** the population group divides into approximately 11.3%, 10.4%, and 8.5%
  of total table width.
- **Observed:** second-band proportions are approximately 15.9%, 17.0%, 16.0%, 14.1%,
  23.6%, and 13.2%.
- **Target approximation:** preserve those proportions within 1.5 percentage points.
  Adjust only to prevent clipping in complex indels or long disease names.
- Prevent row splitting when practical. Repeat the relevant header band if a value row
  must continue onto another page.

### First header/value band

| Display field | Current evidence meaning | Format | Missing-value behavior | Conditionality and provenance |
|---|---|---|---|---|
| Gene & Transcript | Exact gene symbol and selected transcript from normalized annotation | Gene italic; transcript on next line; centered | `Not available` | Never infer from phenotype or another same-gene allele; retain annotation source in Data Sources |
| Variant | Coding HGVS and protein HGVS; retain genomic identity elsewhere | Coding HGVS first; protein HGVS second; wrap at `/` or line break | Show available HGVS only; if neither exists, show `GRCh37/38 chr:pos REF>ALT` | Must resolve to this report's exact allele |
| Frequency | Exact-allele population evidence grouped by named dataset/source | Up to three source-labelled subcolumns; scientific notation for values below 0.001; otherwise up to six significant digits | `No exact record`, `Not available`, or `Not supported by this source` | Labels must name actual sources such as gnomAD, ExAC, or 1000G; fallback data keeps fallback provider identity |
| Associated Disease & ID | Evidence-backed disease name plus MONDO/OMIM/other validated identifier | Name, then identifier in parentheses; wrap naturally | `No validated association found` or `Not available` | Distinguish gene-disease context from variant-level clinical assertion; never select a disease from phenotype alone |
| Zygosity | Explicit supported zygosity field | Controlled text such as `Heterozygous`, `Homozygous`, or `Hemizygous` | `Not available` | Never derive from presence of one ALT allele or from stripped genotype data |

Population subcolumns are evidence-driven, not decorative. Preserve the observed
grouped-header geometry, but do not fabricate ExAC/1000G values merely because the
professor table contains those labels.

### Second header/value band

| Display field | Current evidence meaning | Format | Missing-value behavior | Conditionality and provenance |
|---|---|---|---|---|
| Computational evidence slots | Available CADD, SIFT, PolyPhen, SpliceAI, or GeneBe-reported predictors | Up to three clearly labelled source/tool cells; score plus controlled prediction when available | `Not available` per cell | Show only retained validated predictor output; do not convert predictions into ACMG criteria |
| Stable variant ID | rsID or other stable exact-allele identifier | Preserve canonical prefix/case; one ID per line | `Not available` | Identifier must have passed exact assembly/allele mapping |
| Classification context | Source-attributed ClinVar, GeneBe automated, or reviewer-confirmed wording | Short classification plus source marker; conflicts use `Conflicting` | `Not available` | Never merge independent/derived sources into a false consensus; CSpec remains context only |
| Inheritance | Evidence-backed mode of inheritance for the displayed disease context | Controlled abbreviation plus generated legend | `Not available` | Must be tied to a validated gene-disease context; never inferred from zygosity |

### Cell and border behavior

- Header cells: white fill, centered vertically/horizontally, regular 9-10 pt text;
  use bold only for group labels where the PDF does.
- Value cells: `#D9D9D9` fill, centered, 9-10 pt, bold for key result values.
- Borders: continuous gray grid, target 0.75-1.0 pt; outer and main band separators
  may be up to 1.25 pt.
- Cell padding: target 3-5 pt horizontal and 2-4 pt vertical.
- Use exact Unicode HGVS characters only when Word font support is verified; otherwise
  use safe ASCII equivalents without changing the allele.
- Do not clip text, reduce below 8.5 pt, or overlap footnote markers.

### Table legends

- Generate legends only for abbreviations actually shown: zygosity, inheritance,
  classification abbreviations, `NA`, and named predictor score interpretation.
- `NA` means `Not available`, never absent evidence of pathogenicity.
- Predictor legends must state what the score represents and must not imply an ACMG
  classification.
- Legends use numbered 8.5-9 pt text directly beneath the table and remain with the
  table when pagination permits.

## 7. Human-readable content mapping

This section maps visible report fields to existing project semantics. It is not a new
schema.

| Visible report content | Existing semantic source | Rendering rule |
|---|---|---|
| Report identity and order | Draft report ID, analysis ID, variant/input index | Show bounded non-PHI identifiers; input index is one-based for display |
| Assembly and genomic allele | Evidence variant plus assembly | Always retain as the stable identity fallback even when HGVS is unavailable |
| Gene/transcript/HGVS/consequence | Current variant summary / normalized context | Display only validated values attached to the exact allele |
| Clinical Features | Accepted HPO terms, matched HPO terms, phenotype status/score, disease context | Prefer HPO ID plus local ontology label; do not expose raw phenotype-extraction text |
| Zygosity | Explicit supported input/evidence only | `Not available` until such evidence exists; never infer from genotype-stripped input |
| Population | Direct exact-allele population result or source-preserving fallback | Show source/dataset with value; preserve `no_match`, unsupported, and unavailable distinctions |
| Disease and inheritance | ClinGen/GenCC and MyDisease context with exact identifiers | State relationship scope; do not turn local HPO disease examples into gene-disease evidence |
| Computational evidence | Validated VEP/GeneBe/MyVariant predictor fields | Name the tool/provider and preserve missingness |
| ClinVar | Exact direct ClinVar or explicitly derived MyVariant fallback | Show classification, review status, conditions, and accession with actual source identity |
| CSpec | Released live or exact last-known-good metadata | Show as context/provenance only; never claim rule execution |
| Conflict | Deterministic conflict audit and interpretation conflict assessment | Put short state in classification/comments and explanation in narrative |
| Interpretation | Original/current reviewer interpretation plus model/failure status | Use reviewer-approved text when present; never regenerate during rendering |
| Literature | Retained PMID, PMCID, or DOI-backed articles | Numbered References only |
| Provider provenance | Capability/provider status, method, fallback, retrieval metadata | Data Sources section; concise main text with optional detailed provenance |
| Limitations/warnings | Report limitations, interpretation warnings, capability missingness | Convert to consequence-oriented comments without exposing raw exceptions |

### Missingness contract

A report remains structurally valid when optional evidence is missing. Use the most
specific supported state:

- `No exact record` for a successful exact query with no match;
- `Not supported by this source` for an unsupported allele/capability;
- `Not available` for unavailable or absent optional evidence;
- `Available via <actual fallback provider>` for successful degraded evidence;
- `Interpretation requires attention` only after core interpretation recovery is
  exhausted in later stages.

Missingness never deletes the variant, collapses two reports, supplies a fabricated
value, or becomes negative clinical evidence.

## 8. Narrative-section specification

### Brief Interpretation(s)

- One to three concise sentences, normally 40-100 words.
- Identify the exact allele and summarize the highest-confidence evidence state.
- State phenotype concordance as `supported`, `partially supported`,
  `no supported association`, `unavailable`, or `not assessed`.
- Mention a material conflict or core interpretation failure.
- Do not diagnose, recommend treatment/testing, or introduce external facts.

### Variant interpretation

The narrative is coherent prose, not an evidence dump. Use this adaptable order:

1. gene function/context only when supported by retained evidence;
2. validated gene-disease relationship and inheritance context;
3. exact variant identity, consequence, and transcript context;
4. exact-allele population evidence or explicit missingness;
5. ClinVar and other source-attributed classification evidence;
6. computational evidence with tool attribution and restrained meaning;
7. literature evidence cited only by retained reference IDs;
8. phenotype concordance or explicit non-concordance;
9. conflict status and unresolved disagreements;
10. evidence-limited conclusion and decision-support limitation.

Sections without evidence may be omitted or represented by one explicit missingness
sentence. They must not be filled with general model knowledge.

Required phenotype behavior:

- **Strong concordance:** describe the supported overlap without claiming causality.
- **Partial concordance:** identify the supported subset and preserve uncertainty.
- **No supported association:** state that no supported phenotype association was
  identified, then continue interpreting from non-phenotype evidence.
- **Unavailable:** state that phenotype evidence could not be assessed and continue.
- **Conflicting evidence:** name the sources and disagreement without forcing a
  resolution.
- **Sparse evidence:** produce a shorter evidence-limited narrative; never fail solely
  because evidence or literature is sparse.

Phenotype mismatch must never make the narrative or report structurally impossible.

### Variant(s) classification

Retain the professor heading, but replace autonomous classification prose with a
source-attributed summary:

1. reviewer-confirmed interpretation, if present;
2. direct ClinVar classification and review status;
3. GeneBe or other automated annotation classification and reported criteria;
4. CSpec availability as context only;
5. deterministic conflict status;
6. explicit statement that the application has not independently adjudicated an
   ACMG/AMP classification unless a future scope change implements that process.

Do not flatten conflicting classifications into one colored label. The conclusive
block may show `Conflicting classifications` and direct the reader to this section.

## 9. Literature References and Data Sources

### References

Reserve the numbered bibliography for publications with validated identifiers:

- PubMed (`PMID`);
- PubMed Central (`PMCID`);
- DOI-backed publications.

Format each entry as a compact hanging-indent paragraph. Use available authors, year,
title, journal, volume/pages, then the validated identifier/link. If metadata is
partial, show only verified fields. Citation numbers follow first appearance in the
narrative and remain stable throughout the report.

### Data Sources

Render database/tool provenance separately, preferably as a compact table with:

```text
Source | Capability | Status | Record/dataset | Retrieval method | Link status
```

Applicable sources include Ensembl VEP or VariantValidator, MyVariant.info or
Ensembl Variation, NCBI ClinVar or its explicitly derived fallback, GeneBe,
ClinGen/GenCC, ClinGen CSpec or its local last-known-good cache, Phen2Gene or local
HPO-gene fallback, MyDisease or local HPO disease context, gnomAD or Ensembl
population fallback, LitVar2/Europe PMC/PubMed, and local HPO resources.

Rules:

- name the actual provider that supplied the displayed evidence;
- preserve primary/fallback role and normalized missingness;
- use a human-readable stable record link when later link-resolution work validates
  one;
- never present a raw API JSON endpoint as an ordinary reference;
- label an optional raw endpoint explicitly as technical source data, if retained at
  all;
- when no validated human page exists, show provenance without a clickable URL;
- do not number Data Sources as literature citations.

## 10. Pagination and document behavior

- Page count is content-driven; the four-page reference is not a fixed requirement.
- Section A is composed as a deliberate overview page.
- Section B starts on a new page by default.
- Long interpretation flows across pages without repeating the section heading.
- Keep the main table together when it fits; otherwise repeat headers and prevent
  value-row splits.
- Keep headings with at least two following body lines.
- Keep each reference entry together when possible.
- Avoid blank trailing pages, clipped HGVS, orphaned legends, and large unexplained
  whitespace.
- DOCX must remain editable in later stages; layout rules must use Word-native styles,
  tables, paragraphs, and page breaks rather than a page-sized screenshot.

## 11. Stage 80 acceptance checklist

Stage 80 passes only if all items are true:

- `docs/TS-Final Report.pdf` is explicitly authoritative and all four pages were
  visually inspected.
- Direct observations are distinguished from target approximations.
- One independent report per accepted allele is mandatory and input order is retained.
- The report hierarchy closely matches the professor report family.
- The conclusive-result block and Main Finding(s) table are dominant visual elements.
- The table specifies fields, evidence meaning, formatting, missingness, optionality,
  and provenance.
- Brief Interpretation, Variant Interpretation, and Classification are embedded report
  sections with evidence-grounded behavior for all phenotype/conflict/sparsity states.
- Literature References and Data Sources are separate.
- Missing evidence, unsupported alleles, no-match, and fallback evidence remain
  explicit and source-preserving.
- Patient identifiers, diagnosis, treatment, counseling, prenatal/PGD, signatures,
  carrier aggregation, and unsupported lab claims are excluded.
- The target is a clinical-report-style editable Word artifact, not web controls or a
  generic export.
- No Stage 81 schema, Stage 82 template, rendering code, preview, editing, UI,
  interpretation-recovery, or link-resolver implementation is included.

## 12. Handoff to later stages

- Stage 81 may design a production contract that can express every field and state in
  this specification; it must not treat this document or YAML as that schema.
- Stage 82 must build the editable Word template from this specification and the PDF,
  preserving observed geometry and validating target approximations visually.
- Stage 83 must render synthetic fixtures and reject generic or unstyled output.
- Later reference, interpretation, and UI stages implement behaviors described here;
  Stage 80 only fixes their presentation requirements.
