"""Interactive configuration of linked Kindertales center metadata."""

import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import TextIO

import attrs
import httpx
import tomlkit

from . import auth, config, credentials, discovery, names, scheduler

Input = Callable[[str], str]


class CenterSetupError(RuntimeError):
    """Raised when linked centers cannot be configured."""


def _table(
    parent: MutableMapping[str, object],
    name: str,
) -> MutableMapping[str, object]:
    value = parent.get(name)
    if isinstance(value, MutableMapping):
        return value
    created = tomlkit.table()
    parent[name] = created
    return created


def _write_center(
    table: MutableMapping[str, object],
    center: config.Center,
) -> None:
    if center.coordinates is not None:
        table["latitude"] = center.coordinates.latitude
        table["longitude"] = center.coordinates.longitude
    if center.timezone is not None:
        table["timezone"] = center.timezone
    if center.gps_uncertainty_meters is not None:
        table["gps_uncertainty_meters"] = center.gps_uncertainty_meters


def update_config(
    path: Path,
    default_center: config.Center,
    centers: Mapping[str, config.Center],
) -> None:
    """Update center tables while retaining unrelated TOML formatting."""
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    metadata = _table(document, "metadata")
    defaults = _table(metadata, "defaults")
    default_table = _table(defaults, "center")
    _write_center(default_table, default_center)
    center_tables = _table(metadata, "centers")
    for center_id, center in centers.items():
        _write_center(_table(center_tables, center_id), center)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.chmod(path.stat().st_mode)
    temporary.replace(path)


@attrs.frozen
class InteractiveSetup:
    """Prompt for default and center-specific metadata."""

    settings: config.Config
    input_fn: Input = input
    output: TextIO = sys.stdout

    def configure(self, children: Sequence[discovery.Child]) -> None:
        """List linked centers, collect values, and update the source TOML."""
        linked: dict[str, list[str]] = defaultdict(list)
        for child in children:
            if child.center_id is not None:
                linked[child.center_id].append(child.name)
        if not linked:
            msg = "Kindertales did not expose a center ID for any linked child"
            raise CenterSetupError(msg)
        if self.settings.source_path is None:
            msg = "center setup requires configuration loaded from a file"
            raise CenterSetupError(msg)
        for center_id, child_names in linked.items():
            print(
                f"Center {center_id}: {', '.join(child_names)}",
                file=self.output,
            )
        default_center = self._prompt_center(
            "Default center",
            self.settings.default_center,
        )
        centers = {
            center_id: self._prompt_center(
                f"Center {center_id}",
                self.settings.centers.get(center_id, config.Center()),
            )
            for center_id in linked
        }
        update_config(self.settings.source_path, default_center, centers)

    def _prompt_center(self, label: str, current: config.Center) -> config.Center:
        print(label, file=self.output)
        coordinates = self._coordinates(current.coordinates)
        timezone = self.input_fn(
            f"  Timezone [{current.timezone or 'inherit'}]: "
        ).strip() or current.timezone
        uncertainty_default = (
            current.gps_uncertainty_meters
            if current.gps_uncertainty_meters is not None
            else "inherit"
        )
        uncertainty_text = self.input_fn(
            f"  GPS uncertainty in meters [{uncertainty_default}]: "
        ).strip()
        uncertainty = (
            float(uncertainty_text)
            if uncertainty_text
            else current.gps_uncertainty_meters
        )
        return config.Center(coordinates, timezone, uncertainty)

    def _coordinates(
        self,
        current: config.Coordinates | None,
    ) -> config.Coordinates | None:
        while True:
            latitude = self.input_fn(
                f"  Latitude [{current.latitude if current else 'inherit'}]: "
            ).strip()
            longitude = self.input_fn(
                f"  Longitude [{current.longitude if current else 'inherit'}]: "
            ).strip()
            if not latitude and not longitude:
                return current
            if latitude and longitude:
                return config.Coordinates(float(latitude), float(longitude))
            print(
                "  Latitude and longitude must be specified together.",
                file=self.output,
            )


def _cookies(state: auth.State) -> httpx.Cookies:  # pragma: no cover
    cookies = httpx.Cookies()
    raw_cookies = state.get("cookies", ())
    if not isinstance(raw_cookies, Sequence):
        return cookies
    for item in raw_cookies:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        ):
            cookies.set(
                item["name"],
                item["value"],
                domain=str(item.get("domain", "app.kindertales.com")),
                path=str(item.get("path", "/")),
            )
    return cookies


async def run_configured(  # pragma: no cover - authorized interactive boundary
    settings: config.Config,
    *,
    headed: bool,
) -> None:
    """Authenticate, discover linked centers, and run interactive setup."""
    password, _persistent = credentials.password(settings.email)
    login = auth.PlaywrightLogin()
    manager = auth.SessionManager(auth.SessionCache(settings))

    async def validate(state: auth.State) -> bool:
        async with httpx.AsyncClient(
            base_url="https://app.kindertales.com",
            cookies=_cookies(state),
            follow_redirects=False,
        ) as client:
            response = await client.get("/index.php?pg=dashboard")
            return response.status_code == httpx.codes.OK and not response.is_redirect

    async def authenticate() -> auth.State:
        return await login.authenticate(settings.email, password, headed=headed)

    state = await manager.state(validate, authenticate)
    async with httpx.AsyncClient(
        base_url="https://app.kindertales.com",
        cookies=_cookies(state),
        follow_redirects=False,
    ) as client:
        limiter = scheduler.RollingLimiter(settings.request_policy)
        requester = scheduler.Requester(settings.request_policy, limiter)
        adapter = discovery.LegacyKindertalesAdapter(client, requester=requester)
        children = names.InteractiveResolver(settings).resolve(
            await adapter.children()
        )
        InteractiveSetup(settings).configure(children)
