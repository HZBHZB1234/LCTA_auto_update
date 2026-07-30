from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import zipfile

from auto_update.config import PublishingConfig


def create_packages(
    output_directory: Path,
    assets_directory: Path,
    version: str,
    config: PublishingConfig,
) -> list[Path]:
    assets_directory.mkdir(parents=True, exist_ok=True)
    assets: list[Path] = []
    base_name = f"{config.asset_prefix}-{version}"

    if config.zip:
        zip_path = assets_directory / f"{base_name}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in output_directory.rglob("*"):
                if file_path.is_file():
                    archive.write(
                        file_path,
                        file_path.relative_to(output_directory.parent),
                    )
        assets.append(zip_path)

    if config.seven_zip:
        seven_zip_path = assets_directory / f"{base_name}.7z"
        seven_zip = shutil.which("7z") or shutil.which("7zz")
        if not seven_zip:
            raise RuntimeError("已启用 7Z 发布，但系统中未找到 7z 或 7zz")
        subprocess.run(
            [
                seven_zip,
                "a",
                "-t7z",
                str(seven_zip_path),
                output_directory.name,
            ],
            cwd=output_directory.parent,
            check=True,
        )
        assets.append(seven_zip_path)

    if not assets:
        raise RuntimeError("publishing.zip 与 publishing.seven_zip 不能同时关闭")
    for asset in assets:
        if not asset.is_file() or asset.stat().st_size == 0:
            raise RuntimeError(f"打包结果无效: {asset}")
    return assets
