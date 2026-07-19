"""ExifTool metadata capture and non-destructive enrichment."""

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import attrs

from . import config, discovery

Runner = Callable[..., subprocess.CompletedProcess[str]]


class MetadataError(RuntimeError):
    """Raised when metadata cannot be read or enriched."""


@attrs.frozen
class Enrichment:
    """Original metadata and fields selected for embedding."""

    original: Mapping[str, Any]
    embedded_fields: Mapping[str, str]
    inferred_time: bool
    inferred_gps: bool
    sidecar_metadata: Mapping[str, Any] = attrs.field(factory=dict)


_HOST_METADATA_NAMES = frozenset(
    {
        "Directory",
        "FileAccessDate",
        "FileInodeChangeDate",
        "FileModifyDate",
        "FileName",
        "FilePermissions",
        "SourceFile",
    }
)


def portable_original(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Remove values derived from the local download path and filesystem."""
    return {
        key: value
        for key, value in metadata.items()
        if _tag_name(key) not in _HOST_METADATA_NAMES
    }


def _qualified_tag(value: str) -> str:
    if value.startswith("[") and "]" in value:
        group, name = value[1:].split("]", 1)
        return f"{group}:{name}".casefold()
    return value.casefold()


def sidecar_metadata(
    original: Mapping[str, Any],
    embedded_fields: Mapping[str, str],
) -> Mapping[str, Any]:
    """Return original metadata only when enrichment overwrites a real tag."""
    targets = {_qualified_tag(name): value for name, value in embedded_fields.items()}
    conflict = any(
        (expected := targets.get(_qualified_tag(name))) is not None
        and str(value) != str(expected)
        for name, value in original.items()
        if _tag_name(name) not in _HOST_METADATA_NAMES
    )
    return portable_original(original) if conflict else {}


def confirmed_fields(
    requested: Mapping[str, str],
    actual: Mapping[str, Any],
) -> Mapping[str, str]:
    """Return requested fields that ExifTool confirms the container retained."""
    actual_tags = frozenset(_qualified_tag(name) for name in actual)
    return {
        name: value
        for name, value in requested.items()
        if _qualified_tag(name) in actual_tags
    }


def _has(metadata: Mapping[str, Any], names: frozenset[str]) -> bool:
    return any(_tag_name(key) in names for key in metadata)


def _tag_name(value: str) -> str:
    """Remove ExifTool's bracketed or colon-separated group prefix."""
    return value.rsplit("]", 1)[-1].rsplit(":", 1)[-1]


def _iptc_text(value: str) -> str:
    """Return the value ExifTool can represent in legacy IPTC text fields."""
    return value.encode("latin-1", errors="replace").decode("latin-1")


def fields_for(
    original: Mapping[str, Any],
    child: discovery.Child,
    activity: discovery.Activity,
    settings: config.Config,
) -> tuple[dict[str, str], bool, bool]:
    """Choose non-conflicting EXIF/IPTC/XMP fields and provenance markers."""
    fields: dict[str, str] = {}
    has_time = _has(
        original,
        frozenset({"DateTimeOriginal", "CreateDate", "DateCreated"}),
    )
    inferred_time = not has_time
    if inferred_time:
        fields["EXIF:DateTimeOriginal"] = activity.occurred_at.strftime(
            "%Y:%m:%d %H:%M:%S"
        )
        offset = activity.occurred_at.strftime("%z")
        fields["EXIF:OffsetTimeOriginal"] = f"{offset[:3]}:{offset[3:]}"
        fields["XMP-photoshop:DateCreated"] = activity.occurred_at.isoformat()

    has_gps = _has(
        original,
        frozenset({"GPSLatitude", "GPSLongitude", "GPSPosition"}),
    )
    center = settings.center(activity.center_id)
    coordinates = center.coordinates
    inferred_gps = not has_gps and coordinates is not None
    if inferred_gps and coordinates is not None:
        fields["EXIF:GPSLatitude"] = str(abs(coordinates.latitude))
        fields["EXIF:GPSLatitudeRef"] = (
            "N" if coordinates.latitude >= 0 else "S"
        )
        fields["EXIF:GPSLongitude"] = str(abs(coordinates.longitude))
        fields["EXIF:GPSLongitudeRef"] = (
            "E" if coordinates.longitude >= 0 else "W"
        )
        if center.gps_uncertainty_meters is not None and not _has(
            original,
            frozenset({"GPSHPositioningError"}),
        ):
            fields["EXIF:GPSHPositioningError"] = str(
                center.gps_uncertainty_meters
            )

    if activity.caption is not None and not _has(
        original,
        frozenset({"Description", "Caption-Abstract"}),
    ):
        fields["XMP-dc:Description"] = activity.caption
        fields["IPTC:Caption-Abstract"] = _iptc_text(activity.caption)
    if activity.author is not None and not _has(
        original,
        frozenset({"Artist", "Creator", "By-line"}),
    ):
        fields["XMP-dc:Creator"] = activity.author
        fields["IPTC:By-line"] = _iptc_text(activity.author)

    fields["XMP-dc:Source"] = "Kindertales"
    fields["XMP-xmp:Identifier"] = ";".join(
        (child.id, activity.id, *(medium.id for medium in activity.media))
    )
    fields["XMP-photoshop:Instructions"] = json.dumps(
        {
            "activity_type": activity.kind,
            "center_id": activity.center_id,
            "child": child.name,
            "gps_inferred": inferred_gps,
            "gps_uncertainty_meters": center.gps_uncertainty_meters
            if inferred_gps
            else None,
            "time_inferred": inferred_time,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return fields, inferred_time, inferred_gps


@attrs.frozen
class ExifTool:
    """Subprocess boundary for ExifTool JSON reads and atomic writes."""

    executable: str = "exiftool"
    runner: Runner = subprocess.run

    def read(self, path: Path) -> Mapping[str, Any]:
        """Read complete grouped numeric metadata as one JSON object."""
        try:
            result = self.runner(
                (self.executable, "-j", "-G", "-n", str(path)),
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            msg = "ExifTool could not read media metadata"
            raise MetadataError(msg) from error
        try:
            documents = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            msg = "ExifTool returned invalid JSON"
            raise MetadataError(msg) from error
        if (
            not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], dict)
        ):
            msg = "ExifTool did not return exactly one metadata object"
            raise MetadataError(msg)
        return documents[0]

    def enrich(
        self,
        path: Path,
        child: discovery.Child,
        activity: discovery.Activity,
        settings: config.Config,
    ) -> Enrichment:
        """Capture metadata, enrich a temporary copy, and atomically replace media."""
        original = self.read(path)
        fields, inferred_time, inferred_gps = fields_for(
            original,
            child,
            activity,
            settings,
        )
        temporary = path.with_suffix(path.suffix + ".enriching")
        shutil.copy2(path, temporary)
        arguments = (
            self.executable,
            "-overwrite_original",
            *(f"-{name}={value}" for name, value in fields.items()),
            str(temporary),
        )
        try:
            self.runner(
                arguments,
                check=True,
                capture_output=True,
                text=True,
            )
            embedded_fields = confirmed_fields(fields, self.read(temporary))
            temporary.replace(path)
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            MetadataError,
        ) as error:
            temporary.unlink(missing_ok=True)
            msg = "ExifTool could not enrich media metadata"
            raise MetadataError(msg) from error
        return Enrichment(
            original,
            embedded_fields,
            inferred_time,
            inferred_gps,
            sidecar_metadata(original, fields),
        )
