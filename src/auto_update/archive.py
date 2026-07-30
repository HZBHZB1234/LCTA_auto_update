from __future__ import annotations

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
