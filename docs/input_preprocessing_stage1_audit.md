# Selected-Variant Input Stabilization — Stage 1 Audit

**Scope:** diagnostic only.  No production behavior was changed.

**Audited revision:** `56000f3b459604704045db48216f9897de7d2b06` (`master`)

## Contract checked

The authoritative project documents define this application as an evidence-synthesis
workflow for one to ten user-selected variants.  It must preserve input order and
must not perform raw-VCF discovery, ranking, Top-N selection, pathogenicity-based
re-filtering, or a `FILTER == PASS` eligibility gate.  Exact assembly-qualified
allele identity is nevertheless required before identity-dependent evidence can be
trusted.

## Workbook inventory

Audited source, read without modification:

`C:\university\internship project\Phen_Candidates.30307400866_R.singleVCF.hg38.vcf.gz.vcf.xlsx`

| Order | Worksheet | Data rows | Columns | `REF=0` | `ALT=0` |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | `Noticable` | 5 | 129 | 0 | 2 |
| 2 | `Top_Phen` | 12 | 97 | 0 | 6 |
| 3 | `All_Phen_Related` | 74 | 97 | 11 | 19 |

The first worksheet contains three SNVs and two rows with numeric `ALT=0`.  The
second and third sheets include additional zero-allele rows, but current code does
not read them.  No multi-base substitution or explicitly anchored insertion/deletion
appears in worksheet 1.  The `Start`/`End` pairs for the two zero-ALT rows are
`102875767–102875770` and `102875773–102875773`.

This is **not a true VCF exported to XLSX**.  It is an ANNOVAR-like annotated
candidate workbook: it has `Chr`, `Start`, `End`, `Ref`, `Alt`, annotation columns
such as `Func.refGene`, `ExonicFunc.refGene`, `ACMG`, and `Clinvar`, plus quality
and phenotype-ranking columns.  It has neither the VCF header nor VCF `INFO`/
sample-column structure.  The `0` values are numeric cells, not empty strings.
Their exact insertion/deletion convention is suggested by the annotation labels,
but is not proven to be canonical VCF CPRA by this workbook alone.

## Current importer path

`frontend.execution.execute_analysis()` and
`prepare_analysis_recovery_request()` route `.xlsx` bytes directly to
`backend.excel_processing.parse_excel_variants()`.  That function opens only
`workbook.sheetnames[0]` (`Noticable` here), maps aliases, then calls the shared
`backend.vcf_processing.parse_manual_variants()` contract.  The resulting list is
passed into `backend.pipeline.validate_analysis_input()` and
`_process_filtered_variants()`.

Current aliases map `Chr -> chrom`, `Start -> pos`, `Ref -> ref`, `Alt -> alt`,
`Quality -> qual`, and `Filter -> filter`.  `End` has no alias and is ignored.
`Depth`, `AD`, and `GT_Quality` have no aliases and are ignored rather than used as
thresholds or preserved in the normalized input.  Unknown annotated columns are
ignored.  There is no user worksheet selector: only index 0 is considered.

## Per-row trace: worksheet 1

The following was produced by the actual Excel importer, shared manual parser, and
the pipeline's input-processing stage.  The local exact-identity probe is
`backend.variant_identity.normalize_variant_edit()` /
`normalized_genomic_hgvs()`; it does not make provider calls.

| Worksheet | Row | Gene | Source CHROM | Start/POS | End | REF | ALT | FILTER | Mapped representation | Current outcome | Exact reason / responsible function | Correct under preselected scope? | Recommended Stage-2 treatment |
| --- | ---: | --- | --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| `Noticable` | 2 | ERCC4 | 16 | 13947844 | 13947844 | C | T | PASS | `16:13947844:C>T` | accepted; identity probe succeeds | alias projection in `_manual_row_from_excel`; `normalize_variant_edit` gives CPRA and HGVS | YES | retain |
| `Noticable` | 3 | ERCC8 | 5 | 60944943 | 60944943 | C | T | PASS | `5:60944943:C>T` | accepted; identity probe succeeds | same path | YES | retain |
| `Noticable` | 4 | ERCC5 | 13 | 102875767 | 102875770 | GTGC | numeric 0 | QDfilter | `13:102875767:GTGC><DEL>` | accepted into preprocessing, but no local normalized edit/HGVS | `_excel_alternate` manufactures `<DEL>`; `_parse_manual_row` permits symbolic ALT; `normalize_variant_edit` rejects `<DEL>`; `End` is ignored | **NO** | require reference-aware proof before emitting canonical deletion CPRA, or hold as unresolved identity |
| `Noticable` | 5 | ERCC5 | 13 | 102875773 | 102875773 | C | numeric 0 | QDfilter | `13:102875773:C><DEL>` | accepted into preprocessing, but no local normalized edit/HGVS | same path; `End` is ignored | **NO** | same as row 4 |
| `Noticable` | 6 | LMNA | 1 | 156114969 | 156114969 | C | T | PASS | `1:156114969:C>T` | accepted; identity probe succeeds | same path | YES | retain |

The Stage 87 integrity ledger currently hashes the symbolic `<DEL>` text, so it
proves stability of that text across parser/pipeline stages, not reference-backed
allele correctness.  `backend.annotation._validate_variant()` checks only that REF
and ALT are non-empty strings; it therefore does not close this identity gap before
an identity-dependent annotation request.  No live provider was called in this
audit.

## Filtering, quality, and count behavior

`FILTER` is carried as metadata.  No importer or pre-analysis predicate compares it
with `PASS`; both `QDfilter` rows above are accepted.  `QUAL` is carried as a float.
There is no low-QUAL cutoff.  The shared manual parser rejects only non-numeric,
non-finite, or negative `QUAL` as malformed metadata.  `Depth`, `AD`, and
`GT_Quality` are neither eligibility gates nor retained normalized metadata by the
current Excel adapter.

An XLSX parser scans only worksheet 1, skips wholly blank recognized rows, and
rejects the entire input when it contains more than ten nonblank variant rows.  It
does not truncate, select the first ten, rank, or choose a Top-N subset.  VCF and
manual inputs enforce the same maximum after multiallelic splitting.

| Current rejection / decision path | Classification | Appropriate under scope? |
| --- | --- | --- |
| `FILTER` value such as `QDfilter` or `SORfilter` | Not an exclusion | YES |
| low numeric `QUAL`, `Depth`, `AD`, or `GT_Quality` | Not an exclusion | YES |
| non-numeric/non-finite/negative manual `QUAL` | STRUCTURAL_VALIDATION | YES; malformed metadata, not a low-quality cutoff |
| missing/invalid CHROM, POS, REF, ALT | STRUCTURAL_VALIDATION / PARSER-MAPPING | YES |
| more than ten selected rows/alleles | INPUT_LIMIT | YES |
| `ALT=0 -> <DEL>` accepted without reference-aware conversion | REPRESENTATION_NORMALIZATION and IDENTITY_VALIDATION gap | NO |
| `REF=0` on a processed sheet | STRUCTURAL_VALIDATION (`REF allele is invalid`) | YES until a source convention is proven reference-aware |

## Proven root causes and bounded history finding

The current workbook does **not** reproduce an exclusion caused by `FILTER`, `QUAL`,
depth, AD, GQ, worksheet iteration, aliases, or the ten-variant limit.  It accepts
all five rows from `Noticable`; the two `QDfilter` rows prove that source filtering
flags are not an eligibility gate.

The proven current defect is instead an unsafe representation bridge: numeric
`ALT=0` is converted to `<DEL>` without using `End` or a GRCh38 reference base.
The same code then permits the symbolic value through pre-analysis while the shared
exact-edit/HGVS normalizer cannot establish an allele identity.  This is an
identity/normalization defect, not biological absence or quality filtering.

Git history identifies commit `07f82ae` (`Fix professor XLSX recovery preparation`)
as the introduction of the `Chr`/`Start` aliases and `ALT=0 -> <DEL>` bridge, with
a synthetic regression test.  No current or reachable historical source message
proving the exact wording or count “four variants had problems” was found.  The
precise historical four-variant symptom therefore remains unproven; it must not be
attributed to `FILTER` or quality metadata on the evidence available here.

## Stage-2 recommendations (not implemented)

1. Replace the unconditional `ALT=0 -> <DEL>` conversion with an explicit,
   reference-aware convention handler that uses validated assembly, coordinates,
   `Start`/`End`, and reference sequence to prove CPRA; otherwise emit an
   identity-unresolved structural error without manufacturing an anchor.
2. Keep `FILTER`, QUAL, depth, AD, and GQ as quality provenance/warnings only;
   never make them a selected-variant eligibility rule.
3. Preserve source quality metadata in a bounded non-identity input-provenance
   structure if the approved Stage-2 contract requires it; do not use it to rank or
   re-filter variants.
4. Add fixtures for numeric/string zero REF/ALT, ambiguous `Start`/`End`, proven
   deletions/insertions, reference contradictions, and over-ten rejection.  A
   worksheet-selection product change is separate and must not silently begin
   iterating or choosing later sheets.
