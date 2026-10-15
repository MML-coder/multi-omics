# ADR-004: No manual DLQ overrides in the prototype

Status: Accepted

## Context
Rows rejected to the DLQ need a path back into the pipeline once the problem is understood. A person
could fix the value directly, or the fix could come from the source or from the pipeline's rules and
reference data.

## Options
1. **Fixes only via vendor resend, or via reviewed changes to rules / reference data followed by replay.**
2. Allow operators to edit DLQ rows and resubmit them.
3. Allow overrides as a separate, recorded input (who, why, when), applied on top of source data.

## Decision
Option 1 for the prototype. Option 3 is the documented path if overrides become necessary.

## Tradeoffs
- Gain: every value in silver is traceable to a source file or a versioned rule/reference change; no
  silent human edits; the DLQ stays append-only and auditable.
- Give up: a genuinely wrong source value that the vendor will not resend cannot be fixed; the row stays
  in the DLQ (or is marked `DISCARDED` with a reason).
- Option 2 rejected: hand-edited values break provenance — nobody can tell the value came from a person
  rather than the lab.

## Local vs AWS
No difference.

## At scale
Holds. With many sources, the volume of DLQ rows that need human judgement grows; option 3 (recorded
overrides with an approval step) becomes worthwhile.

## Revisit when
Recurring cases arise where data must be corrected and the source cannot or will not resend it.
