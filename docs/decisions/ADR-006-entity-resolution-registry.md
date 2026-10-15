# ADR-006: Entity resolution links identifiers only with registry evidence

Status: Accepted

## Context
Several sources use similar-looking person identifiers (`P001` in EHR, Lab A and Lab B). Identifiers are
scoped to their source (assumption A1). Proteomics has no person identifier at all and is linked through
the sample. The two possible errors are not symmetric:

- **False merge** (two people under one ID): one person's results appear in another's record. Clinically
  wrong, a privacy breach, and hard to detect afterwards.
- **Missed match** (one person under two IDs): incomplete data; recoverable by linking later.

## Options
1. **Enrollment registry:** a study-maintained file mapping each source's person ID to one study
   participant. Links are created only from this evidence (plus sample links for proteomics).
2. Same identifier string = same person across sources.
3. Probabilistic matching on demographics.

## Decision
Option 1.

## Tradeoffs
- Gain: every link has auditable evidence (registry file, version, row); errors can only be missed
  matches, never false merges. Mirrors how studies manage enrollment.
- Give up: one more input to maintain; subjects missing from the registry stay unlinked (`HELD`) until
  the registry is updated.
- Option 2 rejected: the only option that can silently merge two people, based on an unverifiable
  assumption.
- Option 3 rejected: the data contains no demographics, and a measurement platform should not hold them.

## Local vs AWS
Locally the registry is a versioned file and links live in `entity_links`. On AWS the crosswalk moves to a
restricted store, separate from measurement data, since it is the table that links pseudonymous IDs.

## At scale
With many sources the crosswalk becomes a shared identity service used across domains. Probabilistic
matching, if ever needed, runs inside a restricted PII zone and publishes only global IDs.

## Revisit when
A source arrives without registry coverage, or the registry itself becomes a bottleneck for onboarding.
