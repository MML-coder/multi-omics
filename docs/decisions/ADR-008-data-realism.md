# ADR-008: Model lab result status and proteomics QC / control rows

Status: Accepted

## Context
The sample tables in the brief have six columns per source. Real deliveries carry fields that three
required capabilities depend on:

- **Corrections:** labs mark a corrected result with status `C` (HL7 v2 OBX-11).
- **Retractions:** labs withdraw a wrong result with status `W`.
- **Validation / QC:** proteomics files contain control-sample rows (not people) and quality flags.

The brief allows extending the synthetic datasets.

## Options
1. **Add result status to lab sources and QC / control-sample rows to proteomics; leave censored values
   out of scope.**
2. Keep the brief's schemas exactly.
3. Model all realistic fields (result status, censored values, reference ranges, specimen type, LOD).

## Decision
Option 1.

## Tradeoffs
- Gain: corrections and retractions are explicit rather than inferred; control samples cannot become
  fake participants; the demo shows real failure modes.
- Give up: source schemas differ from the brief and need a short justification; more fixtures.
- Option 2 rejected: corrections would be guessed from "same key, new value", and retractions could not be
  represented at all.
- Option 3 rejected: censored values, reference ranges and specimen types are realistic but support no
  required capability; they would add scope without adding evidence. Censored values are rejected as
  non-numeric and documented.

## Local vs AWS
No difference.

## At scale
Real vendor feeds will carry more fields over time; versioned source contracts absorb them without
changing the canonical model.

## Revisit when
A consumer needs censored values or reference ranges, or a real vendor file layout becomes available.
