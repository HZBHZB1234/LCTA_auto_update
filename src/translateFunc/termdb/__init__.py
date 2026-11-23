"""translateFunc.termdb — 官方汉化语料固化 / 四语对齐 / 术语库前置步骤。

P0（语料固化与对齐）交付：
  - refresh   : 以 HZBHZB1234/LimbusLocalizeAchieve 刷新本地镜像（失败沿用旧快照并标注日期）
  - snapshot  : 按 (LLC tag, raw token, commit SHA, blob sha256) 固化快照清单
  - align     : 四语字段对齐 + stale 标记 + utf-8-sig/BOM 保真 + 对齐报告
  - preflight : 工程化 _diag_analysis/glossary_* 五个脚本为前置步骤

统一入口： python -m translateFunc.termdb all
子命令：   python -m translateFunc.termdb {refresh,snapshot,align,preflight,verify,all}
"""

from .config import (  # noqa: F401
    LANGS,
    PROJECT_ROOT,
    ARTIFACT_DIR,
    MIRROR_DIR,
    snapshot_manifest_path,
    alignment_report_path,
    preflight_dir,
)

__all__ = [
    "LANGS",
    "PROJECT_ROOT",
    "ARTIFACT_DIR",
    "MIRROR_DIR",
    "snapshot_manifest_path",
    "alignment_report_path",
    "preflight_dir",
]
