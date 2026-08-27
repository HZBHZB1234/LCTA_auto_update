from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import yaml


class ConfigError(ValueError):
    """配置文件无效。"""


@dataclass(frozen=True)
class SourcesConfig:
    status_url: str
    raw_url_template: str
    raw_languages: tuple[str, ...]
    cooked_repository: str
    cooked_asset_prefix: str
    cooked_asset_format: str


@dataclass(frozen=True)
class NetworkConfig:
    connect_timeout: float
    read_timeout: float
    retries: int
    github_token_env: str

    @property
    def timeout(self) -> tuple[float, float]:
        return self.connect_timeout, self.read_timeout


@dataclass(frozen=True)
class TranslationSettings:
    translator_name: str
    api_key_env: str
    api: dict[str, Any]
    from_lang: str
    proper_path: str
    max_workers: int
    translation_mode: str
    disambiguation_mode: str
    min_confidence: str
    prompt_format: str


@dataclass(frozen=True)
class FeatureConfig:
    enabled: bool
    deduplicate: bool
    enable_proper: bool
    auto_fetch_proper: bool
    enable_role: bool
    enable_skill: bool
    enable_concurrent: bool
    enable_self_check: bool
    enable_rule_validation: bool
    enable_thinking: bool
    fallback: bool
    debug_mode: bool
    dump: bool


@dataclass(frozen=True)
class PublishingConfig:
    zip: bool
    seven_zip: bool
    output_dir: str
    asset_prefix: str
    upload_diagnostics: bool


@dataclass(frozen=True)
class AppConfig:
    sources: SourcesConfig
    network: NetworkConfig
    translation: TranslationSettings
    features: FeatureConfig
    publishing: PublishingConfig

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError(f"配置文件不存在: {path}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件不是有效 YAML: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError("配置文件根节点必须是对象")

        sources = _object(raw, "sources")
        network = _object(raw, "network")
        translation = _object(raw, "translation")
        features = _object(raw, "features")
        publishing = _object(raw, "publishing")

        languages = _string_list(sources, "raw_languages")
        if set(languages) != {"jp", "kr", "en"} or len(languages) != 3:
            raise ConfigError("sources.raw_languages 必须且只能包含 jp、kr、en")

        status_url = _url(sources, "status_url")
        raw_url_template = _string(sources, "raw_url_template")
        if "{token}" not in raw_url_template or "{lang}" not in raw_url_template:
            raise ConfigError("sources.raw_url_template 必须包含 {token} 和 {lang}")
        _validate_url(raw_url_template.format(token="token", lang="kr"), "sources.raw_url_template")

        cooked_repository = _string(sources, "cooked_repository")
        if cooked_repository.count("/") != 1:
            raise ConfigError("sources.cooked_repository 必须使用 owner/repository 格式")

        cooked_asset_prefix = _optional_string(sources, "cooked_asset_prefix")
        cooked_asset_format = _choice(
            sources, "cooked_asset_format", {"zip", "7z"}
        )

        connect_timeout = _positive_number(network, "connect_timeout")
        read_timeout = _positive_number(network, "read_timeout")
        retries = _integer(network, "retries", minimum=0, maximum=10)

        max_workers = _integer(translation, "max_workers", minimum=1, maximum=32)
        translation_mode = _choice(
            translation, "translation_mode", {"multi_stage", "single_stage"}
        )
        disambiguation_mode = _choice(
            translation, "disambiguation_mode", {"similarity", "llm", "hybrid"}
        )
        min_confidence = _choice(
            translation, "min_confidence", {"high", "medium", "low"}
        )
        prompt_format = _choice(
            translation, "prompt_format", {"xml_json", "xml_xml", "json_json"}
        )

        output_dir = _safe_name(publishing, "output_dir")
        asset_prefix = _safe_name(publishing, "asset_prefix")
        publish_zip = _boolean(publishing, "zip")
        publish_seven_zip = _boolean(publishing, "seven_zip")
        if not publish_zip and not publish_seven_zip:
            raise ConfigError("publishing.zip 与 publishing.seven_zip 不能同时关闭")

        return cls(
            sources=SourcesConfig(
                status_url=status_url,
                raw_url_template=raw_url_template,
                raw_languages=tuple(languages),
                cooked_repository=cooked_repository,
                cooked_asset_prefix=cooked_asset_prefix,
                cooked_asset_format=cooked_asset_format,
            ),
            network=NetworkConfig(
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                retries=retries,
                github_token_env=_string(network, "github_token_env"),
            ),
            translation=TranslationSettings(
                translator_name=_string(translation, "translator_name"),
                api_key_env=_string(translation, "api_key_env"),
                api=_object(translation, "api"),
                from_lang=_string(translation, "from_lang"),
                proper_path=_optional_string(translation, "proper_path"),
                max_workers=max_workers,
                translation_mode=translation_mode,
                disambiguation_mode=disambiguation_mode,
                min_confidence=min_confidence,
                prompt_format=prompt_format,
            ),
            features=FeatureConfig(
                enabled=_boolean(features, "enabled"),
                deduplicate=_boolean(features, "deduplicate"),
                enable_proper=_boolean(features, "enable_proper"),
                auto_fetch_proper=_boolean(features, "auto_fetch_proper"),
                enable_role=_boolean(features, "enable_role"),
                enable_skill=_boolean(features, "enable_skill"),
                enable_concurrent=_boolean(features, "enable_concurrent"),
                enable_self_check=_boolean(features, "enable_self_check"),
                enable_rule_validation=_boolean(features, "enable_rule_validation"),
                enable_thinking=_boolean(features, "enable_thinking"),
                fallback=_boolean(features, "fallback"),
                debug_mode=_boolean(features, "debug_mode"),
                dump=_boolean(features, "dump"),
            ),
            publishing=PublishingConfig(
                zip=publish_zip,
                seven_zip=publish_seven_zip,
                output_dir=output_dir,
                asset_prefix=asset_prefix,
                upload_diagnostics=_boolean(publishing, "upload_diagnostics"),
            ),
        )


def _object(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} 必须是对象")
    return value


def _string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 必须是非空字符串")
    return value.strip()


def _optional_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key, "")
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    return value.strip()


def _string_list(parent: dict[str, Any], key: str) -> list[str]:
    value = parent.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} 必须是字符串数组")
    return [item.strip().lower() for item in value]


def _boolean(parent: dict[str, Any], key: str) -> bool:
    value = parent.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是布尔值")
    return value


def _integer(
    parent: dict[str, Any], key: str, *, minimum: int, maximum: int
) -> int:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{key} 必须位于 {minimum} 到 {maximum} 之间")
    return value


def _positive_number(parent: dict[str, Any], key: str) -> float:
    value = parent.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{key} 必须是正数")
    return float(value)


def _choice(parent: dict[str, Any], key: str, choices: set[str]) -> str:
    value = _string(parent, key)
    if value not in choices:
        raise ConfigError(f"{key} 必须是以下值之一: {', '.join(sorted(choices))}")
    return value


def _url(parent: dict[str, Any], key: str) -> str:
    value = _string(parent, key)
    _validate_url(value, key)
    return value


def _validate_url(value: str, key: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(f"{key} 必须是有效的 HTTPS URL")


def _safe_name(parent: dict[str, Any], key: str) -> str:
    value = _string(parent, key)
    if Path(value).name != value or value in {".", ".."}:
        raise ConfigError(f"{key} 只能是单个安全文件名")
    return value
