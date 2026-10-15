"""Loading of source contracts, reference data and the enrollment registry (DATA_MODEL.md §6).

Everything here is versioned input: each run records a hash of the reference data and registry so
any silver row can be traced to the exact configuration that produced it.
"""

import hashlib
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

CONFIG_DIR_ENV = "PIPELINE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path("config")

# Canonical fields a contract may map. `observed_at` and `assay_run_date` are parsed as timestamps.
CANONICAL_FIELDS = (
    "source_subject_id",
    "source_sample_id",
    "source_encounter_id",
    "source_analyte_code",
    "source_analyte_name",
    "source_value",
    "source_unit",
    "observed_at",
    "result_status",
    "qc_flag",
    "assay_run_date",
)
TIME_FIELDS = ("observed_at", "assay_run_date")

REFERENCE_FILES = (
    "analytes.csv",
    "analyte_aliases.csv",
    "unit_conversions.csv",
    "unit_aliases.csv",
    "value_ranges.csv",
)
REGISTRY_FILE = Path("registry") / "enrollment.csv"


class ConfigError(ValueError):
    """Raised when a contract or reference file is malformed."""


@dataclass(frozen=True)
class FieldSpec:
    column: str
    type: str = "string"
    format: str | None = None
    required: bool = True
    default: str | None = None


@dataclass(frozen=True)
class Contract:
    source: str
    version: int
    domain: str
    format: str
    layout: str
    sample_namespace: str | None
    timezone: str | None
    fields: dict[str, FieldSpec]
    constants: dict[str, str] = field(default_factory=dict)
    natural_key: tuple[str, ...] = ()
    observed_at_from: str | None = None
    control_sample_pattern: str | None = None
    # Schema-drift decisions (README "Schema change workflow"): columns deliberately not used, and
    # columns that must never be received (files containing them are quarantined).
    ignored_columns: tuple[str, ...] = ()
    forbidden_columns: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.source}_v{self.version}"

    @property
    def mapped_columns(self) -> set[str]:
        return {spec.column for spec in self.fields.values()}

    @property
    def required_columns(self) -> set[str]:
        return {spec.column for spec in self.fields.values() if spec.required}

    def column_status(self, column: str) -> str:
        """How this contract treats a column: ADOPTED (mapped), IGNORED, FORBIDDEN, or OPEN (undecided)."""
        if column in self.mapped_columns:
            return "ADOPTED"
        if column in self.ignored_columns:
            return "IGNORED"
        if column in self.forbidden_columns:
            return "FORBIDDEN"
        return "OPEN"

    def matches(self, columns: list[str]) -> bool:
        """A file matches when every required column is present; extra columns are ignored."""
        return self.required_columns <= set(columns)


def resolve_config_dir(config_dir: Path | None = None) -> Path:
    if config_dir is not None:
        return config_dir
    from_env = os.environ.get(CONFIG_DIR_ENV)
    return Path(from_env) if from_env else DEFAULT_CONFIG_DIR


def _field_spec(name: str, raw: str | dict) -> FieldSpec:
    if isinstance(raw, str):
        spec = FieldSpec(column=raw)
    else:
        spec = FieldSpec(
            column=raw["column"],
            type=raw.get("type", "string"),
            format=raw.get("format"),
            required=raw.get("required", True),
            default=None if raw.get("default") is None else str(raw["default"]),
        )
    if name in TIME_FIELDS and spec.format is None:
        raise ConfigError(f"Field '{name}' needs a 'format'")
    return spec


def parse_contract(path: Path) -> Contract:
    raw = yaml.safe_load(path.read_text())
    unknown = set(raw["fields"]) - set(CANONICAL_FIELDS)
    if unknown:
        raise ConfigError(f"{path.name}: unknown canonical fields {sorted(unknown)}")
    fields = {name: _field_spec(name, spec) for name, spec in raw["fields"].items()}
    natural_key = tuple(raw.get("natural_key", ()))
    missing_key = set(natural_key) - set(fields)
    if missing_key:
        raise ConfigError(f"{path.name}: natural key uses unmapped fields {sorted(missing_key)}")
    ignored = tuple(raw.get("ignored_columns") or ())
    forbidden = tuple(raw.get("forbidden_columns") or ())
    mapped = {spec.column for spec in fields.values()}
    conflicts = (set(ignored) & set(forbidden)) | ((set(ignored) | set(forbidden)) & mapped)
    if conflicts:
        raise ConfigError(f"{path.name}: columns {sorted(conflicts)} are listed as more than one of mapped / ignored / forbidden")
    return Contract(
        source=raw["source"],
        version=int(raw["contract_version"]),
        domain=raw["domain"],
        format=raw.get("format", "csv"),
        layout=raw.get("layout", "long"),
        sample_namespace=raw.get("sample_namespace"),
        timezone=raw.get("timezone"),
        fields=fields,
        constants={k: str(v) for k, v in (raw.get("constants") or {}).items()},
        natural_key=natural_key,
        observed_at_from=raw.get("observed_at_from"),
        control_sample_pattern=raw.get("control_sample_pattern"),
        ignored_columns=ignored,
        forbidden_columns=forbidden,
    )


def check_config_dir(config_dir: Path) -> None:
    """Fail fast with a clear message when the config folder is missing or incomplete."""
    if not config_dir.is_dir():
        raise ConfigError(f"config folder '{config_dir}' not found (set --config-dir or $PIPELINE_CONFIG_DIR)")
    required = [config_dir / "sources", *(config_dir / "reference" / n for n in REFERENCE_FILES), config_dir / REGISTRY_FILE]
    missing = [str(p.relative_to(config_dir)) for p in required if not p.exists()]
    if missing:
        raise ConfigError(f"config folder '{config_dir}' is incomplete; missing: {missing}")
    if not any((config_dir / "sources").glob("*.yaml")):
        raise ConfigError(f"config folder '{config_dir}' has no source contracts in sources/")


def _carry_decisions_forward(versions: list[Contract]) -> list[Contract]:
    """Schema-drift decisions apply to every version of a source, so a new version cannot drop them.

    - forbidden: the union over all versions; mapping a forbidden column is a configuration error.
      Lifting a ban requires removing it from every version — never by omission.
    - ignored: the union over all versions, except columns a version maps (that is adoption).
    """
    forbidden = {c for v in versions for c in v.forbidden_columns}
    ignored = {c for v in versions for c in v.ignored_columns} - forbidden
    result = []
    for contract in versions:
        clash = forbidden & contract.mapped_columns
        if clash:
            raise ConfigError(f"{contract.name} maps {sorted(clash)}, which a version of {contract.source} forbids")
        result.append(replace(
            contract,
            forbidden_columns=tuple(sorted(forbidden)),
            ignored_columns=tuple(sorted(ignored - contract.mapped_columns)),
        ))
    return result


def load_contracts(config_dir: Path) -> dict[str, list[Contract]]:
    """All contracts per source, newest version first, with drift decisions carried across versions."""
    check_config_dir(config_dir)
    contracts: dict[str, list[Contract]] = {}
    for path in sorted((config_dir / "sources").glob("*.yaml")):
        contract = parse_contract(path)
        contracts.setdefault(contract.source, []).append(contract)
    for source, versions in contracts.items():
        versions.sort(key=lambda c: c.version, reverse=True)
        contracts[source] = _carry_decisions_forward(versions)
    return contracts


def select_contract(candidates: list[Contract], columns: list[str]) -> Contract | None:
    """Newest contract version whose required columns are all present in the file."""
    return next((c for c in candidates if c.matches(columns)), None)


def files_version(paths: list[Path]) -> str:
    """Short content hash identifying a set of configuration files."""
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@dataclass(frozen=True)
class ConfigVersions:
    reference: str
    registry: str


def config_versions(config_dir: Path) -> ConfigVersions:
    return ConfigVersions(
        reference=files_version([config_dir / "reference" / name for name in REFERENCE_FILES]),
        registry=files_version([config_dir / REGISTRY_FILE]),
    )
