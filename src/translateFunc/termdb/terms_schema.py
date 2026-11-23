"""term-v1 词条 schema、枚举与确定性辅助函数。

设计口径：
  * 权威富格式沿用 docs/lcta-v2-plan/04-authority-and-terms.md 的字段名
    （entity_id / sense_id / revision / status / forms / zh / scope /
     evidence_ids / source_binding / provenance / supersedes_revision）；
  * 自维护机制额外挂 score / state / tier / components / landing 字段；
  * 扁平兼容视图（proper_terms.json）只暴露运行时认识的字段，见 terms_build.py。

本模块纯函数、零 IO、零模型调用。
"""
from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = 1

# ---- 档位（score 决定"怎么用"，不决定"记不记录"）----
TIER_HARD = "hard"          # score >= 0.80：进强制区，译文必须落位
TIER_SOFT = "soft"          # 0.55 <= score < 0.80：只进提示区
TIER_CANDIDATE = "candidate"  # score < 0.55：只在报告/候选池，不进翻译请求
TIERS = (TIER_HARD, TIER_SOFT, TIER_CANDIDATE)
HARD_MIN = 0.80
SOFT_MIN = 0.55

# ---- 生命周期状态 ----
STATE_CANDIDATE = "candidate"
STATE_PROVISIONAL = "provisional"
STATE_ACTIVE = "active"
STATE_DEPRECATED = "deprecated"
STATE_CONFLICT = "conflict"
STATES = (
    STATE_CANDIDATE, STATE_PROVISIONAL, STATE_ACTIVE,
    STATE_DEPRECATED, STATE_CONFLICT,
)

# ---- 04 规范的权威状态（富格式 status 字段）----
AUTHORITATIVE = "authoritative"
PENDING = "pending"
UPSTREAM_UNRELEASED = "upstream_unreleased"
RETIRED = "retired"
STATUS_CONFLICT = "conflict"
AUTHORITY_STATUSES = (
    AUTHORITATIVE, PENDING, UPSTREAM_UNRELEASED, RETIRED, STATUS_CONFLICT,
)

# ---- 作用域匹配强度 ----
MATCH_ENTITY_BOUND = "entity_bound"  # 绑定实体 ID，最硬
MATCH_NAME_ONLY = "name_only"        # 仅名称字段；单体词/高频碰撞词强制
MATCH_UNBOUND = "unbound"            # 正文浮动
MATCH_EXACT_SENTENCE = "exact_sentence"  # 官方整句/固定措辞，绑定字段+实体
MATCHES = (MATCH_ENTITY_BOUND, MATCH_NAME_ONLY, MATCH_UNBOUND, MATCH_EXACT_SENTENCE)

# ---- 落位四态 ----
LANDING_NONE = "none"        # 源文未命中该术语，无信号
LANDING_APPLIED = "applied"  # 译文中出现期望中文名
LANDING_PARTIAL = "partial"  # 出现部分/变体
LANDING_MISSING = "missing"  # 源文命中但译文缺失（强制档失败）
LANDINGS = (LANDING_NONE, LANDING_APPLIED, LANDING_PARTIAL, LANDING_MISSING)

# ---- 评分参数（plan §4 F1）----
W_STRUCT = 0.40
W_CONSENSUS = 0.30
W_FRESH = 0.15
W_PLACE = 0.15
FRESH_LAMBDA = 6.0          # λ ≈ 6 个 release 版本
PLACE_INIT = 0.5            # 中性初始落位率
PLACE_STEP = 0.05           # 每次落位校验 ±0.05
MAX_CONSECUTIVE_FAILS = 3   # 连续 3 次强制档失败 → conflict

# 谚文音节（用于单音节判定）
_HANGUL_SYLLABLE = re.compile(r"^[\uac00-\ud7a3]$")


def is_single_syllable(kr: str) -> bool:
    """单音节韩文词：必须强制 name_only，且不得进入硬替换档。"""
    return isinstance(kr, str) and bool(_HANGUL_SYLLABLE.match(kr.strip()))


def norm_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def sense_from_field(field: Any) -> str:
    """把名称字段名映射为 sense_id（保持字段名，便于证据回溯）。"""
    f = norm_text(field)
    return f or "name"


def entity_key(entity_id: Any, sense_id: Any, revision: int = 1) -> str:
    """SQLite 主键 = (entity_id, sense_id)， revision 单独存列。"""
    return f"{norm_text(entity_id)}|{norm_text(sense_id)}"


def evidence_id(file: Any, path: Any, field: Any, blob: Any) -> str:
    """证据引用：llc-asset:<sha256>:<file>:<path>:<field>。"""
    return (
        f"llc-asset:{norm_text(blob) or 'noblob'}:"
        f"{norm_text(file) or 'nofile'}:{norm_text(path) or 'nopath'}:"
        f"{norm_text(field) or 'nofield'}"
    )


def make_term(
    *,
    entity_id: Any,
    sense_id: Any,
    revision: int,
    kr: str,
    zh: str,
    jp: str = "",
    en: str = "",
    kind: str = "",
    domains: list[str] | None = None,
    match: str = MATCH_UNBOUND,
    evidence: list[str] | None = None,
    llc_tag: str = "",
    commit_sha: str = "",
    raw_token: str = "",
    blob_kr: str = "",
    blob_zh: str = "",
    status: str = AUTHORITATIVE,
    stale: bool = False,
    supersedes_revision: int | None = None,
) -> dict:
    """构造一条 term-v1 富格式词条（仅结构，评分/状态由 terms_score 填）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "entity_id": norm_text(entity_id),
        "sense_id": norm_text(sense_id),
        "revision": int(revision),
        "status": status,
        "forms": {
            "kr": [kr] if kr else [],
            "jp": [jp] if jp else [],
            "en": [en] if en else [],
        },
        "zh": {"preferred": zh, "allowed_variants": []},
        "scope": {
            "domains": list(domains or ([kind] if kind else [])),
            "entity_refs": [norm_text(entity_id)] if entity_id else [],
            "fields": [norm_text(sense_id)] if sense_id else [],
            "match": match if match in MATCHES else MATCH_UNBOUND,
            "exclude_unit_ids": [],
        },
        "application": "exact_name_contextual_prose",
        "evidence_ids": list(evidence or []),
        "source_binding": {
            "alignment_id": f"{commit_sha}:{raw_token}" if (commit_sha or raw_token) else "",
            "source_fingerprint": blob_kr,
            "llc_tag": llc_tag,
            "raw_token": raw_token,
            "commit_sha": commit_sha,
        },
        "provenance": {
            "kind": "structural_name",
            "commit": commit_sha or None,
            "asset_sha256": blob_zh,
        },
        "supersedes_revision": supersedes_revision,
        "stale": bool(stale),
    }
