# Source Datasets

Reference for the four input sources: what each represents, what every column means, and what the
pipeline must handle. All data in this repository is synthetic.

## Overview

| Dataset | In plain words | Measures | Keyed by |
|---|---|---|---|
| EHR vitals | What clinic staff measured at a visit | Body measurements (heart rate, blood pressure) | Person + visit |
| Lab A | What a clinical lab found in a blood sample | A substance in blood (CRP) | Person + sample |
| Lab B | Same idea as Lab A; different lab, names, method and unit | Same substance | Person + sample |
| Proteomics (Olink) | Thousands of proteins measured at once in a sample | Relative protein levels | **Sample only** |

## Terms

| Term | Meaning |
|---|---|
| Patient / participant / subject | The person. Hospitals say *patient*; studies say *participant* or *subject*. |
| Encounter | One clinic visit |
| Sample / specimen | A physical tube of body fluid. Same idea, two words. |
| Aliquot | A portion split from one sample so several labs can test it |
| Analyte | The substance being measured |
| Assay | The method used to measure an analyte |
| CRP | C-reactive protein; rises with inflammation |
| hs-CRP | The same protein measured with a high-sensitivity assay |
| NPX | Olink's Normalized Protein eXpression: a relative, log2-scale value |
| UniProt ID | Standard global identifier for a protein (CRP = `P02741`) |
| LOINC | Standard code system for lab tests and clinical observations |
| QC flag | The lab's quality-control verdict on a measurement |

## Example timeline

```
Jan 10, 09:30  Clinic visit E300: heart rate 72 bpm                     → EHR vitals
Jan 10         Blood drawn into sample S100, split into aliquots
               ├─ Lab A measures CRP = 3.2 mg/L                         → Lab A
               └─ one aliquot frozen
Mar 01         Second blood draw, sample S200; Lab B hs-CRP = 3200 ug/L → Lab B
Mar 05         Frozen S100 run on Olink: CRP NPX = 10.4                 → Proteomics
```

One person, two visits, two samples, three organizations, four files.

---

## EHR vitals

Recorded by clinic staff during a visit; exported from the electronic health record.

| Column | Example | Meaning | Watch out for |
|---|---|---|---|
| `patient_id` | P001 | Person, in the clinic's numbering | Unique only within this source |
| `encounter_id` | E300 | The visit | A visit, not a sample; vitals have no sample |
| `vital_type` | HR, SYS_BP | What was measured | Local codes; mapped to canonical names |
| `value` | 72 | The number | Real data may contain `"120/80"` as one string |
| `unit` | bpm, mmHg | Unit | Mixed units (°F/°C, lb/kg) occur in real data |
| `recorded_at` | 2026-01-10 09:30 | When measured | No timezone |

Domain: operational.

## Lab A

A clinical lab measured CRP in a blood sample.

| Column | Example | Meaning | Watch out for |
|---|---|---|---|
| `participant_id` | P001 | Person, in the study's numbering | Unique only within this source |
| `sample_id` | S100 | The blood sample | The same ID is used by the proteomics vendor |
| `analyte` | CRP | Substance measured | Lab's own naming |
| `value` | 3.2 | Amount | Real labs may send censored values such as `"<0.3"` |
| `unit` | mg/L | Milligrams per litre | Reference unit for CRP |
| `collected_at` | 2026-01-10 | When the blood was drawn | Date only |

Domain: scientific. **Lab A is the bridge:** it is the only source linking sample S100 to a person.

## Lab B

A different lab reporting the same protein with different names, method and unit.

| Column | Example | Lab A equivalent | Watch out for |
|---|---|---|---|
| `subject` | P001 | `participant_id` | Whether this is Lab A's P001 requires evidence |
| `specimen` | S200 | `sample_id` | A different sample (March) |
| `test_code` | HSCRP | `analyte` | High-sensitivity CRP: same protein, different assay |
| `result` | 3200 | `value` | Appears 1000× larger… |
| `unit` | ug/L | `unit` | …because it is micrograms: 3200 ug/L = 3.2 mg/L |
| `collection_date` | 2026-03-01 | `collected_at` | Same meaning, different name |

Domain: scientific. Requires column mapping, unit conversion and analyte mapping (keeping the assay).

## Proteomics (Olink)

A proteomics vendor measured many proteins in a sample. One row = one protein in one sample.

| Column | Example | Meaning | Watch out for |
|---|---|---|---|
| `SampleID` | S100 | The sample | **The only identifier; no person ID** |
| `AssayTarget` | CRP, TNF | Protein short name | |
| `UniProtID` | P02741 | Standard protein ID | The reliable protein key |
| `NPX_Value` | 10.4 | Relative level, log2 scale | Not a concentration; not convertible. +1 ≈ 2× more |
| `QC_Flag` | PASS, WARN | Quality verdict | WARN: keep and flag |
| `RunDate` | 2026-03-05 | When the assay ran | Not when the blood was drawn |

Domain: scientific. The person is found through the sample (S100 → Lab A → P001); the biological date
comes from the sample's collection date.

Real NPX deliveries typically include additional columns (plate, panel, limit of detection, assay-level
warnings) and rows for control samples that are not people. Exact column names vary by product and
software version.

## Metabolomics (planned fifth source — designed, not built)

Not part of the brief, and not implemented: the TSV reader exists, but the wide → long shape step does
not. Designed as the proof that a new file format, data shape and kind of data can be onboarded without
changing the pipeline ([ADR-010](decisions/ADR-010-pluggable-source-adapters.md)). Synthetic; it would not
reproduce any specific vendor's format.

**Format:** tab-separated. **Shape:** wide — one row per sample, one column per metabolite.

```
sample_id	run_date	glucose	lactate	alanine	citrate
S100	2026-03-10	1.12	0.87	1.05
S101	2026-03-10	0.94	1.21		0.99
```

| Column | Meaning | Watch out for |
|---|---|---|
| `sample_id` | The sample (same `study_biobank` namespace as Lab A and proteomics) | No person ID; resolved through the sample |
| `run_date` | When the assay ran | Not the collection date (same as proteomics) |
| metabolite columns | One relative value per metabolite | Column names are the analyte codes; empty cell = not reported |

After reshaping, each value cell would become one long row: `S100 | glucose | 1.12`, with the column name
kept as `source_locator` for lineage. Values would be **relative** (normalized intensity), never converted
and never combined with absolute or NPX values.

## Other vendor formats (documented, not built)

Example contracts show what the adapter layer would need for two further formats:

| Format | Shape | Example | Adapter needs |
|---|---|---|---|
| Clinical lab export (CSV) | Long, many columns | `accession_number, patient_ref, specimen_id, specimen_type, test_code, loinc_code, result_value, result_units, reference_range, abnormal_flag, result_status, collected_datetime` | Existing CSV reader; mapping only |
| Proteomics (JSON) | Nested | `{"run_date": …, "samples": [{"sample_id": "S100", "results": [{"uniprot": "P02741", "value": 10.4, "qc": "PASS"}]}]}` | JSON reader; flatten by record path |

---

## How the sources connect

```
                        ┌──────────── EHR vitals (P001, visit E300)
                        │
  Person P001 ──────────┼──────────── Lab A  (P001, sample S100) ◀─┐
                        │                                          │ same sample
                        ├──────────── Lab B  (P001?, sample S200)  │
                        │                                          │
                        └ ─ ─ ─ ─ ─ ─ Proteomics (sample S100) ────┘
```

Solid lines: the file states the person. Dashed: the person is derived through the sample.
`P001?`: same person only with linking evidence.

## What can be compared

| Comparison | Comparable? | Why |
|---|---|---|
| Lab A CRP vs Lab B CRP | Yes, after unit conversion | Same protein, absolute amounts; assay differs |
| Lab A CRP vs Olink CRP | No, not numerically | Absolute (mg/L) vs relative (NPX) |
| Olink CRP across samples | Yes, with caveats | Same scale if measured in the same run or bridged batches |
| Vitals vs lab values | Different quantities | Related by person and time, not compared numerically |

## Fields in real deliveries not present in the sample data

| Source type | Field | Relevance |
|---|---|---|
| Clinical labs | Result status (HL7 OBX-11: `P` preliminary, `F` final, `C` corrected, `W` withdrawn as wrong) | How corrections and retractions arrive |
| Clinical labs | Reference range, abnormal flag | Interpretation |
| Clinical labs | Censored results (`<0.3`, `>200`), `HEMOLYZED`, `QNS` | Not plain numbers |
| Clinical labs | LOINC code, specimen type | Standard coding |
| Proteomics | Plate, panel, limit of detection, control samples | QC and filtering |
| EHR | Combined values (`120/80`), local timestamps, amendments | Parsing and corrections |

The prototype models lab result status and Olink QC / control-sample rows; the rest are documented as
out of scope ([ADR-008](decisions/ADR-008-data-realism.md)).
