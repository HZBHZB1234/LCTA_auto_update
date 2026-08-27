from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
import zipfile


def extract_zip_safely(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        bad_file = archive.testzip()
        if bad_file is not None:
            raise RuntimeError(f"ZIP 校验失败: {bad_file}")
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            if not member_path.is_relative_to(destination_root):
                raise RuntimeError(f"ZIP 包含不安全路径: {member.filename}")
        archive.extractall(destination)


def extract_archive_safely(archive_path: Path, destination: Path) -> None:
    """按扩展名分发的安全解压，用于 cooked LLC 资产（zip / 7z）。"""
    destination.mkdir(parents=True, exist_ok=True)
    suffix = archive_path.suffix.lower()
    if suffix == ".zip":
        extract_zip_safely(archive_path, destination)
        return
    if suffix == ".7z":
        seven_zip = shutil.which("7z") or shutil.which("7zz")
        if not seven_zip:
            raise RuntimeError("需要解压 7z 资产，但系统中未找到 7z 或 7zz")
        subprocess.run(
            [seven_zip, "x", "-y", f"-o{destination}", str(archive_path)],
            check=True,
        )
        _assert_within_root(destination)
        return
    raise RuntimeError(f"不支持的归档格式: {archive_path.name}")


def _assert_within_root(destination: Path) -> None:
    """解压后复核所有成员都落在目标目录内，防止路径穿越。"""
    destination_root = destination.resolve()
    for member in destination.rglob("*"):
        if not member.resolve().is_relative_to(destination_root):
            raise RuntimeError(f"归档包含不安全路径: {member}")


def find_named_directory(root: Path, name: str) -> Path:
    direct = root / name
    if direct.is_dir():
        return direct
    matches = [path for path in root.rglob(name) if path.is_dir()]
    if len(matches) != 1:
        raise RuntimeError(
            f"无法唯一定位目录 {name}: 找到 {len(matches)} 个候选"
        )
    return matches[0]


def find_repository_root(extracted_root: Path, required_directory: str) -> Path:
    required = find_named_directory(extracted_root, required_directory)
    return required.parent
