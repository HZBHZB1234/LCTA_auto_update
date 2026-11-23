"""翻译后自动落位回流闭环（零人工 CLI）。

链路：已保存译文 + KR 源文 → TermLandingValidator 四态观测 →
TermStore.apply_landing 状态机回流 → （版本变化时）S_fresh 衰减 →
JSONL / proper_terms.json 重导出。全程无人工审批、无手动命令。

任何一步失败都只记日志；本模块是翻译附加产出，绝不阻断翻译主流程。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import config, terms_build
from .lib import flatten, read_json
from .terms_store import DB_NAME, TermStore

_logger = logging.getLogger("LCTA")

# 同一术语在多个文件里的观测聚合优先级：applied > partial > missing
_RANK = {"applied": 0, "partial": 1, "missing": 2}


def pair_blocks_translations(
    kr_doc: Any, zh_doc: Any, *, rel: str = "", domain: str = "",
) -> tuple[list[dict], list[str]]:
    """按结构路径把 KR 源文与译文配成 (blocks, translations)。

    块上带 ``field`` / ``domain``：落位校验要与运行时同口径执行 scope，
    否则域/字段受限词条会被误判落位。
    """
    kr_flat = flatten(kr_doc)
    zh_flat = flatten(zh_doc)
    blocks: list[dict] = []
    translations: list[str] = []
    for path, kr_text in kr_flat.items():
        if path not in zh_flat:
            continue
        zh_text = zh_flat[path]
        if not isinstance(kr_text, str) or not isinstance(zh_text, str):
            continue
        if not kr_text.strip():
            continue
        field = path[-1] if path and isinstance(path[-1], str) else ""
        blocks.append({
            "kr": kr_text,
            "path": ".".join(str(x) for x in path),
            "field": field,
            "domain": domain,
        })
        translations.append(zh_text)
    return blocks, translations


def collect_landing_records(
    pairs: list[tuple[list[dict], list[str]]],
    proper_terms: list[dict],
) -> list[dict]:
    """跑落位校验并把多文件观测按 term_key 聚合成单次回流记录。"""
    from translateFunc.validator import TermLandingValidator  # noqa: WPS433

    validator = TermLandingValidator(proper_terms)
    best: dict[str, str] = {}
    notes: dict[str, str] = {}
    for blocks, translations in pairs:
        if not blocks:
            continue
        report = validator.check(blocks, translations)
        for obs in report.observations:
            if obs.landing == "none":
                continue
            prev = best.get(obs.term_key)
            if prev is None or _RANK[obs.landing] < _RANK[prev]:
                best[obs.term_key] = obs.landing
                notes[obs.term_key] = obs.note
    return [
        {"term_key": k, "landing": v, "note": notes.get(k, "auto-pipeline")}
        for k, v in sorted(best.items())
    ]


def _snapshot_tag(root: Path) -> str:
    try:
        snap = config.snapshot_manifest_path(root)
        if snap.is_file():
            return str(read_json(snap).get("identity", {}).get("llc_tag", ""))
    except Exception:  # noqa: BLE001
        pass
    return ""


def run_feedback_loop(
    root: Path,
    landing_records: list[dict],
    *,
    version_gap: int | None = None,
) -> dict:
    """把落位记录回流 SQLite、按需衰减并重导出；返回可观测报告。

    **只降不升**：只用 ``missing`` 观测回写（触发失败计数/降档），
    ``applied`` 一律不回写。理由：applied 来自本轮机器译文本身，用它
    正向强化会让碰撞产生的错误译文自我印证，形成闭环自腐蚀。
    """
    root = Path(root).resolve()
    db = config.artifact_dir(root) / DB_NAME
    downgrades = [r for r in landing_records if str(r.get("landing")) in ("missing", "partial")]
    report: dict[str, Any] = {
        "landing_records": len(landing_records),
        "downgrade_records": len(downgrades),
        "applied_skipped": len(landing_records) - len(downgrades),
        "applied": 0,
        "state_changes": 0, "version_gap": 0, "state_change_detail": [],
    }
    with TermStore(db) as store:
        for rec in downgrades:
            result = terms_build.apply_landing(
                store, rec.get("term_key", ""), rec.get("landing", ""),
                rec.get("note", "auto-pipeline"),
            )
            if result.get("found"):
                report["applied"] += 1
            if result.get("changed"):
                report["state_changes"] += 1
                report["state_change_detail"].append({
                    "term_key": result["term_key"], "state": result["state"],
                })

        # 版本前进时自动衰减；gap=0 绝不调用，否则会把历史 freshness 错误地重置为 1.0
        gap = version_gap
        if gap is None:
            tag = _snapshot_tag(root)
            last = str(store.get_meta("last_build", {}).get("llc_tag", ""))
            gap = 1 if (tag and last and tag != last) else 0
        gap = max(0, int(gap))
        report["version_gap"] = gap
        if gap > 0:
            report["decay"] = terms_build.advance_versions(store, gap, root)

        terms_build.export_revision_jsonl(store, root)
        terms_build.export_compat(store, root)
        terms_build.write_terms_report(store, root, {
            "last_auto_feedback": {
                "landing_records": len(landing_records),
                "downgrade_records": len(downgrades),
                "applied_skipped": report["applied_skipped"],
                "state_changes": report["state_changes"],
                "version_gap": gap,
            }
        })
        report["flat_count"] = len(terms_build.build_flat_view(store))
    return report


def run_pipeline_feedback(
    root: Path,
    source_output_pairs: list[tuple],
    proper_terms: list[dict],
) -> dict:
    """翻译后入口：读盘配对 → 落位校验 → 回流 → 重导出。

    ``source_output_pairs`` 支持 ``(src, out)`` 或 ``(src, out, rel)``；
    带 ``rel`` 时按语料域执行术语 scope，与运行时同口径。
    """
    pairs: list[tuple[list[dict], list[str]]] = []
    for item in source_output_pairs:
        src, out = item[0], item[1]
        rel = str(item[2]) if len(item) > 2 else ""
        try:
            kr_doc = json.loads(src.read_text(encoding="utf-8-sig"))
            zh_doc = json.loads(out.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001 - 单文件失败不影响整体
            _logger.debug(f"落位回流跳过 {src}: {exc}")
            continue
        domain = config.file_kind(rel) if rel else ""
        pairs.append(pair_blocks_translations(kr_doc, zh_doc, rel=rel, domain=domain))
    records = collect_landing_records(pairs, proper_terms)
    if not records:
        return {"landing_records": 0, "skipped": True}
    result = run_feedback_loop(root, records)
    _logger.info(
        f"落位自动回流：观测 {result['landing_records']} 条，"
        f"状态变更 {result['state_changes']} 条，"
        f"兼容视图重导出 {result.get('flat_count', 0)} 条"
    )
    return result
