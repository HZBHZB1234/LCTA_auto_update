"""
translateFunc/validator.py
确定性规则后处理校验器 —— 用于翻译质量验证。

在 Stage 1 翻译完成后、Stage 2 LLM 校验前运行。
处理 LLM 始终无法稳定执行的机械性规则检查：
  - [ID] 格式中误用空格（如 [震颤 ] 应为 [震颤]）
  - 源语言（KR/JP/EN）特殊效果引用在译文中的一致性
"""
from __future__ import annotations
from dataclasses import dataclass, field
import re
import logging
from typing import Any

_logger = logging.getLogger("LCTA")


@dataclass
class ValidationViolation:
    """单条校验违规记录。"""
    block_id: int          # 1-based 文本块索引
    rule: str              # "buff_spacing" | "effect_ref"
    severity: str          # "error" (可自动修复) | "warning" (需人工复核)
    message: str           # 人类可读的描述
    auto_fixable: bool     # 是否可以安全自动修复
    fix_fn: str | None     # "replace" | None
    fix_params: dict | None  # 修复函数的参数


@dataclass
class ValidationReport:
    """一次校验运行的聚合报告。"""
    violations: list[ValidationViolation] = field(default_factory=list)
    auto_fixes_applied: int = 0
    warnings_remaining: int = 0


class RuleBasedValidator:
    """确定性后处理校验器，用于技能文件翻译。

    通过已知的 Buff/状态效果名称数据（affects），执行规则检查：
    - [ID] 格式中是否误用了空格
    - 特殊效果引用是否在译文中完整保留
    """

    # 匹配 [Anything] 格式的括号引用（包含中文名称和英文 ID）
    _BRACKET_REF_RE = re.compile(r'\[([^\]]*)\]')

    # 匹配 [Alphanumeric_ID] 格式的特殊效果引用
    _EFFECT_ID_RE = re.compile(r'\[([A-Za-z][A-Za-z0-9_]*)\]')

    # 匹配 Effect ID 内组模式（不含括号），用于区分 ID 与中文内容
    _ID_LIKE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')

    # 匹配括号内含有中文（CJK）内容的形式，如 [出血]、[震颤 ]，
    # 这类是英文ID被误译为中文后塞回括号，需还原为 [EnglishId]。
    _BRACKETED_CN_RE = re.compile(r'\[([^\]]*[一-鿿][^\]]*)\]')

    # 匹配被编码（HTML 实体 / URL 编码）的游戏富文本标签，如
    # &lt;color=#ff6000&gt; / %3Ccolor=%23ff6000%3E 及其闭合标签。
    # 仅当标签被编码时才匹配，避免误伤正文中的裸 < > 字面量。
    _RICHTEXT_ESCAPE_RE = re.compile(
        r"(?P<open>&lt;|%3C)"
        r"(?P<slash>/?)"
        r"(?P<name>color|mark|b|u|i|s|noparse|size)"
        r"(?P<attrs>(?:[^&<>]|%[0-9A-Fa-f]{2})*?)"
        r"(?P<close>&gt;|%3E)"
    )

    def __init__(self, affects_data: list[dict]):
        """初始化校验器。

        Args:
            affects_data: 状态效果列表，每项含 id/kr/cn/desc 字段。
                          来自 BattleKeywords.json 翻译后的配对数据。
        """
        self._affect_cn_names: set[str] = set()
        self._affect_id_to_cn: dict[str, str] = {}
        self._affect_kr_to_cn: dict[str, str] = {}
        # 反向映射：中文显示名（含尾随空格变体）→ 引擎代码ID，
        # 用于将错误放入括号的中文名还原为 [EnglishId]。
        self._cn_to_id: dict[str, str] = {}
        # scope 明确标为 name_only / fields=["name"] 的词条：尾空格规则豁免
        self._name_field_cn: set[str] = set()

        for a in affects_data:
            cn = a.get("cn", "")
            aid = a.get("id", "")
            kr = a.get("kr", "")
            scope = a.get("scope") or {}
            fields = scope.get("fields") or []
            if cn and (fields == ["name"] or scope.get("match") == "name_only"):
                self._name_field_cn.add(cn)
            if cn:
                self._affect_cn_names.add(cn)
                if aid:
                    self._cn_to_id[cn.strip()] = aid
            if aid and cn:
                self._affect_id_to_cn[aid] = cn
            if kr and cn:
                self._affect_kr_to_cn[kr] = cn

        # 按长度降序排列，确保匹配时长名称优先
        self._sorted_cn_names = sorted(
            self._affect_cn_names, key=len, reverse=True
        )

    # ---- 公开 API ----

    def run_all_checks(
        self,
        text_blocks: list[dict],
        translations: list[str],
    ) -> ValidationReport:
        """运行所有校验，返回聚合报告。"""
        report = ValidationReport()
        report.violations.extend(
            self.validate_buff_spacing(translations)
        )
        report.violations.extend(
            self.validate_effect_refs(text_blocks, translations)
        )
        report.violations.extend(
            self.validate_bracketed_cn_buffs(translations)
        )
        report.violations.extend(
            self.validate_buff_trailing_space(translations, text_blocks)
        )
        report.violations.extend(
            self.validate_cjk_quotes(translations, text_blocks)
        )

        report.warnings_remaining = sum(
            1 for v in report.violations if v.severity == "warning"
        )
        report.auto_fixes_applied = sum(
            1 for v in report.violations
            if v.auto_fixable and v.severity == "error"
        )
        return report

    @staticmethod
    def apply_auto_fixes(
        translations: list[str],
        violations: list[ValidationViolation],
    ) -> list[str]:
        """对 translations 的副本应用所有可自动修复的违规修正。

        按 block 分组，按位置倒序应用修复，以确保多次修改不会
        导致后续修复的位置偏移。

        Returns:
            修正后的翻译文本列表（新列表，不修改原列表）。
        """
        result = list(translations)

        # 按 block_id 分组
        by_block: dict[int, list[ValidationViolation]] = {}
        for v in violations:
            if v.auto_fixable and v.fix_fn:
                by_block.setdefault(v.block_id - 1, []).append(v)

        for idx, violations_for_block in by_block.items():
            if idx < 0 or idx >= len(result):
                continue
            text = result[idx]

            # 按位置倒序排序，保证多次修改位置正确
            sorted_v = sorted(
                violations_for_block,
                key=lambda v: v.fix_params.get("position", 0) if v.fix_params else 0,
                reverse=True,
            )

            for v in sorted_v:
                if v.fix_fn == "replace" and v.fix_params:
                    old = v.fix_params.get("old", "")
                    new = v.fix_params.get("new", "")
                    pos = v.fix_params.get("position", 0)
                    radius = v.fix_params.get("radius", 40)
                    search_start = max(0, pos - radius)
                    search_end = min(len(text), pos + radius)
                    sub = text[search_start:search_end]
                    if old in sub:
                        text = (
                            text[:search_start]
                            + sub.replace(old, new, 1)
                            + text[search_end:]
                        )
                elif v.fix_fn == "insert" and v.fix_params:
                    # 精确位置插入，不靠窗口内字符串匹配（同一状态名可能多次出现）
                    pos = v.fix_params.get("position", 0)
                    ins = v.fix_params.get("text", "")
                    if 0 <= pos <= len(text):
                        text = text[:pos] + ins + text[pos:]
                elif v.fix_fn == "char_replace" and v.fix_params:
                    # 按精确下标替换单字符（引号成对出现在窗口内时不会错换）
                    pos = v.fix_params.get("position", 0)
                    old = v.fix_params.get("old", "")
                    new = v.fix_params.get("new", "")
                    if 0 <= pos < len(text) and text[pos] == old:
                        text = text[:pos] + new + text[pos + len(old):]

            result[idx] = text

        return result

    # ---- 单项校验 ----

    def validate_buff_spacing(
        self, translations: list[str],
    ) -> list[ValidationViolation]:
        """校验 [ID] 格式中是否误用了空格。

        规则：中括号内的 ID 引用不应包含多余空格。
        - ❌ 错误：`[震颤 ]`、`[ 震颤]`、`[ 震颤 ]`
        - ✅ 正确：`震颤 `、`[Combustion]`

        检测 [ ] 内部内容的前导空格和尾部空格，自动修复为尾缀空格版本。

        Returns:
            违规列表，误用空格的标记为可自动修复。
        """
        violations: list[ValidationViolation] = []

        for block_idx, cn_text in enumerate(translations):
            if not cn_text:
                continue

            for m in self._BRACKET_REF_RE.finditer(cn_text):
                inner = m.group(1)
                full = m.group(0)  # 含括号的完整匹配

                # 检查内部内容是否有多余空格
                stripped = inner.strip()
                if stripped == inner:
                    continue  # 无多余空格，正常

                if not stripped:
                    continue  # 空括号 []，跳过

                # 构建修正后的版本：ID 类保留括号，中文类去掉括号
                if self._ID_LIKE_RE.match(stripped):
                    fixed = f"[{stripped}]"
                elif stripped in self._cn_to_id:
                    # 中文显示名被错误放入括号 → 还原为引擎代码 [EnglishId]
                    fixed = f"[{self._cn_to_id[stripped]}]"
                else:
                    fixed = f"{stripped} "
                abs_pos = m.start()
                snippet = cn_text[max(0, abs_pos - 5):min(len(cn_text), m.end() + 5)]

                violations.append(ValidationViolation(
                    block_id=block_idx + 1,
                    rule="buff_spacing",
                    severity="error",
                    message=(
                        f"[ID] 格式误用空格：'{full}' 应为 '{fixed}'"
                        f"（上下文: ...{snippet}...）"
                    ),
                    auto_fixable=True,
                    fix_fn="replace",
                    fix_params={
                        "position": abs_pos,
                        "radius": max(len(full) + 10, 20),
                        "old": full,
                        "new": fixed,
                    },
                ))

        return violations

    def validate_buff_trailing_space(
        self, translations: list[str], text_blocks: list[dict] | None = None,
    ) -> list[ValidationViolation]:
        """检测正文中状态效果中文名后被 strip 的半角空格（零模型规则修复）。

        官方写法要求状态名嵌在 desc/summary/整句中时保留一个半角空格
        （如「对攻击者施加1层烧伤 」、「充能 层数」），即使位于句尾或标点前；
        但整条仅为状态名的 name/title 字段官方无尾空格（如「烧伤」），
        必须豁免，否则会误伤 RG-BURN-NAME。方括号内的引擎 ID 不适用本规则。

        name 字段判定（零模型）：译文全文恰为某个状态名、或原文全文恰为
        某个状态名的 KR 词面；另尊重 affects 条目自带的 scope.fields /
        scope.match 元数据。

        Returns:
            违规列表，全部可通过插入空格自动修复。
        """
        violations: list[ValidationViolation] = []
        for block_idx, cn_text in enumerate(translations):
            if not cn_text:
                continue

            source_kr = ""
            if text_blocks and block_idx < len(text_blocks):
                source_kr = str(text_blocks[block_idx].get("kr", "")).strip()
            # name 字段：整条译文/原文就是单独一个状态名 → 全局豁免本块
            if cn_text.strip() in self._affect_cn_names:
                continue
            if source_kr and source_kr in self._affect_kr_to_cn:
                continue

            bracket_spans = [m.span() for m in self._BRACKET_REF_RE.finditer(cn_text)]
            occupied: list[tuple[int, int]] = []
            for name in self._sorted_cn_names:
                if not name or name in self._name_field_cn:
                    continue  # scope 明确标记为 name_only 的词条
                pattern = re.compile(re.escape(name) + r"(?!\s)")
                for m in pattern.finditer(cn_text):
                    start, end = m.start(), m.end()
                    if any(a <= start < b for a, b in bracket_spans):
                        continue  # [ID] 内部不受尾空格规则约束
                    if any(start < b and a < end for a, b in occupied):
                        continue  # 长名已覆盖，避免重名重复插入
                    occupied.append((start, end))
                    violations.append(ValidationViolation(
                        block_id=block_idx + 1,
                        rule="buff_trailing_space",
                        severity="error",
                        message=f"状态名'{name}'后缺少半角空格（不得 strip）",
                        auto_fixable=True,
                        fix_fn="insert",
                        fix_params={"position": end, "text": " "},
                    ))

        return violations

    # 原文的「…」片段：视为必须逐字保留的原文引用（与收紧后的 P1 例外一致）
    _VERBATIM_QUOTE_RE = re.compile(r"「[^」]*」")

    @staticmethod
    def validate_cjk_quotes(
        translations: list[str],
        text_blocks: list[dict] | None = None,
    ) -> list[ValidationViolation]:
        """把 zh-CN 正文里的日式直角引号「」归一为中文双引号“”（零模型确定性修复）。

        官方语料依据（只读引用）：历史回放 gold hv2026111901-ui-92dccb260f0f
        把 KR 的「적응」译为“适应”、「부적응」译为“不适应”；RG-BAN-CJK-QUOTE
        把 JP 的「これが最後の一撃だ…！」译为“这就是最后一击了……！”。
        原文里的「」只是引用标记，普通正文照搬到「适应」是风格回退，必须归一。

        不触碰（与收紧后的 P1 例外保持一致）：
          * 占位符 / ID / 富文本标签：<0>、{0}、[Bleed]、<color=…> 内部不含「」，
            char_replace 按精确下标替换不会误伤；
          * 必须逐字保留的原文引用：某段「…」若在对应源块(kr/jp/en)中原样出现，
            视为原文引用，整段豁免（如引用块、需原样复现的台词内嵌引文）。
        """
        mapping = {"「": "“", "」": "”"}
        violations: list[ValidationViolation] = []

        # 收集原文里逐字出现的「…」片段作为豁免集合
        verbatim_spans: set[str] = set()
        for block in text_blocks or []:
            for lang in ("kr", "jp", "en"):
                src = str((block or {}).get(lang, "") or "")
                verbatim_spans.update(m.group(0) for m in RuleBasedValidator._VERBATIM_QUOTE_RE.finditer(src))

        for block_idx, cn_text in enumerate(translations):
            # 逐块计算原文引用在译文中的覆盖区间，整段跳过
            exempt_ranges: list[tuple[int, int]] = []
            for span in verbatim_spans:
                start = 0
                while True:
                    idx = cn_text.find(span, start)
                    if idx < 0:
                        break
                    exempt_ranges.append((idx, idx + len(span)))
                    start = idx + 1

            for pos, char in enumerate(cn_text):
                if char not in mapping:
                    continue
                if any(a <= pos < b for a, b in exempt_ranges):
                    continue  # 必须逐字保留的原文引用，不触碰
                violations.append(ValidationViolation(
                    block_id=block_idx + 1,
                    rule="cjk_quote",
                    severity="error",
                    message=f"日式直角引号'{char}'应改为'{mapping[char]}'",
                    auto_fixable=True,
                    fix_fn="char_replace",
                    fix_params={
                        "position": pos, "old": char, "new": mapping[char],
                    },
                ))
        return violations

    def validate_effect_refs(
        self, text_blocks: list[dict], translations: list[str],
    ) -> list[ValidationViolation]:
        """校验源语言中的特殊效果引用在译文中是否完整表达。

        已知效果允许保留原始 [EffectID]，也允许使用术语表中的中文名称；
        未知 ID 无法进行语义映射，因此必须原样保留。

        此类违规无法可靠自动修复，标记为 warning。

        Returns:
            违规列表，均为 auto_fixable=False。
        """
        violations: list[ValidationViolation] = []

        for block_idx, (block, cn_text) in enumerate(
            zip(text_blocks, translations)
        ):
            if not cn_text:
                continue

            # 1. 收集源语言（KR/JP/EN）中的所有 [EffectID]
            source_ids: set[str] = set()
            for lang in ("kr", "jp", "en"):
                text = block.get(lang, "")
                if text:
                    for m in self._EFFECT_ID_RE.finditer(text):
                        source_ids.add(m.group(1))

            # 2. 收集 CN 译文中的所有 [EffectID]
            cn_ids: set[str] = set()
            for m in self._EFFECT_ID_RE.finditer(cn_text):
                cn_ids.add(m.group(1))

            # 3. 合并原文显式 ID 与 AC 自动机识别出的效果引用
            affect_refs = block.get("affect_refs", [])
            for ref in affect_refs:
                aff_id = ref.strip("[]").strip()
                if aff_id:
                    source_ids.add(aff_id)

            # 4. 每个效果只检查一次：原 ID 或对应中文名任一存在即可
            for effect_id in sorted(source_ids):
                cn_name = self._affect_id_to_cn.get(effect_id, "")
                if effect_id in cn_ids or (cn_name and cn_name in cn_text):
                    continue

                if cn_name:
                    message = (
                        f"源语言中的特殊效果引用 [{effect_id}]（{cn_name}）"
                        "未在译文中以原 ID 或中文名称表达"
                    )
                else:
                    message = f"源语言中的未知引用 [{effect_id}] 在译文中缺失"

                violations.append(ValidationViolation(
                    block_id=block_idx + 1,
                    rule="effect_ref",
                    severity="warning",
                    message=message,
                    auto_fixable=False,
                    fix_fn=None,
                    fix_params=None,
                ))

        return violations

    def validate_richtext_escapes(
        self, translations: list[str],
    ) -> list[ValidationViolation]:
        """校验译文中的游戏富文本标签是否被错误转义。

        模型有时会把原文 `<color=…>` 等标签转义成 `&lt;…&gt;`（HTML 实体）
        或 `%3C…%3E`（URL 编码），导致游戏无法解析。这里检测这类编码化
        标签并自动还原为原始尖括号。

        仅当标签确实被编码时才匹配，避免误伤正文中偶发的裸 `<` `>` 字面量。

        Returns:
            违规列表，均为 auto_fixable=True（可自动修复）。
        """
        violations: list[ValidationViolation] = []

        for block_idx, cn_text in enumerate(translations):
            if not cn_text:
                continue

            for m in self._RICHTEXT_ESCAPE_RE.finditer(cn_text):
                encoded = m.group(0)
                decoded = (
                    "<" + m.group("slash")
                    + m.group("name") + m.group("attrs") + ">"
                )
                abs_pos = m.start()
                snippet = cn_text[max(0, abs_pos - 5):min(len(cn_text), m.end() + 5)]

                violations.append(ValidationViolation(
                    block_id=block_idx + 1,
                    rule="richtext_escape",
                    severity="error",
                    message=(
                        f"富文本标签被错误转义：'{encoded}' 应还原为 '{decoded}'"
                        f"（上下文: ...{snippet}...）"
                    ),
                    auto_fixable=True,
                    fix_fn="replace",
                    fix_params={
                        "position": abs_pos,
                        "radius": max(len(encoded) + 10, 20),
                        "old": encoded,
                        "new": decoded,
                    },
                ))

        return violations

    def validate_bracketed_cn_buffs(
        self, translations: list[str],
    ) -> list[ValidationViolation]:
        """校验译文中是否出现 [中文名] 形式的错误括号内容。

        方括号内的中文（如 [出血]、[震颤 ]）是英文引擎ID被误译为中文后
        塞回括号导致的，游戏无法识别。已知中文名可确定性还原为 [EnglishId]；
        未知中文名无法可靠映射，标记为 warning 交人工复核（避免误改如 [目标]）。

        仅当存在状态效果数据（self._cn_to_id 非空，即技能文件）时执行，
        避免非技能文件中偶发的合法中文方括号被误报。

        Returns:
            违规列表；已知中文名 → auto_fixable=True（还原为 [EnglishId]），
            未知中文名 → auto_fixable=False（warning）。
        """
        violations: list[ValidationViolation] = []

        # 仅在技能文件（有状态效果数据）时检查，避免误报
        if not self._cn_to_id:
            return violations

        for block_idx, cn_text in enumerate(translations):
            if not cn_text:
                continue

            for m in self._BRACKETED_CN_RE.finditer(cn_text):
                inner = m.group(1)
                full = m.group(0)  # 含括号的完整匹配
                candidate = inner.strip()

                # 已知中文名 → 还原为引擎代码 [EnglishId]
                aff_id = self._cn_to_id.get(candidate)
                if aff_id:
                    fixed = f"[{aff_id}]"
                    abs_pos = m.start()
                    snippet = cn_text[max(0, abs_pos - 5):min(len(cn_text), m.end() + 5)]
                    violations.append(ValidationViolation(
                        block_id=block_idx + 1,
                        rule="bracketed_cn_buff",
                        severity="error",
                        message=(
                            f"方括号内出现中文名（引擎ID被误译）：'{full}' "
                            f"应还原为 '{fixed}'（上下文: ...{snippet}...）"
                        ),
                        auto_fixable=True,
                        fix_fn="replace",
                        fix_params={
                            "position": abs_pos,
                            "radius": max(len(full) + 10, 20),
                            "old": full,
                            "new": fixed,
                        },
                    ))
                    continue

                # 未知中文名：无法可靠映射，标记 warning 交人工复核
                abs_pos = m.start()
                snippet = cn_text[max(0, abs_pos - 5):min(len(cn_text), m.end() + 5)]
                violations.append(ValidationViolation(
                    block_id=block_idx + 1,
                    rule="bracketed_cn_buff",
                    severity="warning",
                    message=(
                        f"方括号内出现无法识别的中文内容：'{full}'"
                        f"（上下文: ...{snippet}...），请人工确认是否为合法标签"
                    ),
                    auto_fixable=False,
                    fix_fn=None,
                    fix_params=None,
                ))

        return violations


# ---------------------------------------------------------------------------
# 术语落位校验（零模型，风险信号；不阻断导出，不自动改译文）
# ---------------------------------------------------------------------------

@dataclass
class LandingObservation:
    """单条术语在一批文本块上的落位观测。"""

    term_key: str
    term: str
    expected_zh: str
    landing: str          # none | applied | partial | missing
    hit_blocks: list[int] = field(default_factory=list)
    note: str = ""


@dataclass
class LandingReport:
    """一次落位校验的聚合结果。"""

    observations: list[LandingObservation] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in ("none", "applied", "partial", "missing")}
        for o in self.observations:
            out[o.landing] = out.get(o.landing, 0) + 1
        return out

    def to_landing_records(self) -> list[dict]:
        """转成 translateFunc.termdb terms-landing 可消费的回流记录。"""
        return [
            {"term_key": o.term_key, "landing": o.landing, "note": o.note}
            for o in self.observations
            if o.landing != "none"
        ]


class TermLandingValidator:
    """确定性落位校验器：把强制档术语在译文中的实际落位判成四态。

    设计约束：
      * 零模型、零人工审批，只产出风险信号，不改译文、不阻断导出；
      * 匹配口径与 stage 1 完全一致：**最长匹配优先 + 不重叠**，并执行
        ``scope``（域/字段）。否则子串碰撞会制造假 missing，把正确术语
        反向降档（自腐蚀）；
      * 不用 2-gram 滑窗判 partial —— 短词会在大词译文里假命中；
      * 斜杠多译名（``A / B``）无法确定性核对，直接不参与观测。
    """

    def __init__(self, proper_terms: list[dict], *, hard_only: bool = True):
        from translateFunc.matcher.ac_automaton import AcAutomaton

        self._registry: list[tuple[str, str, str, str]] = []
        self._by_kr: dict[str, dict] = {}
        self._ac = AcAutomaton()
        seen: set[str] = set()
        for t in proper_terms or []:
            if not isinstance(t, dict):
                continue
            tier = str(t.get("tier", "hard"))
            if hard_only and tier != "hard":
                continue
            if tier == "candidate":
                continue
            scope = t.get("scope") or {}
            if scope.get("match") == "name_only":
                continue
            kr = str(t.get("term", "") or "")
            zh = str(t.get("translation", "") or t.get("cn", "") or "")
            if not (kr and zh) or len(kr) < 2:
                continue
            if "/" in zh or "／" in zh:
                continue  # 多译名条目无法确定性判落位
            key = f"{t.get('entity_id', '')}|{t.get('sense', '')}"
            self._registry.append((key, kr, zh, tier))
            self._by_kr.setdefault(kr, t)
            if kr not in seen:
                seen.add(kr)
                self._ac.add_pattern(kr)
        self._ac.build()
        self._registry.sort(key=lambda x: (-len(x[1]), x[1]))

    def _hits(self, text: str, field: str, domain: str) -> set[str]:
        """对单块文本做与运行时一致的最长匹配 + scope 过滤。"""
        from translateFunc.matcher.engine import scope_allows

        if not text:
            return set()
        allowed: set[str] = set()
        for pat in self._ac.search_non_overlapping(text):
            item = self._by_kr.get(pat.pattern)
            if item is not None and not scope_allows(item, domain=domain, field=field):
                continue
            allowed.add(pat.pattern)
        return allowed

    def check(self, text_blocks: list[dict], translations: list[str]) -> LandingReport:
        report = LandingReport()
        n = min(len(text_blocks), len(translations))
        hits_by_block: dict[int, set[str]] = {}
        for i in range(n):
            block = text_blocks[i] or {}
            hits_by_block[i] = self._hits(
                str(block.get("kr", "") or ""),
                str(block.get("field", "") or ""),
                str(block.get("domain", "") or ""),
            )

        for key, kr, zh, tier in self._registry:
            hits = [i + 1 for i in range(n) if kr in hits_by_block[i]]
            if not hits:
                report.observations.append(LandingObservation(
                    key, kr, zh, "none", note=f"tier={tier}"))
                continue
            applied = 0
            for idx in hits:
                out = str(translations[idx - 1] or "")
                if zh in out:
                    applied += 1
            landing = "applied" if applied else "missing"
            report.observations.append(LandingObservation(
                key, kr, zh, landing, hit_blocks=hits,
                note=f"tier={tier};hits={len(hits)};applied={applied}",
            ))
        return report
