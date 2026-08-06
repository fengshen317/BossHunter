"""Launch a local Chromium browser for the BossHunter one-click workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping

SUPPORTED_BROWSER_CHOICES = {"auto", "chrome", "edge"}


def find_browser_executable(
    browser_choice: str = "auto",
    configured_path: Path | str | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Find a Chrome or Edge executable without changing system configuration.

    Args:
        browser_choice: ``auto``, ``chrome``, or ``edge``.
        configured_path: Optional explicit executable path from configuration.
        environment: Environment mapping used to resolve platform install paths.

    Returns:
        The first usable executable path, or ``None`` when no browser is found.
    """
    if configured_path:
        explicit_path = Path(configured_path).expanduser()
        if explicit_path.is_file():
            return explicit_path

    choice = browser_choice if browser_choice in SUPPORTED_BROWSER_CHOICES else "auto"
    browser_names = ("chrome", "edge") if choice == "auto" else (choice,)
    command_names = {
        "chrome": ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable"),
        "edge": ("msedge", "msedge.exe", "microsoft-edge"),
    }
    for browser_name in browser_names:
        for command_name in command_names[browser_name]:
            discovered = shutil.which(command_name)
            if discovered:
                return Path(discovered)

    for candidate in _platform_browser_candidates(browser_names, environment or os.environ):
        if candidate.is_file():
            return candidate
    return None


def build_browser_command(
    executable: Path,
    *,
    debug_port: int,
    user_data_dir: Path,
    dashboard_url: str,
) -> list[str]:
    """Build a browser command that opens only the local dashboard initially."""
    return [
        str(executable),
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir.resolve()}",
        "--no-first-run",
        "--no-default-browser-check",
        dashboard_url,
    ]


def launch_browser(
    *,
    executable: Path,
    debug_port: int,
    user_data_dir: Path,
    dashboard_url: str,
) -> subprocess.Popen:
    """Launch the visible browser used by the user for login and automation."""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    command = build_browser_command(
        executable,
        debug_port=debug_port,
        user_data_dir=user_data_dir,
        dashboard_url=dashboard_url,
    )
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _platform_browser_candidates(
    browser_names: tuple[str, ...],
    environment: Mapping[str, str],
) -> list[Path]:
    """Return conventional browser installation paths for the current platform."""
    if os.name == "nt":
        roots = [
            Path(value)
            for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
            if (value := environment.get(key))
        ]
        relative_paths = {
            "chrome": Path("Google/Chrome/Application/chrome.exe"),
            "edge": Path("Microsoft/Edge/Application/msedge.exe"),
        }
        return [root / relative_paths[name] for name in browser_names for root in roots]

    if sys.platform == "darwin":
        paths = {
            "chrome": Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "edge": Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        }
        return [paths[name] for name in browser_names]

    paths = {
        "chrome": (Path("/usr/bin/google-chrome"), Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser")),
        "edge": (Path("/usr/bin/microsoft-edge"), Path("/usr/bin/microsoft-edge-stable")),
    }
    return [path for name in browser_names for path in paths[name]]
