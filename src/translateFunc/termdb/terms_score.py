"""浮动评分与状态机（plan §4 F1/F2，零模型、零人工审批）。

覆盖四种自动矫正：
  1. 落位四态回流 S_place（applied/partial/missing/none）；
  2. 连续 3 次强制档落位失败 → 自动降级 conflict；
  3. 新版官方名称证据确认 → 同一 revision 链复活、S_fresh 浮回 1.0；
  4. 版本距离衰减 → 掉出强制档（soft → deprecated）。

纯逻辑模块，不读写文件、不访问存档。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .terms_schema import (
    FRESH_LAMBDA, HARD_MIN, LANDING_APPLIED, LANDING_MISSING,
    LANDING_NONE, LANDING_PARTIAL, MAX_CONSECUTIVE_FAILS, MATCHES,
    MATCH_NAME_ONLY, MATCH_UNBOUND, PLACE_INIT, PLACE_STEP, SOFT_MIN,
    STATE_ACTIVE, STATE_CANDIDATE, STATE_CONFLICT, STATE_DEPRECATED,
    STATE_PROVISIONAL, TIER_CANDIDATE, TIER_HARD, TIER_SOFT, W_CONSENSUS,
    W_FRESH, W_PLACE, W_STRUCT,
)


@dataclass
class ScoreComponents:
    """四项加权分量，均落在 [0,1]。"""

    struct: float = 0.0
    consensus: float = 0.0
    fresh: float = 1.0
    place: float = PLACE_INIT

    def as_dict(self) -> dict[str, float]:
        return {
            "struct": round(self.struct, 6),
            "consensus": round(self.consensus, 6),
            "fresh": round(self.fresh, 6),
            "place": round(self.place, 6),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ScoreComponents":
        d = data or {}
        return cls(
            struct=float(d.get("struct", 0.0)),
            consensus=float(d.get("consensus", 0.0)),
            fresh=float(d.get("fresh", 1.0)),
            place=float(d.get("place", PLACE_INIT)),
        )


def compute_score(c: ScoreComponents) -> float:
    """score = 0.40·S_struct + 0.30·S_consensus + 0.15·S_fresh + 0.15·S_place。"""
    raw = (W_STRUCT * c.struct + W_CONSENSUS * c.consensus
           + W_FRESH * c.fresh + W_PLACE * c.place)
    return round(min(1.0, max(0.0, raw)), 6)


def freshness(version_gap: int, lam: float = FRESH_LAMBDA) -> float:
    """S_fresh = exp(-Δversions / λ)；gap <= 0 视为当前版本 = 1.0。"""
    if version_gap <= 0:
        return 1.0
    return round(math.exp(-float(version_gap) / float(lam)), 6)


def tier_for(
    score: float,
    *,
    state: str = STATE_PROVISIONAL,
    stale: bool = False,
    match: str = MATCH_UNBOUND,
) -> str:
    """档位判定：hard 需要 active + 非 stale + 非 name_only + score >= 0.80。

    这是 stale/单体词"不硬替换"的唯一收口点，所有导出都经过它。
    """
    if state in (STATE_CONFLICT, STATE_DEPRECATED):
        return TIER_CANDIDATE
    if (
        state == STATE_ACTIVE
        and not stale
        and match != MATCH_NAME_ONLY
        and score >= HARD_MIN
    ):
        return TIER_HARD
    if score >= SOFT_MIN:
        return TIER_SOFT
    return TIER_CANDIDATE


@dataclass
class TermLifecycle:
    """单条词条的浮动状态容器；所有转移都是纯函数式返回新值或原地受控更新。"""

    state: str = STATE_CANDIDATE
    components: ScoreComponents = field(default_factory=ScoreComponents)
    stale: bool = False
    match: str = MATCH_UNBOUND
    consecutive_fail: int = 0
    version_gap: int = 0
    revision: int = 1
    supersedes_revision: int | None = None

    def __post_init__(self) -> None:
        # 只拒绝 schema 枚举之外的 match 值，保持 lifecycle.match 与
        # doc.scope.match 一致。MATCH_EXACT_SENTENCE（官方整句/固定措辞，
        # t13 引入）与 MATCH_ENTITY_BOUND 都是合法枚举，不得在这里被
        # 静默降级成 unbound，否则官方整句会丢掉字段级绑定语义。
        # 单音节仍由构建侧强制 name_only（tier_for 的唯一收口点）。
        if self.match not in MATCHES:
            self.match = MATCH_UNBOUND

    # ---- 派生量 ----

    @property
    def score(self) -> float:
        return compute_score(self.components)

    @property
    def tier(self) -> str:
        return tier_for(self.score, state=self.state,
                        stale=self.stale, match=self.match)

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tier": self.tier,
            "score": self.score,
            "components": self.components.as_dict(),
            "stale": self.stale,
            "match": self.match,
            "consecutive_fail": self.consecutive_fail,
            "version_gap": self.version_gap,
            "revision": self.revision,
            "supersedes_revision": self.supersedes_revision,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "TermLifecycle":
        x = d or {}
        return cls(
            state=x.get("state", STATE_CANDIDATE),
            components=ScoreComponents.from_dict(x.get("components")),
            stale=bool(x.get("stale", False)),
            match=x.get("match", MATCH_UNBOUND),
            consecutive_fail=int(x.get("consecutive_fail", 0)),
            version_gap=int(x.get("version_gap", 0)),
            revision=int(x.get("revision", 1)),
            supersedes_revision=x.get("supersedes_revision"),
        )

    # ---- 初始化 / 自动提升 ----

    @classmethod
    def from_evidence(
        cls,
        *,
        struct: float,
        consensus: float,
        stale: bool = False,
        match: str = MATCH_UNBOUND,
        version_gap: int = 0,
        revision: int = 1,
    ) -> "TermLifecycle":
        """由证据构造并自动提升状态。

        stale / name_only / conflict 永不进入 active；最终档位由 tier_for 收口。
        """
        c = ScoreComponents(
            struct=struct,
            consensus=consensus,
            fresh=0.0 if stale else freshness(version_gap),
            place=PLACE_INIT,
        )
        life = cls(components=c, stale=stale, match=match,
                   version_gap=version_gap, revision=revision)
        life._auto_promote()
        return life

    def _auto_promote(self) -> None:
        """按 score/证据把 candidate → provisional → active（满足硬条件才 active）。"""
        if self.state in (STATE_CONFLICT, STATE_DEPRECATED):
            return
        if self.score >= SOFT_MIN:
            self.state = STATE_PROVISIONAL
        if (
            self.score >= HARD_MIN
            and not self.stale
            and self.match != MATCH_NAME_ONLY
        ):
            self.state = STATE_ACTIVE

    # ---- 1) 落位四态回流 ----

    def observe_landing(self, landing: str) -> bool:
        """回流一次落位结果，更新 S_place / 连续失败计数 / 状态。

        Returns: True 表示发生了状态转移（需要回写 SQLite / 重导出）。
        """
        if landing not in (LANDING_NONE, LANDING_APPLIED,
                           LANDING_PARTIAL, LANDING_MISSING):
            return False
        if landing == LANDING_NONE:
            # 源文未命中 → 无信号，不动分
            return False

        before_state = self.state
        if landing == LANDING_APPLIED:
            self.components.place = min(1.0, self.components.place + PLACE_STEP)
            self.consecutive_fail = 0
        elif landing == LANDING_PARTIAL:
            # 部分落位：不加不减，但打断连续失败
            self.consecutive_fail = 0
        else:  # MISSING
            self.components.place = max(0.0, self.components.place - PLACE_STEP)
            self.consecutive_fail += 1
            if self.consecutive_fail >= MAX_CONSECUTIVE_FAILS:
                self.state = STATE_CONFLICT  # 2) 连续 3 次失败自动降级

        if self.state == before_state:
            self._auto_promote()
        return self.state != before_state

    # ---- 3) 新版官方名称复活 ----

    def confirm_official_name(self, *, revision: int | None = None) -> bool:
        """新版本名称字段再次确认：同一 revision 链复活，S_fresh 浮回 1.0。"""
        before = self.state
        if revision is not None and revision >= self.revision:
            self.supersedes_revision = self.revision
            self.revision = revision
        self.components.fresh = 1.0
        self.version_gap = 0
        self.consecutive_fail = 0
        # 仅 stale 仍阻止 hard；新证据来自 committed blob，清除 stale 标记
        self.stale = False
        self.state = STATE_PROVISIONAL
        self._auto_promote()
        return self.state != before

    # ---- 4) 版本衰减 ----

    def advance_version(self, version_gap: int) -> bool:
        """快照版本前进：S_fresh 衰减；掉出 hard → provisional，低于 soft → deprecated。"""
        before = self.state
        self.version_gap = max(0, int(version_gap))
        self.components.fresh = freshness(self.version_gap)
        if self.state == STATE_ACTIVE and self.score < HARD_MIN:
            self.state = STATE_PROVISIONAL
        if self.state == STATE_PROVISIONAL and self.score < SOFT_MIN:
            self.state = STATE_DEPRECATED
        return self.state != before
