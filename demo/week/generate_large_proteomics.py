#!/usr/bin/env python3
"""Regenerates the large proteomics part of the week demo (seeded, so the output is always identical).

Week 1: the original samples S100-S103 plus a study cohort BS0001-BS0296, each with a 50-protein panel.
Lab A links half the cohort on day 1 and the other half one day late (day 2), so half the week-1
proteomics waits ("held") and then resolves. Week 2: S104-S105 plus BS0297-BS0446, linked in the same run.

Protein panel: CRP and TNF (real UniProt IDs from the brief) plus 48 clearly synthetic proteins.
"""

import csv
import random
from pathlib import Path

WEEK = Path(__file__).resolve().parent
rng = random.Random(2026)

SYNTHETIC = [(f"SYNPROT_{i:03d}", f"SYN{i:05d}") for i in range(1, 49)]
PANEL = [("CRP", "P02741"), ("TNF", "P01375")] + SYNTHETIC
COHORT_DAY1 = [f"BS{i:04d}" for i in range(1, 149)]
COHORT_DAY2 = [f"BS{i:04d}" for i in range(149, 297)]
COHORT_WEEK2 = [f"BS{i:04d}" for i in range(297, 447)]


def participant(sample: str) -> str:
    return f"P{1000 + int(sample[2:])}"


def write(path: str, header: list[str], rows: list[list[str]]) -> None:
    target = WEEK / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def lab_a(fixed: list[list[str]], cohort: list[str], date: str) -> list[list[str]]:
    return fixed + [[participant(s), s, "CRP", f"{rng.uniform(0.4, 8.0):.2f}", "mg/L", date] for s in cohort]


def proteomics(fixed: dict[str, list[list[str]]], cohort: list[str], run_date: str) -> list[list[str]]:
    rows = []
    for sample, sample_rows in fixed.items():
        rows += sample_rows
        rows += [[sample, name, uniprot, f"{rng.gauss(5.0, 2.0):.2f}", "PASS", run_date] for name, uniprot in SYNTHETIC]
    for sample in cohort:
        rows += [[sample, name, uniprot, f"{rng.gauss(5.0, 2.0):.2f}", "WARN" if rng.random() < 0.03 else "PASS", run_date]
                 for name, uniprot in PANEL]
    return rows


LAB_A = ["participant_id", "sample_id", "analyte", "value", "unit", "collected_at"]
OLINK = ["SampleID", "AssayTarget", "UniProtID", "NPX_Value", "QC_Flag", "RunDate"]

write("day1/lab_a/lab_a_day1.csv", LAB_A, lab_a(
    [["P001", "S100", "CRP", "3.2", "mg/L", "2026-01-10"], ["P002", "S101", "CRP", "1.7", "mg/L", "2026-01-11"]],
    COHORT_DAY1, "2026-01-12"))
write("day2/lab_a/lab_a_day2_late.csv", LAB_A, lab_a(
    [["P001", "S102", "CRP", "2.6", "mg/L", "2026-01-20"], ["P002", "S103", "CRP", "1.5", "mg/L", "2026-01-21"]],
    COHORT_DAY2, "2026-01-13"))
write("day7/lab_a/lab_a_day7.csv", LAB_A, lab_a(
    [["P001", "S104", "CRP", "2.2", "mg/L", "2026-02-09"], ["P002", "S105", "CRP", "1.4", "mg/L", "2026-02-09"]],
    COHORT_WEEK2, "2026-02-09"))

write("day1/olink/olink_week1.csv", OLINK, proteomics({
    "S100": [["S100", "CRP", "P02741", "10.4", "PASS", "2026-03-05"], ["S100", "TNF", "P01375", "3.4", "PASS", "2026-03-05"]],
    "S101": [["S101", "CRP", "P02741", "8.9", "PASS", "2026-03-05"], ["S101", "TNF", "P01375", "3.1", "WARN", "2026-03-05"]],
    "S102": [["S102", "CRP", "P02741", "9.7", "PASS", "2026-03-05"], ["S102", "TNF", "P01375", "2.8", "PASS", "2026-03-05"]],
    "S103": [["S103", "CRP", "P02741", "7.5", "PASS", "2026-03-05"], ["S103", "TNF", "P01375", "3.0", "PASS", "2026-03-05"]],
}, COHORT_DAY1 + COHORT_DAY2, "2026-03-05"))
write("day7/olink/olink_week2.csv", OLINK, proteomics({
    "S104": [["S104", "CRP", "P02741", "9.1", "PASS", "2026-03-12"], ["S104", "TNF", "P01375", "3.2", "PASS", "2026-03-12"]],
    "S105": [["S105", "CRP", "P02741", "8.2", "PASS", "2026-03-12"], ["S105", "TNF", "P01375", "2.9", "PASS", "2026-03-12"]],
}, COHORT_WEEK2, "2026-03-12"))

(WEEK / "onboarding/registry_cohort.csv").write_text("".join(
    f"G-{1000 + int(s[2:])},lab_a,{participant(s)}\n" for s in COHORT_DAY1 + COHORT_DAY2 + COHORT_WEEK2))
(WEEK / "onboarding/protein_panel_analytes.csv").write_text("".join(
    f"{name},Synthetic protein {name},{uniprot}\n" for name, uniprot in SYNTHETIC))
(WEEK / "onboarding/protein_panel_aliases.csv").write_text("".join(
    f"olink,{uniprot},{name},olink_npx,relative_log2,NPX\n" for name, uniprot in SYNTHETIC))
print("week-1 proteomics rows:", 4 * 50 + len(COHORT_DAY1 + COHORT_DAY2) * 50,
      "| week-2:", 2 * 50 + len(COHORT_WEEK2) * 50)
