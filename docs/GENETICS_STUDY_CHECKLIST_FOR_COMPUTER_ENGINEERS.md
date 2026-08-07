# Genetics Study Checklist for a Computer Engineer

**Project:** `clinical_variant_app`  
**Audience:** Computer-engineering student with no formal genetics background  
**Purpose:** Learn enough genetics and clinical-variant interpretation to design,
implement, test, and explain this project responsibly  
**Scope:** Germline Mendelian SNVs and small indels  

---

## 1. How to use this checklist

You do not need to become a physician or clinical geneticist. You do need
enough domain knowledge to:

- understand what every input field represents;
- recognize biologically invalid assumptions;
- model evidence without losing its context;
- distinguish annotation, pathogenicity, and patient relevance;
- implement ACMG/AMP rules faithfully;
- design the gene–phenotype graph;
- understand why evidence can conflict;
- prevent the LLM from producing unsupported conclusions;
- communicate limitations honestly.

Use three study levels:

- **Core:** Required before implementing the related roadmap stage.
- **Working knowledge:** Required before validating or explaining the feature.
- **Later:** Important for future expansion but outside the initial project
  scope.

For each module:

1. Study the listed topics.
2. Look up every unfamiliar term in the
   [NHGRI Genetics Glossary](https://www.genome.gov/genetics-glossary).
3. Complete the project exercise.
4. Answer the exit questions without copying a definition.
5. Mark the module complete only when you can explain it in your own words.

This checklist is educational preparation, not clinical certification. Final
clinical rules and interpretations still require authoritative guidance and
expert review.

---

## 2. Recommended learning order

```text
Module 1  Biology foundations
    ↓
Module 2  Genes, transcripts, and proteins
    ↓
Module 3  Genetic variation
    ↓
Module 4  Mendelian inheritance and patient genotype
    ↓
Module 5  Genome coordinates, HGVS, and VCF
    ↓
Module 6  Sequencing and variant quality
    ↓
Module 7  Variant annotation and consequence
    ↓
Module 8  Population genetics and frequency evidence
    ↓
Module 9  Gene–disease validity and disease mechanisms
    ↓
Module 10 Clinical databases and evidence quality
    ↓
Module 11 ACMG/AMP interpretation
    ↓
Module 12 Phenotypes, HPO, and the gene–phenotype graph
    ↓
Module 13 Scientific literature and functional evidence
    ↓
Module 14 Conflict, uncertainty, and clinical reporting
    ↓
Module 15 Ethics, privacy, and responsible software
```

---

## 3. Module 1 — Biology foundations

**Priority:** Core  
**Suggested study time:** 4–6 hours

### Topics

- [ ] Cell structure: nucleus, cytoplasm, mitochondria
- [ ] DNA, RNA, and protein
- [ ] Nucleotide bases: `A`, `C`, `G`, `T`, and `U`
- [ ] DNA double helix and complementary base pairing
- [ ] Chromosomes and chromosome pairs
- [ ] Autosomes versus sex chromosomes
- [ ] Human nuclear genome versus mitochondrial genome
- [ ] Gene, genome, locus, and allele
- [ ] Genotype versus phenotype
- [ ] Central dogma: DNA → RNA → protein
- [ ] DNA replication
- [ ] Transcription
- [ ] RNA processing and splicing
- [ ] Translation and the genetic code
- [ ] Codons and amino acids
- [ ] Start and stop codons
- [ ] Regulatory and noncoding DNA

### Suggested searches

```text
MedlinePlus Genetics cells and DNA
NHGRI DNA gene chromosome glossary
DNA transcription translation beginner
coding DNA versus noncoding DNA
```

### Primary beginner resources

- [MedlinePlus: Cells and DNA](https://medlineplus.gov/genetics/understanding/basics/)
- [NHGRI: About Genomics](https://www.genome.gov/about-genomics)
- [NHGRI genomics fact sheets](https://www.genome.gov/about-genomics/fact-sheets)

### Project exercise

- [ ] Draw this relationship and explain every arrow:

```text
Chromosome → Gene → Transcript → Exon/CDS → Codon → Amino acid → Protein
```

- [ ] Explain why a DNA change may alter a protein.
- [ ] Explain why some DNA changes do not alter a protein.

### Exit questions

- [ ] What is the difference between a gene and a chromosome?
- [ ] What is the difference between DNA and RNA?
- [ ] Why can one DNA base change affect a protein?
- [ ] Why are many genomic variants biologically harmless?
- [ ] What is the difference between genotype and phenotype?

---

## 4. Module 2 — Genes, transcripts, and proteins

**Priority:** Core  
**Suggested study time:** 5–7 hours

### Topics

- [ ] Gene structure
- [ ] Promoter and regulatory regions
- [ ] Exons and introns
- [ ] Untranslated regions: `5' UTR` and `3' UTR`
- [ ] Coding sequence: `CDS`
- [ ] Pre-mRNA and mature mRNA
- [ ] Alternative splicing
- [ ] Transcript isoforms
- [ ] Reference transcripts
- [ ] `MANE Select` and `MANE Plus Clinical`
- [ ] Canonical transcript versus clinically relevant transcript
- [ ] Transcript version numbers
- [ ] Protein domains
- [ ] Active sites and functional motifs
- [ ] Loss of function
- [ ] Gain of function
- [ ] Dominant-negative mechanism
- [ ] Haploinsufficiency
- [ ] Nonsense-mediated mRNA decay: `NMD`
- [ ] Tissue-specific expression

### Suggested searches

```text
gene exon intron transcript isoform explained
MANE Select transcript documentation
loss of function gain of function dominant negative
nonsense mediated decay PVS1
```

### Project exercise

- [ ] Select one gene from the project's example data.
- [ ] Find at least two transcripts for that gene.
- [ ] Compare the exon structure and protein consequence for the same genomic
  position.
- [ ] Explain why transcript choice can change variant interpretation.

### Exit questions

- [ ] Why can one gene have multiple transcripts?
- [ ] Why is a gene symbol insufficient for describing a variant?
- [ ] What does the transcript version number protect against?
- [ ] What is a loss-of-function disease mechanism?
- [ ] Why does a stop-gained consequence not automatically prove `PVS1`?

---

## 5. Module 3 — Genetic variation

**Priority:** Core  
**Suggested study time:** 5–7 hours

### Topics

- [ ] Reference sequence and alternate sequence
- [ ] Variant versus mutation terminology
- [ ] Germline versus somatic variants
- [ ] Inherited versus de novo variants
- [ ] Single-nucleotide variant: `SNV`
- [ ] Multi-nucleotide variant: `MNV`
- [ ] Insertion
- [ ] Deletion
- [ ] Indel
- [ ] Duplication
- [ ] Inversion
- [ ] Copy-number variant: `CNV`
- [ ] Structural variant: `SV`
- [ ] Repeat expansion
- [ ] Mitochondrial variant
- [ ] Mosaicism
- [ ] Coding versus noncoding variants
- [ ] Missense variant
- [ ] Synonymous variant
- [ ] Nonsense or stop-gained variant
- [ ] Frameshift variant
- [ ] In-frame insertion/deletion
- [ ] Splice donor and splice acceptor variants
- [ ] Splice-region variants
- [ ] Start-loss and stop-loss variants
- [ ] Upstream, downstream, intronic, and intergenic variants
- [ ] Penetrance
- [ ] Expressivity
- [ ] Pleiotropy
- [ ] Allelic heterogeneity
- [ ] Locus heterogeneity

### Suggested searches

```text
types of human genetic variants SNV indel CNV
missense synonymous nonsense frameshift
penetrance versus expressivity genetics
allelic heterogeneity versus locus heterogeneity
germline versus somatic variant
```

### Project exercise

- [ ] Create a table with one example of each supported first-release variant
  type.
- [ ] For each example, write the possible effect on RNA and protein.
- [ ] Mark which variant types are outside the current roadmap scope.

### Exit questions

- [ ] What is the difference between an SNV and an indel?
- [ ] Why can a synonymous variant still affect disease?
- [ ] What is the difference between germline and somatic variation?
- [ ] What is penetrance?
- [ ] Why can two variants in the same gene cause different diseases?

---

## 6. Module 4 — Mendelian inheritance and patient genotype

**Priority:** Core  
**Suggested study time:** 7–10 hours

### Topics

- [ ] Diploid and haploid genomes
- [ ] Homozygous, heterozygous, and hemizygous
- [ ] Wild type and reference allele
- [ ] Carrier
- [ ] Autosomal dominant inheritance
- [ ] Autosomal recessive inheritance
- [ ] X-linked inheritance
- [ ] Y-linked inheritance
- [ ] Mitochondrial inheritance
- [ ] De novo occurrence
- [ ] Compound heterozygosity
- [ ] Cis versus trans phase
- [ ] Phased versus unphased genotype
- [ ] Pedigrees
- [ ] Proband
- [ ] Affected and unaffected relatives
- [ ] Segregation and co-segregation
- [ ] Informative meioses
- [ ] Reduced penetrance
- [ ] Variable expressivity
- [ ] Phenocopy
- [ ] Consanguinity
- [ ] Recessive carrier frequency
- [ ] Genomic imprinting: working knowledge
- [ ] Uniparental disomy: working knowledge

### Suggested searches

```text
MedlinePlus inheritance patterns
heterozygous homozygous hemizygous genetics
compound heterozygous cis trans phase
pedigree segregation genetics
de novo variant confirmed parentage
```

### Primary resource

- [MedlinePlus inheritance patterns](https://medlineplus.gov/genetics/understanding/inheritance/inheritancepatterns/)

### Project exercise

- [ ] Draw one autosomal-dominant pedigree.
- [ ] Draw one autosomal-recessive pedigree.
- [ ] Explain how two heterozygous variants in one recessive gene differ when
  they are in `cis` versus `trans`.
- [ ] Explain which ACMG/AMP evidence cannot be evaluated if genotype and family
  data are absent.

### Exit questions

- [ ] What does genotype `0/1` usually mean in a diploid VCF?
- [ ] What does genotype `1/1` usually mean?
- [ ] Why does phase matter in recessive disease?
- [ ] What is a confirmed de novo variant?
- [ ] Why does inheritance mode affect patient priority?

---

## 7. Module 5 — Genome coordinates, reference assemblies, HGVS, and VCF

**Priority:** Core and project-critical  
**Suggested study time:** 10–14 hours

### Topics: genome coordinates

- [ ] Reference genome
- [ ] `GRCh37`/`hg19`
- [ ] `GRCh38`/`hg38`
- [ ] Primary chromosomes `1–22`, `X`, `Y`, and `MT`
- [ ] Contigs and alternate loci
- [ ] One-based versus zero-based coordinates
- [ ] Strand orientation
- [ ] Reference allele validation
- [ ] Liftover and its limitations
- [ ] Why the same biological variant has different positions across assemblies

### Topics: variant normalization

- [ ] Allele-specific representation
- [ ] Splitting multiallelic records
- [ ] Trimming shared prefix and suffix
- [ ] Left alignment
- [ ] Parsimonious representation
- [ ] Duplicate variant representations
- [ ] Exact chromosome/position/reference/alternate matching

### Topics: HGVS

- [ ] Reference-sequence accession and version
- [ ] Genomic notation: `g.`
- [ ] Coding DNA notation: `c.`
- [ ] Noncoding transcript notation: `n.`
- [ ] RNA notation: `r.`
- [ ] Protein notation: `p.`
- [ ] Substitution notation
- [ ] Deletion, insertion, duplication, and deletion-insertion notation
- [ ] Predicted protein consequences in parentheses
- [ ] Why HGVS depends on the transcript

### Topics: VCF

- [ ] VCF metadata lines
- [ ] Header line
- [ ] `CHROM`
- [ ] `POS`
- [ ] `ID`
- [ ] `REF`
- [ ] `ALT`
- [ ] `QUAL`
- [ ] `FILTER`
- [ ] `INFO`
- [ ] `FORMAT`
- [ ] Sample columns
- [ ] `GT`
- [ ] Allele depth: `AD`
- [ ] Read depth: `DP`
- [ ] Genotype quality: `GQ`
- [ ] Phased `|` versus unphased `/`
- [ ] Missing values
- [ ] Multiallelic genotypes
- [ ] VCF is a representation of calls, not a clinical interpretation

### Suggested searches

```text
GRCh37 versus GRCh38 coordinates
variant normalization left align trim alleles
HGVS nomenclature beginner
VCF specification CHROM POS REF ALT QUAL FILTER
VCF genotype GT AD DP GQ
```

### Primary resources

- [Current HTS specifications repository](https://github.com/samtools/hts-specs)
- [HGVS Nomenclature](https://hgvs-nomenclature.org/)
- [HGVS checklist](https://hgvs-nomenclature.org/stable/recommendations/checklist/)

### Project exercise

- [ ] Explain every field in the application's manual input table.
- [ ] Explain why `POS` boundaries depend on `CHROM` and assembly.
- [ ] Take one multiallelic VCF row and split it into allele-specific records.
- [ ] Write the normalized project key:

```text
ASSEMBLY:CHROM:POS:REF:ALT
```

- [ ] Find the corresponding genomic, coding, and protein HGVS descriptions.

### Exit questions

- [ ] Why is `1:941284:G:A` incomplete without a genome assembly?
- [ ] Why must `REF` be checked against the selected assembly?
- [ ] What is the difference between VCF and HGVS representation?
- [ ] Why can one variant have multiple `HGVSc` values?
- [ ] What information is lost if sample and `FORMAT` columns are ignored?

---

## 8. Module 6 — Sequencing and variant quality

**Priority:** Working knowledge  
**Suggested study time:** 7–10 hours

### Topics

- [ ] Sanger sequencing
- [ ] Next-generation sequencing: `NGS`
- [ ] Whole-exome sequencing: `WES`
- [ ] Whole-genome sequencing: `WGS`
- [ ] Targeted gene panels
- [ ] Reads and read length
- [ ] Alignment and mapping
- [ ] Reference bias
- [ ] Duplicate reads
- [ ] Coverage and depth
- [ ] Base quality
- [ ] Mapping quality
- [ ] Variant calling
- [ ] Genotype calling
- [ ] False positive and false negative calls
- [ ] Allele balance
- [ ] Strand bias
- [ ] Low-complexity regions
- [ ] Sequence confirmation
- [ ] Phred score concept
- [ ] Difference between `QUAL`, `GQ`, and depth
- [ ] Why `PASS` does not mean pathogenic
- [ ] Why high pathogenicity evidence cannot rescue a technically invalid call

### Suggested searches

```text
NGS sequencing reads alignment variant calling beginner
sequencing depth coverage allele balance
VCF QUAL GQ DP difference
variant calling false positive low complexity
```

### Project exercise

- [ ] Explain the path:

```text
Biological sample → sequencing reads → alignment → variant calling → VCF
```

- [ ] Create three hypothetical VCF records:
  - high technical confidence;
  - low depth;
  - poor allele balance.
- [ ] Explain why the clinical engine should preserve technical limitations.

### Exit questions

- [ ] What is read depth?
- [ ] What is allele balance?
- [ ] What does a Phred-scaled score express?
- [ ] Why is variant quality different from pathogenicity?
- [ ] Why might a true variant be missed?

---

## 9. Module 7 — Variant annotation and biological consequence

**Priority:** Core  
**Suggested study time:** 8–12 hours

### Topics

- [ ] Annotation versus interpretation
- [ ] Gene overlap
- [ ] Transcript consequence
- [ ] Protein consequence
- [ ] Regulatory consequence
- [ ] Sequence Ontology consequence terms
- [ ] VEP `IMPACT` categories and their limitations
- [ ] Canonical versus MANE transcript
- [ ] Most severe consequence
- [ ] Transcript-specific consequences
- [ ] Existing variant identifiers
- [ ] Protein domain annotations
- [ ] Splice prediction
- [ ] Missense prediction
- [ ] Conservation
- [ ] Correlated prediction tools
- [ ] Tool calibration
- [ ] Loss-of-function prediction
- [ ] Nonsense-mediated decay prediction
- [ ] Why annotation is evidence input, not a final classification

### Suggested searches

```text
Ensembl VEP consequence terms
variant annotation versus variant interpretation
MANE Select clinical transcript
SpliceAI REVEL variant interpretation
loss of function annotation LOFTEE
```

### Primary resource

- [Ensembl Variant Effect Predictor](https://www.ensembl.org/info/docs/tools/vep/index.html)

### Project exercise

- [ ] Query one project variant in Ensembl VEP.
- [ ] List all transcript consequences.
- [ ] Select the clinically relevant transcript and justify the selection.
- [ ] Explain why `HIGH IMPACT` is not equivalent to `Pathogenic`.

### Exit questions

- [ ] What is the difference between annotation and interpretation?
- [ ] Why can VEP return multiple consequences for one allele?
- [ ] Why should correlated predictors not be counted independently?
- [ ] Why is a predicted splice effect not automatically clinical proof?

---

## 10. Module 8 — Population genetics and frequency evidence

**Priority:** Core for ACMG/AMP population criteria  
**Suggested study time:** 8–12 hours

### Topics

- [ ] Population and ancestry groups
- [ ] Allele count: `AC`
- [ ] Allele number: `AN`
- [ ] Allele frequency: `AF`
- [ ] Homozygote and hemizygote count
- [ ] Carrier frequency
- [ ] Population maximum frequency
- [ ] Filtering allele frequency
- [ ] Reference population versus disease cohort
- [ ] Sampling bias
- [ ] Population stratification
- [ ] Founder effect
- [ ] Genetic drift
- [ ] Hardy–Weinberg equilibrium: working knowledge
- [ ] Disease prevalence
- [ ] Penetrance
- [ ] Maximum credible allele frequency
- [ ] Coverage and missingness
- [ ] Why absence from a database is not proof of pathogenicity
- [ ] Why a global maximum frequency can hide important detail
- [ ] ACMG/AMP population criteria: `BA1`, `BS1`, `BS2`, and `PM2`

### Suggested searches

```text
gnomAD AC AN AF homozygote count
filtering allele frequency gnomAD
disease prevalence penetrance allele frequency threshold
population stratification clinical variant interpretation
ACMG BA1 BS1 PM2 population criteria
```

### Primary resource

- [gnomAD browser](https://gnomad.broadinstitute.org/)

### Project exercise

- [ ] Open one gnomAD variant.
- [ ] Record `AC`, `AN`, `AF`, population maximum frequency, and homozygote
  count.
- [ ] Explain why the same frequency may be benign for one disease but too
  common for another.
- [ ] Explain why no gnomAD record may mean `not assessable` rather than
  `absent`.

### Exit questions

- [ ] How is allele frequency calculated?
- [ ] Why does ancestry matter?
- [ ] What is the difference between absence and inadequate coverage?
- [ ] Why must population thresholds be disease specific?

---

## 11. Module 9 — Gene–disease validity and disease mechanisms

**Priority:** Core  
**Suggested study time:** 6–10 hours

### Topics

- [ ] Association versus causation
- [ ] Gene–disease relationship
- [ ] Disease entity
- [ ] Disease identifier
- [ ] MONDO terminology
- [ ] Gene–disease validity classifications
- [ ] Definitive, strong, moderate, limited, disputed, and refuted evidence
- [ ] Mode of inheritance
- [ ] Disease mechanism
- [ ] Loss-of-function mechanism
- [ ] Gain-of-function mechanism
- [ ] Dominant-negative mechanism
- [ ] Dosage sensitivity
- [ ] Haploinsufficiency
- [ ] Triplosensitivity
- [ ] Phenotypic spectrum
- [ ] Disease lumping and splitting
- [ ] One gene causing multiple diseases
- [ ] Multiple genes causing similar phenotypes
- [ ] Difference between gene validity and variant pathogenicity

### Suggested searches

```text
ClinGen gene disease validity classification
GenCC gene disease validity inheritance
MONDO disease ontology
haploinsufficiency triplosensitivity ClinGen
gene disease mechanism loss gain dominant negative
```

### Primary resources

- [ClinGen](https://clinicalgenome.org/)
- [GenCC](https://thegencc.org/)
- [MONDO](https://mondo.monarchinitiative.org/)

### Project exercise

- [ ] Choose one gene with more than one associated disease.
- [ ] Compare the diseases, inheritance modes, and mechanisms.
- [ ] Explain why one variant cannot be classified without selecting the
  disease context.

### Exit questions

- [ ] What is gene–disease validity?
- [ ] Why is a definitive gene–disease relationship not proof that every
  variant in the gene is pathogenic?
- [ ] Why can disease naming affect ACMG/AMP interpretation?

---

## 12. Module 10 — Clinical databases and evidence quality

**Priority:** Core  
**Suggested study time:** 10–14 hours

### ClinVar topics

- [ ] `VCV`, `RCV`, and `SCV`
- [ ] Variant-level versus variant-condition records
- [ ] Submitted versus aggregate classification
- [ ] Review status and star levels
- [ ] Practice guideline
- [ ] Expert-panel review
- [ ] Multiple submitters with agreement
- [ ] Conflicting classifications
- [ ] Assertion method
- [ ] Last evaluated versus last updated
- [ ] Submitter evidence
- [ ] Condition mismatch
- [ ] Why NCBI does not independently curate each submitted classification

### ClinGen topics

- [ ] Variant Curation Expert Panels: `VCEP`
- [ ] Criteria Specification Registry: `CSpec`
- [ ] Evidence Repository
- [ ] Gene-specific ACMG/AMP criteria
- [ ] General variant-classification guidance
- [ ] Specification versioning

### Other project-source topics

- [ ] Ensembl VEP
- [ ] MyVariant.info
- [ ] gnomAD
- [ ] HPO
- [ ] GenCC
- [ ] PubMed
- [ ] LitVar
- [ ] PubTator
- [ ] Data release and provenance
- [ ] Primary versus secondary evidence
- [ ] Database aggregation and circular evidence

### Suggested searches

```text
ClinVar SCV RCV VCV difference
ClinVar review status stars
ClinVar conflicting classifications
ClinGen CSpec registry
ClinGen evidence repository
primary evidence versus database assertion variant interpretation
```

### Primary resources

- [ClinVar classification representation](https://www.ncbi.nlm.nih.gov/clinvar/docs/clinsig/)
- [ClinVar review status](https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/)
- [ClinGen classification guidance](https://www.clinicalgenome.org/tools/clingen-variant-classification-guidance/)
- [ClinGen Criteria Specification Registry](https://cspec.clinicalgenome.org/cspec/ui/svi/)

### Project exercise

- [ ] Open one ClinVar record with multiple submissions.
- [ ] List every relevant `SCV`.
- [ ] Compare condition, classification, review status, date, and rationale.
- [ ] Decide whether disagreement is:
  - the same clinical context;
  - a different disease context;
  - an old-versus-new evidence difference;
  - a genuine unresolved conflict.

### Exit questions

- [ ] What is the difference between an `SCV` and a `VCV`?
- [ ] What does ClinVar star status represent?
- [ ] Why should a ClinVar label not automatically become an ACMG/AMP
  criterion?
- [ ] Why are condition and date important when comparing assertions?

---

## 13. Module 11 — ACMG/AMP variant interpretation

**Priority:** Core and project-critical  
**Suggested study time:** 20–30 hours initially, followed by continued study

### Foundations

- [ ] Purpose and scope of the ACMG/AMP germline sequence-variant guidelines
- [ ] Five-tier classification system
- [ ] Pathogenic versus likely pathogenic
- [ ] VUS
- [ ] Likely benign versus benign
- [ ] Evidence direction
- [ ] Evidence strength
- [ ] Very strong, strong, moderate, and supporting pathogenic evidence
- [ ] Stand-alone, strong, and supporting benign evidence
- [ ] Original combination rules
- [ ] Bayesian point interpretation
- [ ] Gene- and disease-specific specification
- [ ] Classification versus patient diagnosis

### Pathogenic criteria

- [ ] `PVS1`
- [ ] `PS1`
- [ ] `PS2`
- [ ] `PS3`
- [ ] `PS4`
- [ ] `PM1`
- [ ] `PM2`
- [ ] `PM3`
- [ ] `PM4`
- [ ] `PM5`
- [ ] `PM6`
- [ ] `PP1`
- [ ] `PP2`
- [ ] `PP3`
- [ ] `PP4`
- [ ] Understand current guidance regarding `PP5`

### Benign criteria

- [ ] `BA1`
- [ ] `BS1`
- [ ] `BS2`
- [ ] `BS3`
- [ ] `BS4`
- [ ] `BP1`
- [ ] `BP2`
- [ ] `BP3`
- [ ] `BP4`
- [ ] `BP5`
- [ ] Understand current guidance regarding `BP6`
- [ ] `BP7`

### Evidence-control topics

- [ ] Criterion strength modification
- [ ] Conflicting pathogenic and benign criteria
- [ ] Double counting
- [ ] Correlated computational evidence
- [ ] Missing evidence
- [ ] `Not applicable` versus `Not assessable`
- [ ] Functional-assay validation
- [ ] Segregation evidence
- [ ] De novo evidence
- [ ] Phase evidence
- [ ] Phenotype specificity
- [ ] Human review
- [ ] Rule and source versioning

### Suggested searches

```text
Richards 2015 ACMG AMP variant interpretation
ClinGen variant classification guidance
ClinGen PVS1 decision tree
ClinGen PP3 BP4 calibrated predictors
ClinGen PP1 BS4 PP4 guidance
ACMG Bayesian point system
ClinGen CSpec gene specific criteria
```

### Primary resources

- [Original ACMG/AMP guidelines](https://pmc.ncbi.nlm.nih.gov/articles/PMC4544753/)
- [ClinGen classification guidance](https://www.clinicalgenome.org/tools/clingen-variant-classification-guidance/)
- [ClinGen Variant Curation Interface documentation](https://vci-gci-docs.clinicalgenome.org/vci-gci-docs/vci-help/about-variant-curation)

### Project exercise

- [ ] Select one ClinGen expert-curated variant.
- [ ] Reproduce its criterion ledger.
- [ ] For every criterion, record:
  - criterion code;
  - strength;
  - evidence;
  - source;
  - disease context;
  - whether the project could automate it.
- [ ] Explain why the final class follows from the criteria.
- [ ] Identify criteria that require manual evidence.

### Exit questions

- [ ] What exact question does ACMG/AMP classification answer?
- [ ] What is the difference between `PVS1` and a VEP `HIGH` impact label?
- [ ] Why is phenotype similarity not enough to apply `PP4`?
- [ ] Why is ACMG/AMP evidence scoring different from patient priority?
- [ ] When must the engine return `Not assessable`?

---

## 14. Module 12 — Phenotypes, HPO, and the gene–phenotype graph

**Priority:** Core for the conclusion layer  
**Suggested study time:** 10–15 hours

### Phenotype topics

- [ ] Sign versus symptom
- [ ] Phenotype
- [ ] Phenotypic feature
- [ ] Clinical diagnosis versus phenotype description
- [ ] Specific versus nonspecific phenotype
- [ ] Positive versus explicitly absent phenotype
- [ ] Age of onset
- [ ] Disease progression
- [ ] Phenotypic variability

### HPO topics

- [ ] HPO identifier
- [ ] Preferred term and synonym
- [ ] Parent and child terms
- [ ] Ancestors and descendants
- [ ] Ontology DAG
- [ ] Disease–phenotype annotation
- [ ] Gene–phenotype annotation
- [ ] Annotation frequency
- [ ] Evidence and provenance
- [ ] Information content
- [ ] Semantic similarity
- [ ] Exact overlap versus ontology-aware similarity

### Graph topics

- [ ] Node and edge
- [ ] Directed relationship
- [ ] Provenance-backed edge
- [ ] Direct relationship versus inferred path
- [ ] Supporting versus contradicting edge
- [ ] Variant → gene relationship
- [ ] Gene → disease relationship
- [ ] Disease → phenotype relationship
- [ ] Gene → phenotype relationship
- [ ] Disease → inheritance relationship
- [ ] Patient phenotype → disease → gene → variant path
- [ ] MONDO disease normalization
- [ ] Monarch Knowledge Graph
- [ ] Graph release/version
- [ ] Explainable path scoring
- [ ] Why graph similarity is not proof of pathogenicity

### Suggested searches

```text
Human Phenotype Ontology beginner
HPO semantic similarity information content
gene phenotype disease knowledge graph
Monarch Knowledge Graph HPO gene disease
ClinGen PP4 phenotype specificity
phenotype exact match versus semantic similarity
```

### Primary resources

- [Human Phenotype Ontology](https://obophenotype.github.io/human-phenotype-ontology/)
- [Monarch Initiative](https://monarchinitiative.org/)
- [ClinGen PP1/BS4 and PP4 guidance](https://www.clinicalgenome.org/docs/clingen-guidance-for-use-of-the-pp1-bs4-co-segregation-and-pp4-phenotype-specificity-criteria-for-sequence-variant/)

### Project exercise

- [ ] Select three patient HPO terms.
- [ ] Find their parents and grandparents in HPO.
- [ ] Find one disease and gene associated with those terms.
- [ ] Draw this provenance-backed path:

```text
Patient HPO
→ related disease phenotype
→ MONDO disease
→ gene
→ supplied variant
```

- [ ] Identify which edges are direct evidence and which path is inferred.
- [ ] Explain how the path affects patient priority without changing ACMG/AMP
  pathogenicity.

### Exit questions

- [ ] Why is exact HPO overlap insufficient?
- [ ] What does information content represent?
- [ ] What is the difference between a sourced edge and an inferred path?
- [ ] When can phenotype evidence contribute to `PP4`?
- [ ] Why should the graph score be separate from ACMG/AMP points?

---

## 15. Module 13 — Scientific literature and functional evidence

**Priority:** Working knowledge and required for conflict interpretation  
**Suggested study time:** 10–15 hours

### Literature topics

- [ ] Primary research article
- [ ] Review article
- [ ] Case report
- [ ] Case series
- [ ] Cohort study
- [ ] Case-control study
- [ ] Meta-analysis
- [ ] PMID, PMCID, and DOI
- [ ] Abstract versus full text
- [ ] Methods, results, discussion, and conclusion
- [ ] Supplementary material
- [ ] Preprint versus peer-reviewed publication
- [ ] Retraction and correction
- [ ] Citation does not guarantee evidence quality

### Study-quality topics

- [ ] Sample size
- [ ] Controls
- [ ] Replication
- [ ] Statistical significance
- [ ] Effect size
- [ ] Confidence interval
- [ ] Multiple testing
- [ ] Ascertainment bias
- [ ] Publication bias
- [ ] Cohort overlap
- [ ] Duplicate patient counting
- [ ] Ancestry and population matching

### Functional-evidence topics

- [ ] In vitro versus in vivo
- [ ] Patient-derived cells
- [ ] Animal model
- [ ] Cell-line model
- [ ] Protein-function assay
- [ ] RNA/splicing assay
- [ ] Rescue experiment
- [ ] Positive and negative controls
- [ ] Assay calibration
- [ ] Biological relevance
- [ ] Reproducibility
- [ ] Functional abnormality versus proof of disease causality

### Suggested searches

```text
how to read genetics research paper
functional assay variant interpretation PS3 BS3
ClinGen functional evidence guidance
case control odds ratio confidence interval genetics
duplicate probands variant literature
```

### Project exercise

- [ ] Choose one paper linked from a ClinVar assertion.
- [ ] Record:
  - exact variant;
  - transcript;
  - disease;
  - patient count;
  - study type;
  - functional assay;
  - controls;
  - conclusion;
  - limitations.
- [ ] Decide whether it supplies primary evidence or only repeats another
  source.

### Exit questions

- [ ] What makes a functional assay well validated?
- [ ] Why can two papers reach different conclusions?
- [ ] Why must duplicate patients be detected?
- [ ] Why should the LLM not treat an abstract conclusion as verified fact?

---

## 16. Module 14 — Conflict, uncertainty, and clinical reporting

**Priority:** Core for the LLM conclusion layer  
**Suggested study time:** 8–12 hours

### Topics

- [ ] Evidence agreement versus evidence independence
- [ ] True conflict versus different clinical contexts
- [ ] Different diseases for the same variant
- [ ] Different transcripts
- [ ] Different inheritance modes
- [ ] Old versus new evidence
- [ ] Different population-database releases
- [ ] Different functional assays
- [ ] Review-status precedence
- [ ] VUS
- [ ] Insufficient evidence
- [ ] Unresolved material conflict
- [ ] Reclassification over time
- [ ] Variant-level classification versus patient-level interpretation
- [ ] Clinical decision support
- [ ] Diagnostic conclusion
- [ ] Limitation statements
- [ ] Human-review requirement
- [ ] Citation-backed reporting
- [ ] Why uncertainty must remain visible

### Suggested searches

```text
ClinVar conflicting classifications review status
variant reinterpretation reclassification over time
VUS clinical meaning
clinical variant report limitations
variant pathogenicity versus patient diagnosis
```

### Project exercise

- [ ] Compare two conflicting ClinVar submissions.
- [ ] Create a conflict matrix:

```text
Source
Disease
Inheritance
Transcript
Classification
Criteria
Review status
Date
Evidence
```

- [ ] Label the conflict:
  - concordant;
  - context difference;
  - resolved by precedence;
  - minor unresolved;
  - material unresolved;
  - insufficient evidence.
- [ ] Write a short conclusion that preserves the uncertainty.

### Exit questions

- [ ] When are two different classifications not genuinely conflicting?
- [ ] What makes a conflict material?
- [ ] Why should a strong LLM not be allowed to silently select a winner?
- [ ] What should the report say when evidence is insufficient?

---

## 17. Module 15 — Ethics, privacy, and responsible software

**Priority:** Core  
**Suggested study time:** 5–8 hours

### Topics

- [ ] Genetic data as sensitive personal data
- [ ] Patient identifier
- [ ] Sample identifier
- [ ] Pedigree privacy
- [ ] Data minimization
- [ ] Purpose limitation
- [ ] Consent
- [ ] Secondary findings
- [ ] Incidental findings
- [ ] Clinical decision support versus diagnosis
- [ ] Genetic discrimination
- [ ] Re-identification risk
- [ ] Secure storage
- [ ] Audit logging
- [ ] Source licensing
- [ ] Database terms of use
- [ ] Model/provider data handling
- [ ] Prompt injection from external scientific text
- [ ] Hallucination
- [ ] Human oversight
- [ ] Reproducibility and versioning
- [ ] Communicating uncertainty

### Suggested searches

```text
genetic data privacy clinical genomics
genetic discrimination NHGRI
clinical decision support variant interpretation disclaimer
LLM biomedical hallucination evidence citations
genomic database licensing terms of use
```

### Project exercise

- [ ] List every patient field the project could receive.
- [ ] Mark each as required, optional, prohibited, transient, or persisted.
- [ ] Draw the boundary between deterministic evidence and LLM input.
- [ ] Explain what happens if the LLM is unavailable.

### Exit questions

- [ ] Why can a genome be identifying?
- [ ] Why should raw VCF data not be sent to the LLM?
- [ ] Why must source and rule versions be stored?
- [ ] What is the safe fallback when an LLM response fails validation?

---

## 18. Later topics outside the first release

Study these after the germline SNV/indel workflow is working and validated.

- [ ] Copy-number variant interpretation
- [ ] Structural-variant interpretation
- [ ] Repeat expansions
- [ ] Mitochondrial genetics and heteroplasmy
- [ ] Somatic cancer variants
- [ ] Clonal hematopoiesis
- [ ] Pharmacogenomics
- [ ] Polygenic risk scores
- [ ] Genome-wide association studies
- [ ] Noncoding regulatory variants
- [ ] Epigenetics
- [ ] Mosaicism in depth
- [ ] RNA sequencing
- [ ] Long-read sequencing
- [ ] Pangenome references
- [ ] Prenatal genetics
- [ ] Newborn screening
- [ ] Secondary findings policies
- [ ] Clinical actionability

Do not mix these standards into the initial ACMG/AMP germline sequence-variant
engine without a separately approved roadmap.

---

## 19. Project-stage knowledge gates

| Roadmap stage | Genetics knowledge required first |
|---|---|
| Stage 0: Output contract | Modules 1–4 overview |
| Stage 1: Evidence schema | Modules 1–5 and 9–10 |
| Stage 2: Disease/case context | Modules 4, 9, and 12 |
| Stage 3: Evidence infrastructure | Modules 5, 7, and 10 |
| Stage 4: Population/CSpec | Modules 8, 9, 10, and 11 |
| Stage 5: ClinVar/literature | Modules 10, 13, and 14 |
| Stage 6: Gene–phenotype graph | Modules 9 and 12 |
| Stage 7: ACMG framework | Module 11 |
| Stage 8: Automated criteria | Modules 7, 8, and 11 |
| Stage 9: Manual criteria | Modules 4, 11, 12, and 13 |
| Stage 10: Classification/points | Module 11 |
| Stage 11: Priority/conflict | Modules 4, 9, 12, and 14 |
| Stage 12: LLM interpretation | Modules 10, 13, 14, and 15 |
| Stage 13: Reports | Modules 14 and 15 |
| Stage 14: Validation | All core modules |

---

## 20. Suggested eight-week study schedule

### Week 1 — Biological vocabulary

- [ ] Module 1
- [ ] Module 2
- [ ] Build a personal glossary

### Week 2 — Variation and inheritance

- [ ] Module 3
- [ ] Module 4
- [ ] Practice pedigrees and genotype examples

### Week 3 — Representation and sequencing

- [ ] Module 5
- [ ] Module 6
- [ ] Manually inspect project VCF examples

### Week 4 — Annotation and population evidence

- [ ] Module 7
- [ ] Module 8
- [ ] Inspect one VEP and one gnomAD record

### Week 5 — Disease and databases

- [ ] Module 9
- [ ] Module 10
- [ ] Compare individual ClinVar submissions

### Week 6 — ACMG/AMP

- [ ] Begin Module 11
- [ ] Reproduce one expert-curated criterion ledger
- [ ] Do not attempt to memorize all criteria without examples

### Week 7 — Phenotypes and graph reasoning

- [ ] Module 12
- [ ] Build one manual HPO–disease–gene–variant graph

### Week 8 — Literature, conflict, and responsibility

- [ ] Module 13
- [ ] Module 14
- [ ] Module 15
- [ ] Produce one complete mock per-variant report

Continue ACMG/AMP study throughout later implementation.

---

## 21. Personal glossary checklist

Create your own one-sentence definition and one project example for every term:

- [ ] Allele
- [ ] Assembly
- [ ] Chromosome
- [ ] Codon
- [ ] Consequence
- [ ] De novo
- [ ] Disease mechanism
- [ ] Exon
- [ ] Gene
- [ ] Gene–disease validity
- [ ] Genotype
- [ ] Germline
- [ ] Haplotype
- [ ] Hemizygous
- [ ] Heterozygous
- [ ] HGVS
- [ ] Homozygous
- [ ] HPO
- [ ] Inheritance
- [ ] Intron
- [ ] Locus
- [ ] MANE
- [ ] NMD
- [ ] Pathogenicity
- [ ] Penetrance
- [ ] Phase
- [ ] Phenotype
- [ ] Priority
- [ ] Proband
- [ ] Protein domain
- [ ] Reference allele
- [ ] Segregation
- [ ] Transcript
- [ ] Variant
- [ ] VCF
- [ ] VUS
- [ ] Zygosity

---

## 22. Final self-assessment

Do not consider the core study complete until you can answer all of these
without relying on the LLM:

- [ ] I can explain the path from DNA to RNA to protein.
- [ ] I can explain gene, transcript, exon, CDS, and protein.
- [ ] I can explain the major SNV/indel consequences.
- [ ] I can explain dominant, recessive, X-linked, and de novo inheritance.
- [ ] I can explain heterozygous, homozygous, hemizygous, cis, and trans.
- [ ] I can read the main fields of a VCF row.
- [ ] I can explain why assembly is mandatory.
- [ ] I can explain why variants require normalization.
- [ ] I can distinguish VCF representation from HGVS description.
- [ ] I can distinguish annotation from interpretation.
- [ ] I can explain allele count, allele number, and allele frequency.
- [ ] I can explain why population frequency thresholds are disease specific.
- [ ] I can distinguish gene–disease validity from variant pathogenicity.
- [ ] I can distinguish ClinVar submissions from aggregate classifications.
- [ ] I understand ClinVar review status.
- [ ] I can explain what ACMG/AMP does and does not do.
- [ ] I can explain at least one example from each ACMG/AMP evidence category.
- [ ] I can explain why missing evidence is not benign evidence.
- [ ] I can explain why ACMG/AMP points are not patient priority.
- [ ] I can explain HPO hierarchy and semantic similarity.
- [ ] I can distinguish a graph edge from an inferred graph path.
- [ ] I can explain how the graph helps priority without proving pathogenicity.
- [ ] I can identify a genuine evidence conflict.
- [ ] I can explain why an LLM must not set the final ACMG/AMP classification.
- [ ] I can state the project's important scientific and privacy limitations.

---

## 23. Core reference library

### Beginner genetics

- [MedlinePlus: Help Me Understand Genetics](https://medlineplus.gov/genetics/understanding/)
- [MedlinePlus genetics primer PDF](https://medlineplus.gov/download/genetics/understanding/primer.pdf)
- [NHGRI Talking Glossary](https://www.genome.gov/genetics-glossary)
- [NHGRI GenomeEd](https://www.genome.gov/GenomeEd)

### Variant representation

- [HTS specifications and VCF](https://github.com/samtools/hts-specs)
- [HGVS Nomenclature](https://hgvs-nomenclature.org/)
- [HGVS checklist](https://hgvs-nomenclature.org/stable/recommendations/checklist/)

### Annotation and population data

- [Ensembl VEP](https://www.ensembl.org/info/docs/tools/vep/index.html)
- [gnomAD](https://gnomad.broadinstitute.org/)

### Clinical evidence

- [ClinVar](https://www.ncbi.nlm.nih.gov/clinvar/)
- [ClinVar review status](https://www.ncbi.nlm.nih.gov/clinvar/docs/review_status/)
- [ClinGen](https://clinicalgenome.org/)
- [GenCC](https://thegencc.org/)

### ACMG/AMP

- [Richards et al. 2015](https://pmc.ncbi.nlm.nih.gov/articles/PMC4544753/)
- [ClinGen variant-classification guidance](https://www.clinicalgenome.org/tools/clingen-variant-classification-guidance/)
- [ClinGen CSpec Registry](https://cspec.clinicalgenome.org/cspec/ui/svi/)

### Phenotypes and graphs

- [Human Phenotype Ontology](https://obophenotype.github.io/human-phenotype-ontology/)
- [Monarch Initiative](https://monarchinitiative.org/)
- [MONDO](https://mondo.monarchinitiative.org/)

### Literature

- [PubMed](https://pubmed.ncbi.nlm.nih.gov/)
- [LitVar](https://www.ncbi.nlm.nih.gov/research/litvar2/)
- [PubTator](https://www.ncbi.nlm.nih.gov/research/pubtator3/)

---

## 24. Recommended study rule

For every genetics concept, record:

```text
Definition:
Why it matters biologically:
Where it appears in this project:
Which database provides it:
Which ACMG/AMP criterion may use it:
What can go wrong if software misinterprets it:
One real variant example:
```

This structure connects biological learning directly to software design and
prevents memorizing definitions without understanding their project impact.
