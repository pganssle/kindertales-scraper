# /// script
# requires-python = ">=3.13"
# dependencies = [
#   "packaging>=25",
# ]
# ///
"""Check that project, artifact, and release-tag versions agree."""

from __future__ import annotations

import argparse
import ast
import email.message
import email.parser
import email.policy
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from packaging import utils

if TYPE_CHECKING:
    from collections.abc import Sequence


class ReleaseError(ValueError):
    """Report an inconsistent release version or artifact."""


def project_version(root: Path) -> str:
    """Return the version shared by project metadata and the package."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    metadata_version = pyproject["project"]["version"]
    package_path = root / "src" / "kindertales_scraper" / "__init__.py"
    package_version = _assigned_version(package_path)
    if package_version != metadata_version:
        message = (
            f"package version {package_version!r} does not match "
            f"project version {metadata_version!r}"
        )
        raise ReleaseError(message)
    return metadata_version


def check_artifacts(directory: Path, expected_version: str) -> None:
    """Check wheel and sdist filenames and embedded metadata."""
    artifacts = tuple(
        sorted(
            path
            for path in directory.iterdir()
            if path.suffix == ".whl"
            or path.name.endswith(".tar.gz")
        )
    )
    if not artifacts:
        message = f"no distributions found in {directory}"
        raise ReleaseError(message)

    kinds: set[str] = set()
    expected_name = utils.canonicalize_name("kindertales-scraper")
    for artifact in artifacts:
        filename_name, filename_version, kind = _filename_identity(artifact)
        kinds.add(kind)
        metadata = _artifact_metadata(artifact, kind)
        metadata_name = utils.canonicalize_name(_required_header(metadata, "Name"))
        metadata_version = _required_header(metadata, "Version")
        if filename_name != expected_name or metadata_name != expected_name:
            message = f"unexpected distribution name in {artifact.name}"
            raise ReleaseError(message)
        if filename_version != expected_version:
            message = (
                f"artifact filename version {filename_version!r} does not match "
                f"project version {expected_version!r}: {artifact.name}"
            )
            raise ReleaseError(message)
        if metadata_version != expected_version:
            message = (
                f"artifact metadata version {metadata_version!r} does not match "
                f"project version {expected_version!r}: {artifact.name}"
            )
            raise ReleaseError(message)

    missing = {"wheel", "sdist"} - kinds
    if missing:
        message = f"missing distribution type: {', '.join(sorted(missing))}"
        raise ReleaseError(message)


def check_release(root: Path, directory: Path | None, tag: str | None) -> str:
    """Validate the project and optional artifacts and tag."""
    version = project_version(root)
    if directory is not None:
        check_artifacts(directory, version)
    if tag is not None and tag != version:
        message = f"release tag {tag!r} does not match project version {version!r}"
        raise ReleaseError(message)
    return version


def main(argv: Sequence[str] | None = None) -> int:
    """Run release validation from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--tag")
    arguments = parser.parse_args(argv)
    try:
        version = check_release(arguments.root, arguments.dist, arguments.tag)
    except (OSError, ReleaseError, KeyError, ValueError) as error:
        parser.error(str(error))
    print(version)
    return 0


def _assigned_version(path: Path) -> str:
    tree = ast.parse(path.read_text(), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and isinstance(
            statement.value.value, str
        ):
            return statement.value.value
    message = f"could not find a static __version__ assignment in {path}"
    raise ReleaseError(message)


def _filename_identity(path: Path) -> tuple[str, str, str]:
    try:
        if path.suffix == ".whl":
            name, version, _build, _tags = utils.parse_wheel_filename(path.name)
            return utils.canonicalize_name(name), str(version), "wheel"
        name, version = utils.parse_sdist_filename(path.name)
        return utils.canonicalize_name(name), str(version), "sdist"
    except utils.InvalidSdistFilename as error:
        raise ReleaseError(str(error)) from error
    except utils.InvalidWheelFilename as error:
        raise ReleaseError(str(error)) from error


def _artifact_metadata(path: Path, kind: str) -> email.message.Message:
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            candidates = tuple(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            )
            data = archive.read(_one_metadata_path(path, candidates))
    else:
        with tarfile.open(path, "r:*") as archive:
            candidates = tuple(
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith("/PKG-INFO")
            )
            member = _one_metadata_path(path, candidates)
            stream = archive.extractfile(member)
            if stream is None:  # pragma: no cover
                message = f"could not read metadata from {path.name}"
                raise ReleaseError(message)
            data = stream.read()
    return email.parser.BytesParser(policy=email.policy.default).parsebytes(data)


def _one_metadata_path[T](artifact: Path, candidates: Sequence[T]) -> T:
    if len(candidates) != 1:
        message = f"expected exactly one metadata file in {artifact.name}"
        raise ReleaseError(message)
    return candidates[0]


def _required_header(metadata: email.message.Message, name: str) -> str:
    value = metadata.get(name)
    if value is None:
        message = f"artifact metadata is missing {name!r}"
        raise ReleaseError(message)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
