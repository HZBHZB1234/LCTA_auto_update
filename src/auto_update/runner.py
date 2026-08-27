from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from auto_update.archive import (
    extract_archive_safely,
    extract_zip_safely,
    find_named_directory,
    find_repository_root,
)
from auto_update.clients import (
    GitHubClient,
    GitHubRelease,
    HttpClient,
    RawStatus,
    build_release_metadata,
    fetch_raw_status,
    find_release_for_token,
)
from auto_update.config import AppConfig
from auto_update.packaging import create_packages
from auto_update.versioning import is_version_tag, next_version
from translateFunc import PipelineSummary, TranslateConfig, TranslationPipeline
from translateFunc.diagnostics import safe_json_value


_logger = logging.getLogger(__name__)
_SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")


def run(project_root: Path, config: AppConfig) -> None:
    diagnostics_path = project_root / "run-summary.json"
    _write_actions_outputs(
        should_publish=False,
        version="",
        diagnostics_enabled=config.publishing.upload_diagnostics,
    )
    if not config.features.enabled:
        _logger.info("自动更新已通过配置关闭")
        return

    github_token = os.getenv(config.network.github_token_env, "") or os.getenv(
        "GITHUB_TOKEN", ""
    )
    http = HttpClient(config.network)
    github = GitHubClient(http, github_token)

    raw_status = fetch_raw_status(http, config.sources.status_url)
    own_repository = os.getenv(
        "GITHUB_REPOSITORY", "HZBHZB1234/LCTA_auto_update"
    )
    own_releases = [
        release
        for release in github.list_releases(own_repository)
        if not release.draft and not release.prerelease
    ]
    manual_run = os.getenv("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    version, matching_release = _select_release_version(
        own_releases,
        raw_token=raw_status.token,
        manual_run=manual_run,
        deduplicate=config.features.deduplicate,
        now=datetime.now(_SHANGHAI),
    )

    if version is None:
        assert matching_release is not None
        _logger.info(
            "raw token %s 已由 Release %s 处理，跳过",
            raw_status.token,
            matching_release.tag_name,
        )
        diagnostics_path.write_text(
            json.dumps(
                {
                    "status": "skipped",
                    "reason": "duplicate_raw_token",
                    "raw_token": raw_status.token,
                    "release": matching_release.tag_name,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _write_actions_outputs(
            should_publish=False,
            version=matching_release.tag_name,
            diagnostics_enabled=config.publishing.upload_diagnostics,
        )
        return

    if manual_run and matching_release and version == matching_release.tag_name:
        _logger.info("手动重建 raw token，对 Release %s 执行覆盖", version)

    cooked_release = github.latest_stable_release(
        config.sources.cooked_repository
    )
    _logger.info(
        "开始处理 raw=%s cooked=%s output=%s",
        raw_status.token,
        cooked_release.tag_name,
        version,
    )

    output_directory = project_root / config.publishing.output_dir
    assets_directory = project_root / "release-assets"
    _remove_generated_path(project_root, output_directory)
    _remove_generated_path(project_root, assets_directory)
    # 诊断包可能持久化的源文件目录（cooked LLC / 游戏原文），先清理残留
    _remove_generated_path(project_root, project_root / "diagnostics")

    with tempfile.TemporaryDirectory(prefix="lcta-auto-update-") as temp_name:
        temporary_root = Path(temp_name)
        raw_paths = _download_raw_sources(
            http, config, raw_status, temporary_root
        )
        cooked_root = _download_cooked_source(
            github,
            cooked_release,
            temporary_root,
            asset_prefix=config.sources.cooked_asset_prefix,
            asset_format=config.sources.cooked_asset_format,
        )
        summary, staged_output = _run_translation(
            config,
            raw_paths=raw_paths,
            cooked_root=cooked_root,
            temporary_root=temporary_root,
        )
        dump_file = temporary_root / "translation-dump.jsonl"
        if dump_file.is_file():
            shutil.copy2(dump_file, project_root / dump_file.name)
        if config.publishing.upload_diagnostics:
            # 将 cooked LLC 与游戏原文持久化到 project_root，随诊断包一起上传
            _stage_diagnostic_sources(
                project_root,
                raw_paths=raw_paths,
                cooked_root=cooked_root,
            )
        if not any(staged_output.rglob("*.json")):
            raise RuntimeError("翻译管线没有生成任何 JSON 文件")
        _write_package_info(
            staged_output,
            cooked_root,
            {
                "version": version,
                "raw_token": raw_status.token,
                "raw_created_at": raw_status.created_at,
                "cooked_release": cooked_release.tag_name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        shutil.move(str(staged_output), str(output_directory))

    version_info = json.loads(
        (output_directory / "Info" / "version.json").read_text(encoding="utf-8")
    )

    summary_data = _summary_data(
        summary,
        version_info=version_info,
        manual_run=manual_run,
        overwritten_release=bool(manual_run and matching_release),
    )
    diagnostics_path.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    release_note = _build_release_note(summary_data)
    (project_root / "update.md").write_text(release_note, encoding="utf-8")
    assets = create_packages(
        output_directory,
        assets_directory,
        version,
        config.publishing,
    )
    _logger.info("已生成 %d 个发布资产", len(assets))
    _write_actions_outputs(
        should_publish=True,
        version=version,
        diagnostics_enabled=config.publishing.upload_diagnostics,
    )


def _download_raw_sources(
    http: HttpClient,
    config: AppConfig,
    raw_status: RawStatus,
    temporary_root: Path,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for language in config.sources.raw_languages:
        archive_path = temporary_root / "downloads" / f"localize_{language}.zip"
        url = config.sources.raw_url_template.format(
            token=raw_status.token, lang=language
        )
        http.download(url, archive_path)
        extracted = temporary_root / "raw" / language
        extract_zip_safely(archive_path, extracted)
        result[language] = find_named_directory(
            extracted, f"LocalizeTemp_{language}"
        )
    return result


def _download_cooked_source(
    github: GitHubClient,
    release: GitHubRelease,
    temporary_root: Path,
    *,
    asset_prefix: str,
    asset_format: str,
) -> Path:
    asset = github.find_cooked_asset(release, asset_prefix, asset_format)
    archive_path = temporary_root / "downloads" / f"cooked.{asset_format}"
    github.download_release_asset(asset, archive_path)
    extracted = temporary_root / "cooked"
    extract_archive_safely(archive_path, extracted)
    return find_repository_root(extracted, "LLC_zh-CN")


def _run_translation(
    config: AppConfig,
    *,
    raw_paths: dict[str, Path],
    cooked_root: Path,
    temporary_root: Path,
) -> tuple[PipelineSummary, Path]:
    api_settings = dict(config.translation.api)
    api_key = os.getenv(config.translation.api_key_env, "")
    if config.translation.translator_name == "LLM通用翻译服务" and not api_key:
        raise RuntimeError(
            f"缺少翻译 API key 环境变量: {config.translation.api_key_env}"
        )
    if api_key:
        api_settings["api_key"] = api_key

    pipeline_output_root = temporary_root / "pipeline-output"
    dump_path = temporary_root / "translation-dump.jsonl"
    translate_config = TranslateConfig(
        translator_name=config.translation.translator_name,
        translator_api=api_settings,
        output_dir=pipeline_output_root,
        enable_proper=config.features.enable_proper,
        auto_fetch_proper=config.features.auto_fetch_proper,
        proper_path=config.translation.proper_path,
        enable_role=config.features.enable_role,
        enable_skill=config.features.enable_skill,
        enable_dev_settings=True,
        max_workers=config.translation.max_workers,
        enable_concurrent=config.features.enable_concurrent,
        translation_mode=config.translation.translation_mode,
        enable_self_check=config.features.enable_self_check,
        enable_rule_validation=config.features.enable_rule_validation,
        disambiguation_mode=config.translation.disambiguation_mode,
        min_confidence=config.translation.min_confidence,
        prompt_format=config.translation.prompt_format,
        enable_thinking=config.features.enable_thinking,
        debug_mode=config.features.debug_mode,
        dump=config.features.dump,
        dump_path=dump_path if config.features.dump else None,
        fallback=config.features.fallback,
        has_prefix=True,
        from_lang=config.translation.from_lang,
        kr_path=str(raw_paths["kr"]),
        jp_path=str(raw_paths["jp"]),
        en_path=str(raw_paths["en"]),
        llc_path=str(cooked_root / "LLC_zh-CN"),
    )
    pipeline = TranslationPipeline(translate_config)
    pipeline.set_callbacks(on_log=_logger.info)
    summary = pipeline.run()
    return summary, pipeline_output_root / "LLc-CN-LCTA"


def _stage_diagnostic_sources(
    project_root: Path,
    *,
    raw_paths: dict[str, Path],
    cooked_root: Path,
) -> None:
    """将 cooked LLC 与游戏原文复制到 project_root，供诊断包上传使用。

    cooked LLC 来自熟肉仓库的 ``LLC_zh-CN`` 目录；游戏原文来自各语言生肉
    下载目录（``raw_paths``）。两者默认只存在于临时目录，流程结束后会被
    自动清理，因此需要在上传诊断包之前把它们持久化到 ``project_root``。
    """
    diagnostics_root = project_root / "diagnostics"
    _remove_generated_path(project_root, diagnostics_root)
    diagnostics_root.mkdir(parents=True, exist_ok=True)

    # cooked LLC（已翻译的中文参考文件）
    cooked_llc = cooked_root / "LLC_zh-CN"
    if cooked_llc.is_dir():
        shutil.copytree(cooked_llc, diagnostics_root / "cooked-LLC_zh-CN")

    # 游戏原文（各语言生肉）
    for language, source_path in raw_paths.items():
        if source_path.is_dir():
            shutil.copytree(source_path, diagnostics_root / f"raw-{language}")


def _write_package_info(
    output_directory: Path,
    cooked_root: Path,
    version_info: dict[str, str],
) -> None:
    info_directory = output_directory / "Info"
    info_directory.mkdir(parents=True, exist_ok=True)
    (info_directory / "version.json").write_text(
        json.dumps(version_info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # cooked 资产内部布局可能嵌套（如 LimbusCompany_Data/Lang/LLC_zh-CN），
    # LICENSE 不一定位于 cooked_root 顶层，故做一次递归查找以尽力还原旧行为。
    license_source = cooked_root / "LICENSE"
    if not license_source.is_file():
        matches = [p for p in cooked_root.rglob("LICENSE") if p.is_file()]
        if matches:
            license_source = matches[0]
    if license_source.is_file():
        shutil.copy2(license_source, info_directory / "LICENSE")


def _summary_data(
    summary: PipelineSummary,
    *,
    version_info: dict[str, str],
    manual_run: bool,
    overwritten_release: bool,
) -> dict[str, Any]:
    return {
        "status": "completed_with_issues" if summary.errors else "completed",
        **version_info,
        "manual_run": manual_run,
        "overwritten_release": overwritten_release,
        "translation": {
            "total": summary.total,
            "saved": summary.success_count,
            "skipped": len(summary.skipped),
            "fallback": summary.fallback_count,
            "errors": summary.error_count,
            "error_details": [
                {
                    "file": outcome.file_name,
                    "result": outcome.result.name,
                    "extra": safe_json_value(outcome.extra or {}),
                }
                for outcome in summary.errors
            ],
        },
    }


def _build_release_note(summary: dict[str, Any]) -> str:
    translation = summary["translation"]
    metadata = build_release_metadata(
        {
            "raw_token": summary["raw_token"],
            "raw_created_at": summary["raw_created_at"],
            "cooked_release": summary["cooked_release"],
            "generated_at": summary["generated_at"],
        }
    )
    issues = ""
    if translation["errors"]:
        issue_lines = [
            f"- `{item['file']}`: {item['result']}"
            for item in translation["error_details"][:100]
        ]
        issues = "\n## 翻译异常\n\n" + "\n".join(issue_lines) + "\n"
    return (
        f"# {summary['version']} 版资源更新\n\n"
        f"- 生肉 token：`{summary['raw_token']}`\n"
        f"- 熟肉 Release：`{summary['cooked_release']}`\n"
        f"- 保存：{translation['saved']}\n"
        f"- 跳过：{translation['skipped']}\n"
        f"- 回退：{translation['fallback']}\n"
        f"- 错误：{translation['errors']}\n"
        f"{issues}\n"
        "使用 LCTA 工具箱启动器可自动完成版本更新。\n\n"
        f"{metadata}\n"
    )


def _write_actions_outputs(
    *, should_publish: bool, version: str, diagnostics_enabled: bool
) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"should_publish={str(should_publish).lower()}\n")
        output.write(f"version={version}\n")
        output.write(
            f"diagnostics_enabled={str(diagnostics_enabled).lower()}\n"
        )


def _remove_generated_path(project_root: Path, target: Path) -> None:
    if not target.exists():
        return
    resolved_root = project_root.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_root) or resolved_target == resolved_root:
        raise RuntimeError(f"拒绝删除工作区之外的路径: {resolved_target}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()


def _select_release_version(
    releases: list[GitHubRelease],
    *,
    raw_token: str,
    manual_run: bool,
    deduplicate: bool,
    now: datetime,
) -> tuple[str | None, GitHubRelease | None]:
    matching_release = find_release_for_token(releases, raw_token)
    if deduplicate and matching_release and not manual_run:
        return None, matching_release
    if manual_run and matching_release and is_version_tag(matching_release.tag_name):
        return matching_release.tag_name, matching_release
    previous_tag = next(
        (
            release.tag_name
            for release in releases
            if is_version_tag(release.tag_name)
        ),
        None,
    )
    return next_version(previous_tag, now), matching_release
