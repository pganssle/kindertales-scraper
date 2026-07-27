"""Tests for release artifact validation."""

from __future__ import annotations

import io
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

from scripts import check_release

if TYPE_CHECKING:
    from pathlib import Path


def test_project_and_artifact_versions_agree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Matching source, wheel, sdist, and tag versions pass validation."""
    _write_project(tmp_path, "1.2.3", "1.2.3")
    distributions = tmp_path / "dist"
    _write_distributions(distributions, "1.2.3", "1.2.3")

    assert (
        check_release.main(
            ("--root", str(tmp_path), "--dist", str(distributions), "--tag", "1.2.3")
        )
        == 0
    )
    assert capsys.readouterr().out == "1.2.3\n"


@pytest.mark.parametrize(
    "case",
    [
        ("1.2.3", "1.2.4", "1.2.3", "1.2.3", "1.2.3", "package version"),
        ("1.2.3", "1.2.3", "1.2.4", "1.2.3", "1.2.3", "filename version"),
        ("1.2.3", "1.2.3", "1.2.3", "1.2.4", "1.2.3", "metadata version"),
        ("1.2.3", "1.2.3", "1.2.3", "1.2.3", "1.2.4", "release tag"),
    ],
)
def test_rejects_inconsistent_release_versions(
    tmp_path: Path,
    case: tuple[str, str, str, str, str, str],
) -> None:
    """Every source, artifact, and tag version must agree."""
    (
        project_version,
        package_version,
        filename_version,
        metadata_version,
        tag,
        expected,
    ) = case
    _write_project(tmp_path, project_version, package_version)
    distributions = tmp_path / "dist"
    _write_distributions(distributions, filename_version, metadata_version)

    with pytest.raises(check_release.ReleaseError, match=expected):
        check_release.check_release(tmp_path, distributions, tag)


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_requires_both_distribution_types(tmp_path: Path, kind: str) -> None:
    """A release cannot omit either the wheel or the source distribution."""
    distributions = tmp_path / "dist"
    _write_distributions(distributions, "1.2.3", "1.2.3")
    suffix = ".whl" if kind == "wheel" else ".tar.gz"
    next(distributions.glob(f"*{suffix}")).unlink()

    with pytest.raises(check_release.ReleaseError, match=f"missing.*{kind}"):
        check_release.check_artifacts(distributions, "1.2.3")


def _write_project(root: Path, project_version: str, package_version: str) -> None:
    package = root / "src" / "kindertales_scraper"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "kindertales-scraper"\nversion = "{project_version}"\n'
    )
    (package / "__init__.py").write_text(f'__version__ = "{package_version}"\n')


def _write_distributions(
    directory: Path, filename_version: str, metadata_version: str
) -> None:
    directory.mkdir()
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: kindertales-scraper\n"
        f"Version: {metadata_version}\n\n"
    ).encode()
    wheel = directory / (
        f"kindertales_scraper-{filename_version}-py3-none-any.whl"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"kindertales_scraper-{filename_version}.dist-info/METADATA", metadata
        )

    sdist = directory / f"kindertales_scraper-{filename_version}.tar.gz"
    member = tarfile.TarInfo(
        f"kindertales_scraper-{filename_version}/PKG-INFO"
    )
    member.size = len(metadata)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(metadata))
