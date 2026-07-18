"""Typed configuration for kindertales-scraper."""

import datetime as dt
import enum
import math
import string
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import attrs

_MIN_LATITUDE = -90
_MAX_LATITUDE = 90
_MIN_LONGITUDE = -180
_MAX_LONGITUDE = 180


class ConfigError(ValueError):
    """Raised when a configuration file is invalid."""


class FolderFrequency(enum.StrEnum):
    """Calendar grouping applied below the archive media directory."""

    NONE = "none"
    DAILY = "daily"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class SidecarLayout(enum.StrEnum):
    """Location of JSON sidecars relative to archived media."""

    ADJACENT = "adjacent"
    PARALLEL = "parallel"


@attrs.frozen
class ArchiveLayout:
    """Naming and folder layout for archived media and sidecars."""

    folder_frequency: FolderFrequency = attrs.field(
        default=FolderFrequency.NONE,
        converter=FolderFrequency,
    )
    folder_format: str = ""
    filename_format: str = (
        "{timestamp:%Y%m%d_%H%M%S}_{sequence:02d}{extension}"
    )
    sidecar_layout: SidecarLayout = attrs.field(
        default=SidecarLayout.ADJACENT,
        converter=SidecarLayout,
    )

    def __attrs_post_init__(self) -> None:
        """Validate the restricted Python-style filename template."""
        allowed = frozenset(
            {
                "activity_id",
                "activity_type",
                "child_id",
                "child_name",
                "extension",
                "media_id",
                "original_name",
                "original_stem",
                "sequence",
                "timestamp",
            }
        )
        sample = {
            field: (
                dt.datetime(2000, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
                if field == "timestamp"
                else 1
                if field == "sequence"
                else ".jpg"
                if field == "extension"
                else "value"
            )
            for field in allowed
        }

        def validate(
            setting: str,
            template: str,
            required: frozenset[str] = frozenset(),
        ) -> None:
            try:
                fields = frozenset(
                    field_name
                    for _, field_name, _, _ in string.Formatter().parse(template)
                    if field_name is not None
                )
            except ValueError as error:
                msg = f"archive.{setting} is not a valid format string"
                raise ConfigError(msg) from error
            unknown = fields - allowed
            if unknown:
                msg = f"archive.{setting} has unknown fields: {sorted(unknown)}"
                raise ConfigError(msg)
            missing = required - fields
            if missing:
                msg = f"archive.{setting} is missing fields: {sorted(missing)}"
                raise ConfigError(msg)
            try:
                template.format_map(sample)
            except (KeyError, ValueError) as error:
                msg = f"archive.{setting} has an invalid format specification"
                raise ConfigError(msg) from error

        validate("folder_format", self.folder_format)
        validate(
            "filename_format",
            self.filename_format,
            frozenset({"extension", "sequence"}),
        )


@attrs.frozen
class Quota:
    """A rolling request quota."""

    count: int
    window_seconds: float

    def __attrs_post_init__(self) -> None:
        """Validate positive quota values."""
        if self.count <= 0 or not math.isfinite(self.window_seconds):
            msg = "quota count and window must be positive"
            raise ConfigError(msg)
        if self.window_seconds <= 0:
            msg = "quota count and window must be positive"
            raise ConfigError(msg)


@attrs.frozen
class RequestPolicy:
    """Limits governing requests to Kindertales."""

    quotas: tuple[Quota, ...] = (
        Quota(8, 1.0),
        Quota(120, 60.0),
    )
    max_in_flight: int = 8
    max_media_downloads: int = 2
    jitter_fraction: float = 0.10
    max_retries: int = 3
    stop_after_forbidden: int = 3

    def __attrs_post_init__(self) -> None:
        """Validate request limits."""
        if not self.quotas:
            msg = "at least one request quota is required"
            raise ConfigError(msg)
        if not 0.0 <= self.jitter_fraction < 1.0:
            msg = "jitter_fraction must satisfy 0.0 <= value < 1.0"
            raise ConfigError(msg)
        values = (
            self.max_in_flight,
            self.max_media_downloads,
            self.max_retries + 1,
            self.stop_after_forbidden,
        )
        if any(value <= 0 for value in values):
            msg = "request limits must be positive (max_retries may be zero)"
            raise ConfigError(msg)


@attrs.frozen
class Coordinates:
    """A geographic coordinate pair."""

    latitude: float
    longitude: float

    def __attrs_post_init__(self) -> None:
        """Validate coordinate ranges."""
        latitude_valid = _MIN_LATITUDE <= self.latitude <= _MAX_LATITUDE
        longitude_valid = _MIN_LONGITUDE <= self.longitude <= _MAX_LONGITUDE
        if not latitude_valid or not longitude_valid:
            msg = "coordinates are outside the valid latitude/longitude range"
            raise ConfigError(msg)


@attrs.frozen
class Center:
    """Center-specific metadata defaults."""

    coordinates: Coordinates | None = None
    timezone: str | None = None


@attrs.frozen
class Exports:
    """Optional non-media account areas included in synchronization."""

    child_records: bool = True
    messages: bool = False
    billing: bool = False


@attrs.frozen
class Config:
    """Complete application configuration."""

    email: str
    cache_directory: Path = Path(".cache/kindertales-scraper")
    allow_plaintext_session_cache: bool = False
    archive_directory: Path = Path("archive")
    archive_layout: ArchiveLayout = ArchiveLayout()
    overlap_days: int = 7
    request_policy: RequestPolicy = RequestPolicy()
    centers: Mapping[str, Center] = attrs.field(factory=dict)
    fallback_coordinates: Coordinates | None = None
    exports: Exports = Exports()
    child_names: Mapping[str, str] = attrs.field(factory=dict)
    use_kindertales_name: bool = False
    source_path: Path | None = attrs.field(default=None, eq=False)

    def __attrs_post_init__(self) -> None:
        """Validate account and synchronization settings."""
        if "@" not in self.email:
            msg = "account.email must be an email address"
            raise ConfigError(msg)
        if self.overlap_days < 0:
            msg = "synchronization.overlap_days cannot be negative"
            raise ConfigError(msg)


def _table(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        msg = f"{name} must be a TOML table"
        raise ConfigError(msg)
    return value


def _coordinates(data: Mapping[str, Any], prefix: str) -> Coordinates | None:
    latitude = data.get(f"{prefix}latitude")
    longitude = data.get(f"{prefix}longitude")
    if latitude is None and longitude is None:
        return None
    if not isinstance(latitude, int | float) or not isinstance(longitude, int | float):
        msg = f"{prefix}latitude and {prefix}longitude must both be numbers"
        raise ConfigError(msg)
    return Coordinates(float(latitude), float(longitude))


def load(path: Path) -> Config:
    """Load and validate a TOML configuration file."""
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    account = _table(data, "account")
    authentication = _table(data, "authentication")
    archive = _table(data, "archive")
    synchronization = _table(data, "synchronization")
    policy_data = _table(data, "request_policy")
    metadata = _table(data, "metadata")
    exports = _table(data, "exports")
    children = _table(data, "children")

    raw_quotas = policy_data.get(
        "quotas",
        ({"count": 8, "window_seconds": 1.0}, {"count": 120, "window_seconds": 60.0}),
    )
    if not isinstance(raw_quotas, list | tuple):
        msg = "request_policy.quotas must be an array"
        raise ConfigError(msg)
    quotas = tuple(
        Quota(int(item["count"]), float(item["window_seconds"]))
        for item in raw_quotas
        if isinstance(item, dict)
    )
    if len(quotas) != len(raw_quotas):
        msg = "each quota must be a table"
        raise ConfigError(msg)

    raw_centers = metadata.get("centers", {})
    if not isinstance(raw_centers, dict):
        msg = "metadata.centers must be a table"
        raise ConfigError(msg)
    centers = {
        str(center_id): Center(
            coordinates=_coordinates(center_data, ""),
            timezone=str(center_data["timezone"])
            if "timezone" in center_data
            else None,
        )
        for center_id, center_data in raw_centers.items()
        if isinstance(center_data, dict)
    }
    if len(centers) != len(raw_centers):
        msg = "each center must be a table"
        raise ConfigError(msg)

    email = account.get("email")
    if not isinstance(email, str):
        msg = "account.email is required"
        raise ConfigError(msg)
    raw_child_names = children.get("names", {})
    if not isinstance(raw_child_names, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in raw_child_names.items()
    ):
        msg = "children.names must map Kindertales names to preferred names"
        raise ConfigError(msg)
    try:
        folder_frequency = FolderFrequency(
            str(archive.get("folder_frequency", FolderFrequency.NONE))
        )
    except ValueError as error:
        msg = "archive.folder_frequency must be none, daily, monthly, or yearly"
        raise ConfigError(msg) from error
    try:
        sidecar_layout = SidecarLayout(
            str(archive.get("sidecar_layout", SidecarLayout.ADJACENT))
        )
    except ValueError as error:
        msg = "archive.sidecar_layout must be adjacent or parallel"
        raise ConfigError(msg) from error
    return Config(
        email=email,
        cache_directory=Path(
            str(authentication.get("cache_directory", ".cache/kindertales-scraper"))
        ),
        allow_plaintext_session_cache=bool(
            authentication.get("allow_plaintext_session_cache", False)
        ),
        archive_directory=Path(str(archive.get("directory", "archive"))),
        archive_layout=ArchiveLayout(
            folder_frequency=folder_frequency,
            folder_format=str(archive.get("folder_format", "")),
            filename_format=str(
                archive.get(
                    "filename_format",
                    "{timestamp:%Y%m%d_%H%M%S}_{sequence:02d}{extension}",
                )
            ),
            sidecar_layout=sidecar_layout,
        ),
        overlap_days=int(synchronization.get("overlap_days", 7)),
        request_policy=RequestPolicy(
            quotas=quotas,
            max_in_flight=int(policy_data.get("max_in_flight", 8)),
            max_media_downloads=int(policy_data.get("max_media_downloads", 2)),
            jitter_fraction=float(policy_data.get("jitter_fraction", 0.10)),
            max_retries=int(policy_data.get("max_retries", 3)),
            stop_after_forbidden=int(policy_data.get("stop_after_forbidden", 3)),
        ),
        centers=centers,
        fallback_coordinates=_coordinates(metadata, "fallback_"),
        exports=Exports(
            child_records=bool(exports.get("child_records", True)),
            messages=bool(exports.get("messages", False)),
            billing=bool(exports.get("billing", False)),
        ),
        child_names=dict(raw_child_names),
        use_kindertales_name=bool(children.get("use_kindertales_name", False)),
        source_path=path,
    )


def write_initial(path: Path, email: str) -> None:
    """Write a private initial configuration file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = (
        f'[account]\nemail = "{email.replace(chr(34), chr(92) + chr(34))}"\n\n'
        "[authentication]\nallow_plaintext_session_cache = false\n"
        'cache_directory = ".cache/kindertales-scraper"\n\n'
        '[archive]\ndirectory = "archive"\n'
        'folder_format = ""\n'
        'folder_frequency = "none"\n'
        'filename_format = "{timestamp:%Y%m%d_%H%M%S}_{sequence:02d}{extension}"\n'
        'sidecar_layout = "adjacent"\n'
        "\n[exports]\nchild_records = true\nmessages = false\nbilling = false\n"
        "\n[children]\nuse_kindertales_name = false\n"
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
