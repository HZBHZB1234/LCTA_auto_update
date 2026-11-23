"""
translateFunc/matcher/engine.py
MatcherEngine —— 管理全部四个 AC 自动机实例，提供统一匹配接口。
"""
from __future__ import annotations
from dataclasses import dataclass, field
import threading

from translateFunc.matcher.ac_automaton import AcAutomaton, ACPattern

# 名称类字段：scope.match=name_only 只允许在这些字段命中
NAME_FIELDS = frozenset({"name", "teller", "title", "keyword", "summary"})

# 「短词面」阈值。域门只拦 ≤2 字纯韩文（기사/두려움/흰색 这类同形高危词），
# 3 字及以上即便跨域也放行——가시춘(贾惜春) 这类人名常挂在 ui_or_data，
# 若按域硬拦，剧情正文就永远拿不到官方译名（审查实测 M1/M2 同类误杀）。
DOMAIN_GATE_MAX_LEN = 2
# 软档短词面不进非名称字段的上限（比域门宽一档：검정색/노란색 等颜色标签
# 是 3 字软词，必须挡住，否则「검정색 연기」会被提示成「哀叹」）。
SOFT_PROSE_MAX_LEN = 3


def _is_hangul(ch: str) -> bool:
    code = ord(ch)
    return (
        0xAC00 <= code <= 0xD7A3
        or 0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
    )


def is_pure_hangul(text: str) -> bool:
    return bool(text) and all(_is_hangul(ch) for ch in text)


def is_short_surface(text: str, max_len: int) -> bool:
    """纯韩文且不超过 max_len 的词面——同形碰撞的高危区。"""
    return bool(text) and len(text) <= max_len and is_pure_hangul(text)


def scope_allows(item: dict, *, domain: str = "", field: str = "") -> bool:
    """判断词条作用域是否允许在当前 (domain, field) 命中。

    术语库的 scope 是元数据，历史上从未在运行时执行，导致字段/域受限词条
    （如仅 title 的 기사→司机）泄漏到正文。这里统一收口：

      * domains：**只对 ≤2 字纯韩文词面硬拦**。长词面同形歧义低，若也硬拦，
        会让 95% 非 story 域的人名在剧情正文里彻底失效（格里高尔/猩红视线）；
      * match=name_only：只允许名称类字段；
      * match=entity_bound/exact_sentence：有 fields 时按字段强制；
      * match=unbound：软档短词面（≤3）不进非名称字段。
    """
    scope = item.get("scope") or {}
    if not isinstance(scope, dict) or not scope:
        return True

    match = str(scope.get("match") or "")
    domains = [str(x) for x in (scope.get("domains") or []) if x]
    fields = [str(x) for x in (scope.get("fields") or []) if x]
    kr = str(item.get("term") or item.get("kr") or "")

    if (
        domain and domains and domain not in domains
        and is_short_surface(kr, DOMAIN_GATE_MAX_LEN)
    ):
        return False
    if match == "name_only":
        return field in NAME_FIELDS if field else True
    if match in ("entity_bound", "exact_sentence"):
        return (not fields) or (not field) or (field in fields)
    if match == "unbound" and field and field not in NAME_FIELDS:
        tier = str(item.get("tier", "hard"))
        if tier == "soft" and is_short_surface(kr, SOFT_PROSE_MAX_LEN):
            return False
    return True


def _filter_by_scope(matches: list[ACPattern], domain: str, field: str) -> list[ACPattern]:
    if not domain and not field:
        return matches
    return [
        m for m in matches
        if not isinstance(m.data, dict) or scope_allows(m.data, domain=domain, field=field)
    ]


@dataclass
class MatchResult:
    """同时对文本运行全部匹配器的聚合结果。"""
    proper_matches: list[ACPattern] = field(default_factory=list)
    role_matches: list[ACPattern] = field(default_factory=list)
    affect_id_matches: list[ACPattern] = field(default_factory=list)
    affect_name_matches: list[ACPattern] = field(default_factory=list)

    @property
    def has_any(self) -> bool:
        return bool(self.proper_matches or self.role_matches
                    or self.affect_id_matches or self.affect_name_matches)


class MatcherEngine:
    """统一匹配引擎，管理四个 AC 自动机：
    专有名词、角色、状态效果 ID（如 [Combustion]）、状态效果名称（如 '燃烧 '）。
    """

    def __init__(self):
        self._proper_ac = AcAutomaton()
        self._role_ac = AcAutomaton()
        self._affect_id_ac = AcAutomaton()
        self._affect_name_ac = AcAutomaton()

        self._role_data: list[dict] = []
        self._affect_data: list[dict] = []
        # 专有名词原文表（term -> 词条），供热更新时整体重建 AC 自动机
        self._proper_data: dict[str, dict] = {}
        self._proper_lock = threading.RLock()

        # 确保所有 AC 自动机在翻译优先文件前已处于已构建状态，
        # 后续通过 _update_roles / _update_affects 用实际数据重建。
        self._proper_ac.build()
        self._role_ac.build()
        self._affect_id_ac.build()
        self._affect_name_ac.build()

    # ----- 构建 -----

    def build_proper(self, proper_terms: list[dict]) -> None:
        """从 [{term, translation, note, ...}, ...] 构建专有名词 AC 自动机。

        收口规则：candidate 不进 AC；单字词面不进 AC（如 ``나``→我、``부``→富
        会在任何正文里误命中，是术语噪声的最大来源之一）。
        """
        with self._proper_lock:
            self._proper_data = {}
            for item in proper_terms:
                # candidate 只在候选池，绝不进 AC 匹配（防御性收口）
                if str(item.get("tier", "hard")) == "candidate":
                    continue
                term = item.get("term", "")
                if term and len(term) < 2:
                    continue
                if term and term not in self._proper_data:
                    self._proper_data[term] = item
            self._proper_ac = self._build_proper_ac(self._proper_data)

    @staticmethod
    def _build_proper_ac(terms: dict[str, dict]) -> AcAutomaton:
        ac = AcAutomaton()
        for term, item in terms.items():
            ac.add_pattern(term, data=item)
        ac.build()
        return ac

    def add_proper_terms(self, items: list[dict]) -> int:
        """热更新：把模型新发现的专有名词并入术语表，返回新增条数。

        重建后的自动机以**原子替换引用**的方式生效（``self._proper_ac = new``），
        并发中的 ``match_proper`` / ``match_all`` 要么看到旧对象、要么看到新对象，
        因此搜索侧无需加锁。
        """
        with self._proper_lock:
            added = 0
            for item in items or []:
                # 与 build_proper 同口径：candidate 与单字词面都不进自动机
                if str(item.get("tier", "hard")) == "candidate":
                    continue
                term = (item.get("term") or "").strip()
                if term and len(term) < 2:
                    continue
                if term and term not in self._proper_data:
                    self._proper_data[term] = item
                    added += 1
            if added:
                self._proper_ac = self._build_proper_ac(self._proper_data)
        return added

    @property
    def proper_terms(self) -> dict[str, dict]:
        """当前术语表快照（term -> 词条）。"""
        with self._proper_lock:
            return dict(self._proper_data)

    def proper_terms_by_tier(self, tier: str = "hard") -> dict[str, dict]:
        """按 tier 取快照；旧词条无 tier 字段时视为 hard（向后兼容）。"""
        return {k: v for k, v in self.proper_terms.items()
                if str(v.get("tier", "hard")) == tier}

    def landing_terms(self) -> dict[str, dict]:
        """可进翻译术语区的词条（hard + soft）。

        candidate 档（模型新词 / L2 / L3）只在候选池，不进提示词强制区。
        旧词条无 tier 字段时默认 hard，保持旧行为。
        """
        return {k: v for k, v in self.proper_terms.items()
                if str(v.get("tier", "hard")) in ("hard", "soft")}

    def build_roles(self, role_items: list[dict]) -> None:
        """从 [{id, kr, cn, nickName}, ...] 构建角色 AC 自动机。
        角色通过 `id` 字段精确匹配，非子串匹配。"""
        self._role_data = role_items
        self._role_by_id_cache = None  # 清除缓存
        self._role_ac = AcAutomaton()
        for item in role_items:
            role_id = item.get("id", "")
            if role_id:
                self._role_ac.add_pattern(role_id, data=item)
        self._role_ac.build()

    def build_affects(self, affect_items: list[dict]) -> None:
        """从 [{id, kr, jp, en, cn, desc}, ...] 构建状态效果匹配器。"""
        self._affect_data = affect_items
        self._affect_id_ac = AcAutomaton()
        self._affect_name_ac = AcAutomaton()
        for item in affect_items:
            aff_id = f'[{item.get("id", "")}]'
            aff_name = f'{item.get("kr", "")} '
            if item.get("id"):
                self._affect_id_ac.add_pattern(aff_id, data=item)
            if item.get("kr"):
                self._affect_name_ac.add_pattern(aff_name, data=item)
        self._affect_id_ac.build()
        self._affect_name_ac.build()

    # ----- 匹配 -----

    def match_all(
        self,
        text: str,
        *,
        jp_text: str = "",
        en_text: str = "",
        domain: str = "",
        field: str = "",
    ) -> MatchResult:
        """对文本运行全部匹配器，并用 JP/EN 参考过滤韩文名称误匹配。

        ``domain`` / ``field`` 用于执行词条作用域（scope）：不满足的词条
        不会进入 proper_refs，从源头阻断跨域/跨字段的术语误用。
        """
        affect_name_matches = [
            match for match in self._affect_name_ac.search(text)
            if self._is_affect_name_supported(match.data, text, jp_text, en_text)
        ]
        return MatchResult(
            proper_matches=_filter_by_scope(
                self._proper_ac.search_non_overlapping(text), domain, field,
            ),
            role_matches=self._role_ac.search(text),
            affect_id_matches=self._affect_id_ac.search(text),
            affect_name_matches=affect_name_matches,
        )

    @staticmethod
    def _is_affect_name_supported(
        affect_data: object,
        kr_text: str,
        jp_text: str,
        en_text: str,
    ) -> bool:
        """有可用 JP/EN 对照时，要求至少一种语言同时出现对应效果名。"""
        if not isinstance(affect_data, dict):
            return True

        comparisons: list[bool] = []
        jp_name = str(affect_data.get("jp", "") or "").strip()
        if jp_name and jp_text and jp_text != kr_text:
            comparisons.append(jp_name in jp_text)

        en_name = str(affect_data.get("en", "") or "").strip()
        if en_name and en_text and en_text != kr_text:
            comparisons.append(en_name.casefold() in en_text.casefold())

        return any(comparisons) if comparisons else True

    def match_proper(
        self, text: str, *, domain: str = "", field: str = "",
    ) -> list[ACPattern]:
        """仅匹配专有名词（最长匹配优先 + 作用域过滤）。"""
        return _filter_by_scope(
            self._proper_ac.search_non_overlapping(text), domain, field,
        )

    # ----- 访问器 -----

    @property
    def role_data(self) -> list[dict]:
        return self._role_data

    @property
    def affect_data(self) -> list[dict]:
        return self._affect_data

    @property
    def role_by_id(self) -> dict[str, dict]:
        """以角色 ID 为键的 O(1) 查找表。"""
        if not hasattr(self, "_role_by_id_cache") or self._role_by_id_cache is None:
            self._role_by_id_cache = {r.get("id", ""): r for r in self._role_data}
        return self._role_by_id_cache
