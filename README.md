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
