"""集中配置：路径、语言、文本判定正则、证据字段。

只读本文件不触碰网络；所有路径基于 project_root() 解析，便于从任意 cwd 调用。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ---- 身份 ----
ARCHIVE_REPO = "HZBHZB1234/LimbusLocalizeAchieve"
ARCHIVE_URL_DIRECT = "https://github.com/HZBHZB1234/LimbusLocalizeAchieve.git"
ARCHIVE_URL_PROXY = "https://gh-proxy.org/https://github.com/HZBHZB1234/LimbusLocalizeAchieve.git"
FALLBACK_MIRROR = "LocalizeLimbusCompany"  # 既有快照（无法刷新时沿用并标注日期）
MIRROR_DIR = "LimbusLocalizeAchieve"        # 归档镜像本地目录名

# ---- 语言 ----
LANGS = ("KR", "JP", "EN", "LLC_zh-CN")
ZH = "LLC_zh-CN"

STATE_TRANSLATION = ".github/state/translation_state.json"
STATE_RAW = ".github/state/raw_state.json"
VERSION_FILE = "LLC_zh-CN/Info/version.json"

# ---- 文本判定（供 align / preflight 复用，口径与 _diag_analysis 一致）----
CJK = re.compile(r"[\u4e00-\u9fff]")
HANGUL = re.compile(r"[\uac00-\ud7a3]")
KANA = re.compile(r"[\u3040-\u30ff]")
KATAKANA = re.compile(r"[\u30a0-\u30ff]{3,}")
LATIN = re.compile(r"[A-Za-z]")
PLACEHOLDER = re.compile(r"\{[^{}]{0,20}\}|<[^<>]{0,20}>|\[[^\[\]]{0,30}\]|%[sd]|\\n")
CAP_GROUP = re.compile(
    r"\b(?:[A-Z][a-z]{2,})(?:\s+(?:of|the|and)\s+[A-Z][a-z]{2,}|\s+[A-Z][a-z]{2,})*"
)
STOP_CAP = {
    "The", "This", "That", "When", "While", "After", "Before", "Each", "Every",
    "Deal", "Gain", "Lose", "Inflict", "Target", "Random", "All", "One", "Two", "Three",
    "If", "For", "With", "From", "Into", "Upon", "On", "At", "In", "To", "By",
}
NAME_KEYS = {"name", "teller", "title", "keyword", "summary"}
NAME_FIELD_CANDIDATES = ("name", "keyword", "title")
NAME_FIELDS = NAME_FIELD_CANDIDATES

# commit 主题前缀（archive 的自动化提交）
SYNC_SUBJECT = "Auto Translation Sync"
RAW_SUBJECT = "Auto RAW Update"
GTP_SUBJECT = "Auto GTP Update"

# 基线实测（docs/2026-11-15-glossary-selfbuild-plan.md §2.1 / §11），用于同量级校验
BASELINE_FILE_ALIGN_RATE = 0.987   # 2036 / 2063
BASELINE_FIELD_ALIGN_RATE = 0.9999  # ≈99.99%


def project_root() -> Path:
    """workspace 根目录（向上查找含 src/translateFunc 的目录）。可用 DSH_WORKSPACE 覆盖。"""
    env = os.environ.get("DSH_WORKSPACE")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "translateFunc").is_dir():
            return parent
    return here.parents[2]


PROJECT_ROOT = project_root()


def artifact_dir(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / "docs" / "termdb"


ARTIFACT_DIR = artifact_dir()


def preflight_dir(root: Path | None = None) -> Path:
    return artifact_dir(root) / "preflight"


def mirror_dir(root: Path | None = None) -> Path:
    return (root or PROJECT_ROOT) / MIRROR_DIR


def default_mirror(root: Path | None = None) -> Path:
    """优先归档镜像，不存在则既有快照。"""
    root = root or PROJECT_ROOT
    m = mirror_dir(root)
    return m if (m / ".git").exists() else root / FALLBACK_MIRROR


def snapshot_manifest_path(root: Path | None = None) -> Path:
    return artifact_dir(root) / "snapshot_manifest.json"


def alignment_report_path(root: Path | None = None) -> Path:
    return artifact_dir(root) / "alignment_report.json"


def derived_dir(mirror: Path) -> Path:
    """派生产物（对齐语料等）落在镜像内的 _derived，随镜像一起 gitignore。"""
    return mirror / "_derived"


def aligned_units_path(mirror: Path) -> Path:
    return derived_dir(mirror) / "aligned_units.jsonl"


def file_kind(rel: str) -> str:
    """沿用 _diag_analysis/glossary_feasibility.py 的域分类。

    入口先归一化路径分隔符：调用方可能传 Windows 风格（``StoryData\\x.json``）
    或 ``Path`` 的字符串形式，未归一化会让 ``startswith("StoryData/")`` 失配，
    使剧情文件被误判成 ui_or_data（运行时 scope 与回流口径随即分叉）。
    """
    rel = str(rel or "").replace("\\", "/")
    base = rel.split("/")[-1]
    if rel.startswith("StoryData/") or base.startswith(("AbDlg", "Story")):
        return "story"
    if base.startswith(("Skills", "Passives", "Bufs", "SkillTag", "BattleKeywords")):
        return "mechanic"
    if base.startswith(("PersonalityVoiceDlg", "EGOVoiceDig", "BattleAnnouncerDlg", "BgmLyrics")):
        return "voice"
    return "ui_or_data"


def is_name_path(path: tuple) -> bool:
    keys = {k for k in path if k != "#"}
    return bool(keys & NAME_KEYS)


def json_dumps(data) -> str:
    """确定性 JSON 序列化：不转义中文、稳定键序、末尾换行。"""
    import json
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
