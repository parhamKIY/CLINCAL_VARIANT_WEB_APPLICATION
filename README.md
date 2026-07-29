# Clinical Variant Interpretation

Clinical variant interpretation MVP built with Python and Streamlit.

## Current implementation status

- Central configuration is loaded from `.env` through `config.py`.
- `.vcf` and `.vcf.gz` files are validated and streamed.
- Multi-allelic records are split into one object per ALT allele.
- Genotype is extracted when a sample column exists.
- Manual `CHROM:POS:REF:ALT` input is supported.
- A `bcftools norm` integration point exists for reference-aware
  normalization.
- Normalization is opt-in for the Windows MVP and requires an explicitly
  matching reference FASTA.
- The public VCF processing entry point returns an iterator so large files
  can flow into prioritization without being fully loaded into memory.
- MVP prioritization uses bounded-memory uniform random selection.
- Candidate count defaults to `TOP_VARIANTS` and can be overridden per call.
- Annotation phase one connects candidates to Ensembl VEP in bounded batches.
- VEP output is cleaned into a stable evidence structure before later stages.
- VCF processing tests are stored in `tests/test_pipeline.py`.

The two current files under `data/samples/` are Ensembl reference VCFs.
They do not contain `FORMAT` or patient sample columns, so their genotype
output is correctly reported as `None`.

## Environment

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Tests

Run the offline test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Run the retained manual LLM smoke test:

```powershell
.\.venv\Scripts\python.exe tests\manual_llm_smoke.py
```

## Windows VCF parser decision

`cyvcf2` has no compatible native Windows wheel for the current Python
3.13 environment and its Windows support is experimental. The MVP uses
`vcfpy`, a tested parser with native Python 3.13 support. The public
functions remain isolated in `backend/vcf_processing.py`, allowing the
parser implementation to be replaced later without changing the
pipeline interface.

## Normalization limitation

Reference-aware normalization is optional during MVP parsing but must be
enabled before clinical annotation whenever reference-aware left alignment
or representation normalization is required. It needs:

- a matching reference genome FASTA;
- `bcftools` available on `PATH`.

`bcftools` is not installed in the current Windows environment, so the
normalization command is implemented and error-tested but has not yet
been executed against a reference FASTA.

## Temporary prioritization strategy

`backend/prioritization.py` currently uses reservoir sampling. This keeps
memory usage bounded while giving each parsed variant an equal chance of
selection.

Random selection is only an MVP placeholder. It is not a clinically valid
ranking algorithm. The pipeline calls only `prioritize_variants()`, so the
internal strategy can later be replaced with configurable frequency,
functional-impact, phenotype, gene-disease, and inheritance scoring without
changing the pipeline interface.

## Annotation phase one

`backend/annotation.py` currently integrates Ensembl VEP:

- explicit GRCh37/GRCh38 assembly configuration;
- POST batching with Ensembl's 200-variant maximum;
- request timeout and limited retries for transient failures;
- structured per-variant error output without stopping the whole pipeline;
- cleaned transcript, gene, consequence, impact, HGVS protein change, and
  source-reference fields;
- no raw API response is passed to later pipeline stages.

MyVariant, direct ClinVar evidence, and ClinGen evidence remain later
annotation phases. They can be added under the existing `sources` field
without changing the public `annotate_variants()` interface.

Run live Ensembl VEP, MyVariant.info, and ClinGen connectivity checks
separately from the offline test suite:

```powershell
.\.venv\Scripts\python.exe tests\manual_annotation_smoke.py
```

An optional manual variant can be supplied in `CHROM:POS:REF:ALT` format.
