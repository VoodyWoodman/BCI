from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


GITHUB_REPOSITORY = "VoodyWoodman/BCI"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases?per_page=100"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPOSITORY}/releases"
USER_AGENT = "BCI-Update-Manager/1"
MAX_RELEASES_RESPONSE = 2 * 1024 * 1024
MAX_MANIFEST_SIZE = 1024 * 1024
MAX_INSTALLER_SIZE = 300 * 1024 * 1024
ALLOWED_RELEASE_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}
VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
SAFE_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class UpdateSettings:
    channel: str = "Preview"
    auto_check: bool = True
    last_check_utc: str = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    channel: str
    latest_version: str | None = None
    update_available: bool = False
    release_url: str = GITHUB_RELEASES_PAGE
    release_notes: str = ""
    published_at: str = ""
    prerelease: bool = False
    installer_name: str = ""
    installer_sha256: str = ""
    installer_size: int = 0
    installer_download_url: str = ""
    error: str = ""

    @property
    def installer_ready(self) -> bool:
        return bool(
            self.update_available
            and self.installer_name
            and self.installer_sha256
            and self.installer_download_url
        )


class UpdateError(RuntimeError):
    """Raised when a GitHub update cannot be safely checked or downloaded."""


def _version_parts(value: str) -> tuple[int, int, int, tuple[tuple[int, Any], ...], bool]:
    match = VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError(f"Неподдерживаемый формат версии BCI: {value!r}")
    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))
    pre = match.group("pre")
    if not pre:
        return major, minor, patch, (), True
    identifiers: list[tuple[int, Any]] = []
    for item in pre.split("."):
        if item.isdigit():
            identifiers.append((0, int(item)))
        else:
            identifiers.append((1, item.lower()))
    return major, minor, patch, tuple(identifiers), False


def compare_versions(left: str, right: str) -> int:
    """Compare the BCI release versions used by the project (SemVer subset)."""
    a = _version_parts(left)
    b = _version_parts(right)
    core_a, core_b = a[:3], b[:3]
    if core_a != core_b:
        return 1 if core_a > core_b else -1
    if a[4] != b[4]:
        return 1 if a[4] else -1
    if a[4] and b[4]:
        return 0
    pre_a, pre_b = a[3], b[3]
    for item_a, item_b in zip(pre_a, pre_b):
        if item_a == item_b:
            continue
        # SemVer: numeric identifiers have lower precedence than text identifiers.
        return 1 if item_a > item_b else -1
    if len(pre_a) == len(pre_b):
        return 0
    return 1 if len(pre_a) > len(pre_b) else -1


def load_update_settings(path: str | Path, *, default_channel: str = "Preview") -> UpdateSettings:
    settings_path = Path(path)
    if not settings_path.exists():
        return UpdateSettings(channel=default_channel)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return UpdateSettings(channel=default_channel)
    if not isinstance(data, dict):
        return UpdateSettings(channel=default_channel)
    channel = str(data.get("channel", default_channel))
    if channel not in {"Stable", "Preview"}:
        channel = default_channel
    return UpdateSettings(
        channel=channel,
        auto_check=bool(data.get("auto_check", True)),
        last_check_utc=str(data.get("last_check_utc", "")),
    )


def save_update_settings(path: str | Path, settings: UpdateSettings) -> None:
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "channel": settings.channel if settings.channel in {"Stable", "Preview"} else "Preview",
        "auto_check": bool(settings.auto_check),
        "last_check_utc": settings.last_check_utc,
    }
    temp_path = settings_path.with_suffix(settings_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, settings_path)


def is_check_due(last_check_utc: str, *, now: datetime | None = None, interval_hours: int = 24) -> bool:
    if not last_check_utc:
        return True
    try:
        checked = datetime.fromisoformat(last_check_utc.replace("Z", "+00:00"))
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current - checked).total_seconds()
    return age_seconds < 0 or age_seconds >= interval_hours * 3600


def _validate_release_asset_url(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise UpdateError("Ссылка на файл обновления не относится к официальному GitHub Releases.")
    expected_prefix = f"/{GITHUB_REPOSITORY}/releases/download/"
    if not parsed.path.startswith(expected_prefix):
        raise UpdateError("Путь файла обновления не соответствует репозиторию BCI.")
    return url


def _validate_final_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise UpdateError("GitHub перенаправил загрузку на небезопасный протокол; файл отклонён.")
    if parsed.hostname == "api.github.com" and parsed.path.startswith(f"/repos/{GITHUB_REPOSITORY}/releases"):
        return
    if parsed.hostname not in ALLOWED_RELEASE_HOSTS:
        raise UpdateError("GitHub перенаправил загрузку на неожиданный адрес; файл отклонён.")


class GitHubUpdateClient:
    """Checks official GitHub Releases and verifies installer assets before launch."""

    def __init__(
        self,
        repository: str = GITHUB_REPOSITORY,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: int = 20,
    ) -> None:
        if repository != GITHUB_REPOSITORY:
            raise ValueError("BCI updates may only be fetched from the configured official repository.")
        self.repository = repository
        self.api_url = f"https://api.github.com/repos/{repository}/releases?per_page=100"
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _request_bytes(self, url: str, *, max_bytes: int, accept: str = "application/octet-stream") -> tuple[bytes, str]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout)
            with response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                _validate_final_download_url(final_url)
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise UpdateError("GitHub ограничил запросы API. Попробуй проверить обновления позже.") from exc
            if exc.code == 404:
                raise UpdateError("Запрошенный релиз или файл не найден на GitHub.") from exc
            raise UpdateError(f"GitHub вернул HTTP {exc.code} при проверке обновления.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"Не удалось связаться с GitHub: {exc}") from exc
        if len(data) > max_bytes:
            raise UpdateError("Ответ GitHub превышает допустимый размер и отклонён.")
        return data, final_url

    def _load_releases(self) -> list[dict[str, Any]]:
        data, _ = self._request_bytes(
            self.api_url,
            max_bytes=MAX_RELEASES_RESPONSE,
            accept="application/vnd.github+json",
        )
        try:
            releases = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("GitHub вернул некорректный список релизов.") from exc
        if not isinstance(releases, list):
            raise UpdateError("Формат ответа GitHub Releases не распознан.")
        return [item for item in releases if isinstance(item, dict)]

    @staticmethod
    def _choose_release(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
        candidates: list[tuple[tuple[int, int, int, tuple[tuple[int, Any], ...], bool], dict[str, Any]]] = []
        for release in releases:
            if release.get("draft") is True:
                continue
            prerelease = bool(release.get("prerelease"))
            if channel == "Stable" and prerelease:
                continue
            if channel not in {"Stable", "Preview"}:
                raise ValueError("Канал обновлений должен быть Stable или Preview.")
            tag = str(release.get("tag_name", ""))
            try:
                key = _version_parts(tag)
            except ValueError:
                continue
            candidates.append((key, release))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        return candidates[0][1]

    def check_for_updates(self, current_version: str, channel: str = "Preview") -> UpdateCheckResult:
        _version_parts(current_version)
        if channel not in {"Stable", "Preview"}:
            raise ValueError("Неизвестный канал обновлений.")
        releases = self._load_releases()
        release = self._choose_release(releases, channel)
        if release is None:
            return UpdateCheckResult(current_version=current_version, channel=channel)

        tag = str(release.get("tag_name", ""))
        latest_version = tag[1:] if tag.startswith("v") else tag
        is_newer = compare_versions(latest_version, current_version) > 0
        release_url = str(release.get("html_url") or GITHUB_RELEASES_PAGE)
        if not release_url.startswith(f"https://github.com/{GITHUB_REPOSITORY}/releases/"):
            release_url = GITHUB_RELEASES_PAGE
        base_result = {
            "current_version": current_version,
            "channel": channel,
            "latest_version": latest_version,
            "update_available": is_newer,
            "release_url": release_url,
            "release_notes": str(release.get("body") or ""),
            "published_at": str(release.get("published_at") or ""),
            "prerelease": bool(release.get("prerelease")),
        }
        if not is_newer:
            return UpdateCheckResult(**base_result)

        assets = release.get("assets") if isinstance(release.get("assets"), list) else []
        asset_by_name = {
            str(asset.get("name")): asset
            for asset in assets
            if isinstance(asset, dict) and asset.get("name")
        }
        manifest_asset = asset_by_name.get("release-manifest.json")
        if not manifest_asset:
            return UpdateCheckResult(**base_result, error="Релиз найден, но в нём нет release-manifest.json. Автоматическая установка недоступна.")

        manifest_url = _validate_release_asset_url(str(manifest_asset.get("browser_download_url") or ""))
        manifest_bytes, _ = self._request_bytes(manifest_url, max_bytes=MAX_MANIFEST_SIZE)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("Манифест обновления повреждён или имеет неверный формат.") from exc
        if not isinstance(manifest, dict):
            raise UpdateError("Манифест обновления должен быть JSON-объектом.")
        if manifest.get("repository") != self.repository or manifest.get("version") != latest_version:
            raise UpdateError("Манифест не соответствует репозиторию или версии выбранного релиза.")
        if manifest.get("product") != "Bannerlord Compatibility Intelligence":
            raise UpdateError("Манифест относится к другому продукту.")
        if manifest.get("architecture") != "x64":
            raise UpdateError("Релиз не предназначен для Windows x64.")
        if manifest.get("schema_version") != 1:
            raise UpdateError("Версия формата манифеста не поддерживается этой сборкой BCI.")
        minimum_updater = str(manifest.get("minimum_updater_version") or "")
        if minimum_updater and compare_versions(current_version, minimum_updater) < 0:
            return UpdateCheckResult(
                **base_result,
                error=(
                    f"Для установки BCI {latest_version} сначала требуется обновить приложение вручную "
                    f"до версии {minimum_updater} или новее."
                ),
            )
        channel_in_manifest = str(manifest.get("channel", "")).lower()
        expected_channel = "preview" if bool(release.get("prerelease")) else "stable"
        if channel_in_manifest != expected_channel:
            raise UpdateError("Канал в манифесте не совпадает с типом GitHub Release.")

        asset_info = manifest.get("assets", {}).get("installer") if isinstance(manifest.get("assets"), dict) else None
        if not isinstance(asset_info, dict):
            raise UpdateError("В манифесте отсутствует описание установщика.")
        installer_name = str(asset_info.get("name") or "")
        if not SAFE_ASSET_NAME_RE.fullmatch(installer_name):
            raise UpdateError("Имя установщика в манифесте недопустимо.")
        expected_name = f"BCI_Setup_{latest_version}_x64.exe"
        if installer_name != expected_name:
            raise UpdateError("Имя установщика не соответствует версии релиза.")
        release_installer = asset_by_name.get(installer_name)
        if not release_installer:
            raise UpdateError("Установщик из манифеста отсутствует среди файлов релиза.")
        digest = str(asset_info.get("sha256") or "").lower()
        if not SHA256_RE.fullmatch(digest):
            raise UpdateError("Контрольная сумма установщика отсутствует или имеет неверный формат.")
        size = int(asset_info.get("size_bytes") or 0)
        if size <= 0 or size > MAX_INSTALLER_SIZE:
            raise UpdateError("Размер установщика в манифесте недопустим.")
        api_size = int(release_installer.get("size") or 0)
        if api_size and api_size != size:
            raise UpdateError("Размер установщика не совпадает с данными GitHub Releases.")
        download_url = _validate_release_asset_url(str(release_installer.get("browser_download_url") or ""))

        return UpdateCheckResult(
            **base_result,
            installer_name=installer_name,
            installer_sha256=digest,
            installer_size=size,
            installer_download_url=download_url,
        )

    def download_and_verify(
        self,
        result: UpdateCheckResult,
        destination_dir: str | Path,
        *,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        if not result.installer_ready:
            raise UpdateError(result.error or "Для выбранного релиза нет проверенного установщика.")
        url = _validate_release_asset_url(result.installer_download_url)
        target_dir = Path(destination_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / result.installer_name
        partial = target.with_suffix(target.suffix + ".part")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"})
        digest = hashlib.sha256()
        downloaded = 0
        try:
            response = self.opener(request, timeout=self.timeout)
            with response, partial.open("wb") as stream:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                _validate_final_download_url(final_url)
                content_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
                if content_length and int(content_length) > MAX_INSTALLER_SIZE:
                    raise UpdateError("Размер загружаемого установщика превышает ограничение.")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_INSTALLER_SIZE or downloaded > result.installer_size:
                        raise UpdateError("Загруженный файл больше размера, указанного в манифесте.")
                    digest.update(chunk)
                    stream.write(chunk)
                    if progress:
                        progress(downloaded, result.installer_size)
            if downloaded != result.installer_size:
                raise UpdateError(
                    f"Размер установщика не совпал: получено {downloaded}, ожидалось {result.installer_size} байт."
                )
            if digest.hexdigest().lower() != result.installer_sha256.lower():
                raise UpdateError("SHA-256 установщика не совпадает с манифестом GitHub Release.")
            os.replace(partial, target)
            return target
        except Exception:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def has_installer_marker(executable_path: str | Path) -> bool:
    """Return whether an executable is inside an Inno-Setup-managed install folder."""
    try:
        executable = Path(executable_path).resolve()
        return (executable.parent / ".bci-installed").is_file()
    except OSError:
        return False


def is_installer_managed_install() -> bool:
    """Auto-update is enabled only for the Inno-installed Windows application."""
    return os.name == "nt" and has_installer_marker(sys.executable)


def launch_verified_installer(installer_path: str | Path) -> None:
    """Launch an already verified Inno Setup installer and request UAC elevation."""
    path = Path(installer_path).resolve()
    if os.name != "nt":
        raise OSError("Автоматическая установка обновления поддерживается только в Windows.")
    if not path.is_file() or path.suffix.lower() != ".exe":
        raise FileNotFoundError("Проверенный установщик BCI не найден.")
    import ctypes

    parameters = "/VERYSILENT /NORESTART /CLOSEAPPLICATIONS"
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell_execute = shell32.ShellExecuteW
    shell_execute.restype = ctypes.c_void_p
    shell_execute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    result = shell_execute(None, "runas", str(path), parameters, str(path.parent), 1)
    if result is None or result <= 32:
        raise OSError(f"Windows не смогла запустить установщик BCI (ShellExecute={result}).")
