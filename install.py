#!/usr/bin/env python3
"""Install or safely update the OpenCode team room plugin."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
HOME_DIR = Path.home()
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", HOME_DIR / ".config")).expanduser()
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", HOME_DIR / ".local/share")).expanduser()
PLUGIN = CONFIG_HOME / "opencode/plugins/app-workflow-team"
BASE = DATA_HOME / "opencode-team-room"
RELEASES = BASE / "releases"
RELEASE = RELEASES / VERSION
VENV = BASE / "venv"
CURRENT = BASE / "current"
WRAPPER = BASE / "opencode-team-room"
BIN_DIR = HOME_DIR / ".local/bin"
COMMAND = BIN_DIR / "opencode-team-room"
RECEIPT = ".app-workflow-team-install.json"
INSTALL_RECEIPT = "install.json"
SUPPORTED_OPENCODE = "2.1.3"


def refuse_symlinks(path: Path, include_leaf: bool = True) -> None:
    components = (path.absolute(), *path.absolute().parents) if include_leaf else tuple(path.absolute().parents)
    for item in components:
        if item.is_symlink():
            raise SystemExit(f"Refusing symlinked install path: {item}")


def snapshot(root: Path, skip_receipt: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                directories.remove(name)
                result[relative] = "link:" + os.readlink(item)
            else:
                result[relative] = f"dir:{stat.S_IMODE(item.stat().st_mode):04o}"
        for name in files:
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            if skip_receipt and relative == RECEIPT:
                continue
            if item.is_symlink():
                result[relative] = "link:" + os.readlink(item)
            else:
                digest = hashlib.sha256(item.read_bytes()).hexdigest()
                result[relative] = f"file:{stat.S_IMODE(item.stat().st_mode):04o}:{digest}"
    return result


def check_toolchain() -> str:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    for command in ("opencode", "bun", "openssl"):
        if not shutil.which(command):
            raise SystemExit(f"Required command not found: {command}")
    try:
        result = subprocess.run(
            ["opencode", "--version"], capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        raise SystemExit("Could not read the installed OpenCode version.") from None
    version = result.stdout.strip()
    if not re.search(rf"(?:^|\s)v?{re.escape(SUPPORTED_OPENCODE)}(?:\s|$)", version):
        raise SystemExit(
            f"This plugin targets OpenCode {SUPPORTED_OPENCODE}; found {version or 'unknown'}. "
            "Review the TUI API before upgrading."
        )
    return version


def plugin_receipt(path: Path) -> dict:
    if path.is_symlink() or not path.is_dir():
        raise SystemExit("The existing plugin path is not a regular directory; refusing replacement.")
    receipt = path / RECEIPT
    if receipt.is_symlink() or not receipt.is_file():
        raise SystemExit("The existing plugin has no safe install receipt; refusing replacement.")
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SystemExit("The existing plugin receipt is unreadable; refusing replacement.") from None
    managed = data.get("manager") == "opencode-team-room" or data.get("source") == "app-toolkit integration/opencode/team-plugin"
    if data.get("version") != 1 or not managed or data.get("files") != snapshot(path, skip_receipt=True):
        raise SystemExit("The existing plugin changed or is not managed by this installer; refusing replacement.")
    return data


def verify_existing_install() -> dict:
    receipt_path = BASE / INSTALL_RECEIPT
    if BASE.is_symlink() or not BASE.is_dir() or receipt_path.is_symlink() or not receipt_path.is_file():
        raise SystemExit("The app-workflow room data path already exists without a safe install receipt; refusing replacement.")
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SystemExit("The app-workflow room install receipt is unreadable; refusing replacement.") from None
    old_version = data.get("version")
    old_release = RELEASES / str(old_version)
    if (
        data.get("format") != 1
        or data.get("command") != str(COMMAND)
        or not isinstance(old_version, str)
        or not re.fullmatch(r"\d+\.\d+\.\d+", old_version)
        or old_release.is_symlink()
        or not old_release.is_dir()
        or data.get("release_files") != snapshot(old_release)
        or not CURRENT.is_symlink()
        or os.readlink(CURRENT) != f"releases/{old_version}"
        or VENV.is_symlink()
        or not VENV.is_dir()
        or WRAPPER.is_symlink()
        or not WRAPPER.is_file()
        or hashlib.sha256(WRAPPER.read_bytes()).hexdigest() != data.get("wrapper_sha256")
        or not COMMAND.is_symlink()
        or COMMAND.resolve() != WRAPPER.resolve()
    ):
        raise SystemExit("The existing app-workflow room installation changed; refusing replacement.")
    return data


def copy_release(destination: Path) -> None:
    source_package = ROOT / "app_workflow"
    source_requirements = ROOT / "requirements-team.txt"
    if not source_package.is_dir() or not source_requirements.is_file():
        raise SystemExit("The room runtime source is incomplete.")
    for source in (source_package, source_requirements):
        if source.is_symlink():
            raise SystemExit(f"Refusing symlinked runtime source: {source}")
    for current, directories, files in os.walk(source_package, followlinks=False):
        if any((Path(current) / name).is_symlink() for name in [*directories, *files]):
            raise SystemExit(f"Refusing symlinks in runtime source: {current}")
    shutil.copytree(source_package, destination / "app_workflow", symlinks=False)
    shutil.copy2(source_requirements, destination / "requirements-team.txt")


def prepare_plugin() -> Path:
    source = ROOT / "plugin"
    if not source.is_dir():
        raise SystemExit("The OpenCode plugin source is missing.")
    for current, directories, files in os.walk(source, followlinks=False):
        if any((Path(current) / name).is_symlink() for name in [*directories, *files]):
            raise SystemExit(f"Refusing symlinks in plugin source: {current}")
    refuse_symlinks(PLUGIN)
    PLUGIN.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".app-workflow-team-", dir=PLUGIN.parent))
    try:
        shutil.copytree(source, stage, dirs_exist_ok=True)
        subprocess.run(
            ["bun", "install", "--frozen-lockfile", "--ignore-scripts"],
            cwd=stage,
            check=True,
        )
        receipt = {
            "version": 1,
            "manager": "opencode-team-room",
            "package_version": VERSION,
            "files": snapshot(stage),
        }
        (stage / RECEIPT).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (stage / RECEIPT).chmod(0o600)
        return stage
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def build_wrapper() -> None:
    script = (
        "#!/bin/sh\n"
        f"BASE={shlex.quote(str(BASE))}\n"
        "PYTHONPATH=\"$BASE/current${PYTHONPATH:+:$PYTHONPATH}\" exec \"$BASE/venv/bin/python\" -m app_workflow.team_plugin_cli \"$@\"\n"
    )
    WRAPPER.write_text(script, encoding="utf-8")
    WRAPPER.chmod(0o700)


def create_install(version: str) -> None:
    refuse_symlinks(BASE)
    if BASE.exists() or BASE.is_symlink():
        raise SystemExit("Room data already exists; pass --update only for an installation created by this installer.")
    BASE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    BASE.mkdir(mode=0o700)
    RELEASES.mkdir(mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix=".release-", dir=RELEASES))
    try:
        copy_release(stage)
        final_release = RELEASES / version
        if final_release.exists() or final_release.is_symlink():
            raise SystemExit(f"Refusing to overwrite existing runtime release: {final_release}")
        stage.rename(final_release)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        subprocess.run(
            [str(VENV / "bin/python"), "-m", "pip", "install", "--requirement", str(final_release / "requirements-team.txt")],
            check=True,
        )
        CURRENT.symlink_to(f"releases/{version}")
        build_wrapper()
        BIN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        refuse_symlinks(COMMAND)
        if COMMAND.exists() or COMMAND.is_symlink():
            raise SystemExit(f"Refusing to overwrite existing command: {COMMAND}")
        temporary_link = BIN_DIR / f".opencode-team-room-{os.getpid()}"
        temporary_link.symlink_to(WRAPPER)
        temporary_link.rename(COMMAND)
    except BaseException:
        shutil.rmtree(BASE, ignore_errors=True)
        if stage.exists() and not stage.is_symlink():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def update_install(old_receipt: dict, version: str) -> None:
    release_stage = Path(tempfile.mkdtemp(prefix=".release-", dir=RELEASES))
    try:
        copy_release(release_stage)
        final_release = RELEASES / version
        if final_release.exists() or final_release.is_symlink():
            if final_release.is_symlink() or snapshot(final_release) != snapshot(release_stage):
                raise SystemExit(f"Runtime version {version} already exists with different contents; increment VERSION before publishing.")
            shutil.rmtree(release_stage)
        else:
            release_stage.rename(final_release)
        subprocess.run(
            [str(VENV / "bin/python"), "-m", "pip", "install", "--requirement", str(final_release / "requirements-team.txt")],
            check=True,
        )
        next_link = BASE / f".current-{os.getpid()}"
        next_link.symlink_to(f"releases/{version}")
        os.replace(next_link, CURRENT)
        build_wrapper()
    except BaseException:
        if release_stage.exists() and not release_stage.is_symlink():
            shutil.rmtree(release_stage, ignore_errors=True)
        raise


def save_install_receipt(plugin_files: dict[str, str]) -> None:
    current_version = os.readlink(CURRENT).split("/", 1)[1]
    data = {
        "format": 1,
        "version": current_version,
        "release_files": snapshot(RELEASES / current_version),
        "plugin_files": plugin_files,
        "command": str(COMMAND),
        "wrapper_sha256": hashlib.sha256(WRAPPER.read_bytes()).hexdigest(),
        "requirements_sha256": hashlib.sha256((RELEASES / current_version / "requirements-team.txt").read_bytes()).hexdigest(),
    }
    temporary = BASE / f".{INSTALL_RECEIPT}-{os.getpid()}"
    temporary.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, BASE / INSTALL_RECEIPT)


def replace_plugin(stage: Path, existing_receipt: dict | None) -> Path | None:
    if PLUGIN.exists() or PLUGIN.is_symlink():
        if not existing_receipt:
            raise SystemExit("Refusing to replace an existing OpenCode plugin without a verified managed receipt.")
        backup_root = BASE / "backups"
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        backup = backup_root / f"plugin-{stamp}"
        if backup.exists() or backup.is_symlink():
            raise SystemExit(f"Refusing to overwrite an existing backup: {backup}")
        before = snapshot(PLUGIN, skip_receipt=True)
        shutil.copytree(PLUGIN, backup, symlinks=True)
        if snapshot(PLUGIN, skip_receipt=True) != before or snapshot(backup, skip_receipt=True) != before:
            shutil.rmtree(backup, ignore_errors=True)
            raise SystemExit("Plugin changed while backing it up; refusing replacement.")
        parked = PLUGIN.parent / f".{PLUGIN.name}.replacing-{os.getpid()}"
        if parked.exists() or parked.is_symlink():
            raise SystemExit(f"Refusing to overwrite temporary path: {parked}")
        PLUGIN.rename(parked)
        try:
            stage.rename(PLUGIN)
        except BaseException:
            parked.rename(PLUGIN)
            raise
        shutil.rmtree(parked)
        return backup
    if existing_receipt:
        raise SystemExit("The managed plugin disappeared during update; refusing an implicit reinstall.")
    stage.rename(PLUGIN)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="update only an unchanged installation created by this installer")
    args = parser.parse_args()

    if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
        raise SystemExit("Invalid VERSION file; use semantic version format such as 0.1.0.")
    version = check_toolchain()
    refuse_symlinks(PLUGIN)
    refuse_symlinks(BASE)
    refuse_symlinks(COMMAND, include_leaf=False)

    base_exists = BASE.exists() or BASE.is_symlink()
    plugin_exists = PLUGIN.exists() or PLUGIN.is_symlink()
    if (base_exists or plugin_exists) and not args.update:
        raise SystemExit("An installation already exists. Use --update only if it was installed by this project.")

    old_receipt = verify_existing_install() if base_exists else None
    old_plugin = plugin_receipt(PLUGIN) if plugin_exists else None
    plugin_stage: Path | None = prepare_plugin()
    backup = None
    try:
        if old_receipt:
            update_install(old_receipt, VERSION)
        else:
            create_install(VERSION)
        save_install_receipt(snapshot(plugin_stage, skip_receipt=True))
        backup = replace_plugin(plugin_stage, old_plugin)
        plugin_stage = None
    finally:
        if plugin_stage is not None and plugin_stage.exists() and not plugin_stage.is_symlink():
            shutil.rmtree(plugin_stage, ignore_errors=True)

    print(f"Installed OpenCode team room {VERSION} for {version}.")
    print(f"Plugin: {PLUGIN}")
    print(f"Command: {COMMAND}")
    print("Restart OpenCode, then use /team-room or choose Team workspace from the command palette.")
    if backup:
        print(f"Previous plugin backed up at: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
