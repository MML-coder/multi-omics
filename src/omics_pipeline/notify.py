"""Operational events and where they go (docs/DESIGN.md §11, ERROR_CATALOG.md).

The pipeline only calls `Notifier.publish(event)`. Locally, `LocalNotifier` records events in
`meta.events` and keeps them for the run summary and exit code. In production the same events would be
published to an event bus (e.g. EventBridge) and routed to the source owner; only the notifier changes.
"""

from dataclasses import dataclass
from typing import Protocol

import duckdb

HIGH = "high"
MEDIUM = "medium"


@dataclass(frozen=True)
class Event:
    event_type: str          # FileQuarantined | SchemaDriftDetected | DistributionShift | ProcessingFailed
    severity: str            # high | medium
    code: str                # catalog code, e.g. F004, F006, B001, P001
    source: str
    file_name: str
    file_hash: str
    detail: str


class Notifier(Protocol):
    def publish(self, event: Event) -> None: ...


class LocalNotifier:
    """Stores events in meta.events (survives the run) and keeps them for the run summary."""

    def __init__(self, con: duckdb.DuckDBPyConnection, run_id: str):
        self.con = con
        self.run_id = run_id
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)
        self.con.execute(
            "INSERT INTO meta.events VALUES (?, ?, ?, ?, ?, ?, ?, ?, (now() AT TIME ZONE 'UTC'))",
            [self.run_id, event.event_type, event.severity, event.code, event.source, event.file_name,
             event.file_hash, event.detail],
        )
