"""规则违规回流修复的回归测试。

对应本次改造：
  1. 规则校验判定不合规（``auto_fixable=False``）的条目，构造最小请求发还给模型修复；
  2. 请求只带违规块；reference 走 slim 但**单独保留 affects**
     （effect_ref 需要 id→中文名 映射），models / model_docs / skill_doc 一律砍掉；
  3. 回填前**必须复验**：原先不合规的规则不再报违规才覆盖，否则保留原译文；
  4. 空译文 / 韩文回填 / 低置信度一律不采纳；
  5. 开关关闭、预算为 0、无违规时**零调用**（回归锚点）。
"""
from __future__ import annotations

import json
from pathlib import Path

from auto_update.config import AppConfig
from translateFunc.builder.request import RequestBuilder
from translateFunc.config import TranslateConfig
from translateFunc.diagnostics import HttpResponseObserver
from translateFunc.processor import FileProcessor
from translateFunc.validator import RuleBasedValidator


# ============================================================
# 测试替身
# ============================================================

class _FakePathConfig:
    real_name = "Skills_Test.json"
    rel_path = Path("Skills_Test.json")


class _FakeTranslator:
    """按脚本依次返回响应的假翻译器。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.clear_cache_count = 0
        self.update_config_calls: list[dict] = []

    def update_config(self, **kwargs):
        self.update_config_calls.append(dict(kwargs))

    def clear_cache(self):
        self.clear_cache_count += 1

    def translate(self, user_prompt, timeout=60):
        index = len(self.calls)
        script = self._responses
        response = script[index] if index < len(script) else script[-1]
        self.calls.append({"prompt": user_prompt, "response": response})
        return response


class _SpyStrategy:
    """记录修复请求用的提示词档位。"""

    def __init__(self):
        self.verbosities: list[str] = []

    def build_stage_1_prompt(self, file_type, prompt_format="xml_json", *, verbosity="full"):
        self.verbosities.append(verbosity)
        return f"system::{verbosity}::{prompt_format}"

    def parse_stage_1_result(self, response, prompt_format="xml_json"):
        return json.loads(response)["translations"]

    def consume_parse_errors(self):
        return []

    def consume_new_terms(self):
        return []


class _RepairBuilder:
    """记录子请求裁剪方式（slim / include_affects）与参与块数。"""

    def __init__(self):
        self.requests: list[dict] = []

    def build_part_request(self, blocks, *, slim=False, include_affects=None):
        self.requests.append({
            "slim": slim,
            "include_affects": include_affects,
            "count": len(blocks),
        })
        return {"metadata": {}, "reference": {}, "text_blocks": list(blocks)}

    def _get_request_text(self, request, prompt_format):
        return "\n".join(
            f'<block id="{i + 1}">{b.get("kr", "")}</block>'
            for i, b in enumerate(request["text_blocks"])
        )


class _LegacyBuilder:
    """只实现旧接口（不接受 include_affects）的 builder，用于兼容层回归。"""

    def __init__(self):
        self.slim_flags: list[bool] = []

    def build_part_request(self, blocks, *, slim=False):
        self.slim_flags.append(slim)
        return {"metadata": {}, "reference": {}, "text_blocks": list(blocks)}

    def _get_request_text(self, request, prompt_format):
        return f"legacy::{len(request['text_blocks'])}"


def _response(entries) -> str:
    return json.dumps({"translations": entries}, ensure_ascii=False)


def _make_processor(
    config: TranslateConfig,
    translator: _FakeTranslator,
) -> FileProcessor:
    processor = FileProcessor.__new__(FileProcessor)
    processor._config = config
    processor._translator = translator
    # 非 None 时诊断事件才会进 _api_calls，便于断言
    processor._recorder = object()
    processor._api_calls = []
    processor._last_failed_call = None
    processor._input_text_blocks = []
    processor._input_reference = {}
    processor._new_term_collector = None
    processor.path_config = _FakePathConfig()
    processor.is_story = False
    processor.is_skill = True
    processor._http_observer = HttpResponseObserver(translator)
    return processor


def _config(**overrides) -> TranslateConfig:
    base = dict(
        enable_rule_validation=True,
        enable_rule_repair=True,
        rule_repair_max_calls=2,
        retry_chunk_size=8,
        min_confidence="low",
    )
    base.update(overrides)
    return TranslateConfig(**base)


def _repair_events(processor: FileProcessor) -> list[dict]:
    """取出规则修复的汇总诊断事件（区别于同 stage 的 API 调用记录）。"""
    return [
        call for call in processor._api_calls
        if call.get("stage") == "rule_repair"
        and "attempts" in (call.get("metadata") or {})
    ]


# ============================================================
# 素材：真实 RuleBasedValidator 产出的违规
# ============================================================

_AFFECTS = [{"id": "Combustion", "kr": "연소", "cn": "燃烧"}]

# 源块引用了 [Combustion]，但译文既没有 [Combustion] 也没有「燃烧」→ effect_ref
_BLOCK_EFFECT = {
    "kr": "연소 3 부여",
    "jp": "",
    "en": "",
    "affect_refs": ["[Combustion]"],
    "proper_refs": [],
}

# 译文把中文名塞进方括号，且该中文名不在 affects 映射里 → bracketed_cn_buff(warning)
_BLOCK_BRACKET = {
    "kr": "대상에게 부여",
    "jp": "",
    "en": "",
    "affect_refs": [],
    "proper_refs": [],
}


def _pending(validator: RuleBasedValidator, blocks, translations) -> list[dict]:
    """复刻 processor 里暂存待修清单的口径。"""
    report = validator.run_all_checks(blocks, translations)
    return [
        {
            "rule": v.rule,
            "severity": v.severity,
            "message": v.message,
            "block_id": v.block_id,
            "auto_fixable": v.auto_fixable,
        }
        for v in report.violations
        if not v.auto_fixable
    ]


def _run_repair(
    blocks: list[dict],
    translations: list[str],
    responses: list[str],
    *,
    config: TranslateConfig | None = None,
    builder=None,
) -> tuple[list[str], _FakeTranslator, _SpyStrategy, FileProcessor]:
    validator = RuleBasedValidator(_AFFECTS)
    pending = _pending(validator, blocks, translations)
    translator = _FakeTranslator(responses)
    strategy = _SpyStrategy()
    processor = _make_processor(config or _config(), translator)
    result = processor._repair_rule_violations(
        builder or _RepairBuilder(), strategy, list(translations),
        blocks, pending, validator, "xml_json",
    )
    return result, translator, strategy, processor


# ============================================================
# 1. 违规被修好并复验通过
# ============================================================

class TestRepairSucceeds:
    def test_effect_ref_violation_is_repaired(self):
        result, translator, strategy, processor = _run_repair(
            [_BLOCK_EFFECT], ["施加3层火焰"],
            [_response([{"id": 1, "translation": "施加3层燃烧 强度",
                         "confidence": "high"}])],
        )

        assert result == ["施加3层燃烧 强度"]
        assert len(translator.calls) == 1
        # 修复档位必须是 slim：minimal 会丢掉被违反的 SKILL 规则
        assert strategy.verbosities == ["slim"]

        events = _repair_events(processor)
        assert len(events) == 1
        assert events[0]["status"] == "success"
        assert events[0]["metadata"]["repairable"] == 1
        assert events[0]["metadata"]["repaired"] == 1
        assert events[0]["metadata"]["by_rule"] == "effect_ref=1"

    def test_unknown_bracketed_cn_is_repaired(self):
        result, translator, _, processor = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "对目标 施加效果",
                         "confidence": "high"}])],
        )

        assert result == ["对目标 施加效果"]
        assert len(translator.calls) == 1
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 1

    def test_both_rules_on_same_block_share_one_call(self):
        blocks = [{
            "kr": "연소 3 부여",
            "jp": "",
            "en": "",
            "affect_refs": ["[Combustion]"],
            "proper_refs": [],
        }]
        validator = RuleBasedValidator(_AFFECTS)
        pending = _pending(validator, blocks, ["施加[目标]火焰"])
        assert {v["rule"] for v in pending} == {"bracketed_cn_buff", "effect_ref"}

        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "施加燃烧 效果", "confidence": "high"}]),
        ])
        processor = _make_processor(_config(), translator)
        result = processor._repair_rule_violations(
            _RepairBuilder(), _SpyStrategy(), ["施加[目标]火焰"],
            blocks, pending, validator, "xml_json",
        )

        assert result == ["施加燃烧 效果"]
        assert len(translator.calls) == 1
        assert _repair_events(processor)[0]["metadata"]["by_rule"] == (
            "bracketed_cn_buff=1, effect_ref=1"
        )


# ============================================================
# 2. 复验不通过 → 保留原译文
# ============================================================

class TestRevalidationRejects:
    def test_still_violating_candidate_is_rejected(self):
        result, translator, _, processor = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "施加[目标]效果",
                         "confidence": "high"}])],
        )

        assert result == ["施加[目标]效果"]
        assert len(translator.calls) == 1
        event = _repair_events(processor)[0]
        assert event["status"] == "partial"
        assert event["metadata"]["repaired"] == 0
        assert event["validation_errors"][0]["block_id"] == 1
        assert event["validation_errors"][0]["action"] == "keep_original_translation"

    def test_bracketed_cn_candidate_is_normalized_then_accepted(self):
        """候选把中文名塞进方括号：复验的确定性修复会还原成 [EnglishId] 后采纳。"""
        result, _, _, processor = _run_repair(
            [_BLOCK_EFFECT], ["施加3层火焰"],
            [_response([{"id": 1, "translation": "施加3层[燃烧]",
                         "confidence": "high"}])],
        )

        assert result == ["施加3层[Combustion]"]
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 1

    def test_partial_fix_of_two_rules_is_rejected(self):
        """两条规则都违规、候选只修好其中一条 → 整块不采纳，保留原译文。"""
        blocks = [{
            "kr": "연소 3 부여",
            "jp": "",
            "en": "",
            "affect_refs": ["[Combustion]"],
            "proper_refs": [],
        }]
        validator = RuleBasedValidator(_AFFECTS)
        pending = _pending(validator, blocks, ["施加[目标]火焰"])
        assert {v["rule"] for v in pending} == {"bracketed_cn_buff", "effect_ref"}

        translator = _FakeTranslator([
            # effect_ref 修好了（正文出现「燃烧」），但 [目标] 仍是不合规的中文方括号
            _response([{"id": 1, "translation": "施加[目标]燃烧", "confidence": "high"}]),
        ])
        processor = _make_processor(_config(), translator)
        result = processor._repair_rule_violations(
            _RepairBuilder(), _SpyStrategy(), ["施加[目标]火焰"],
            blocks, pending, validator, "xml_json",
        )

        assert result == ["施加[目标]火焰"]
        event = _repair_events(processor)[0]
        assert event["metadata"]["repaired"] == 0
        assert event["validation_errors"][0]["rules"] == [
            "bracketed_cn_buff", "effect_ref",
        ]

    def test_auto_fixable_side_issue_is_fixed_then_accepted(self):
        """复验前先过确定性自动修复：模型修对主问题、附带可修小毛病时不该被否掉。"""
        result, _, _, _ = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "施加[目标 ]效果",
                         "confidence": "high"}])],
        )

        assert result == ["施加目标 效果"]

    def test_encoded_richtext_is_restored_before_acceptance(self):
        result, _, _, _ = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{
                "id": 1,
                "translation": "施加&lt;color=#ff6000&gt;目标&lt;/color&gt;效果",
                "confidence": "high",
            }])],
        )

        assert result == ["施加<color=#ff6000>目标</color>效果"]

    def test_empty_translation_is_rejected(self):
        result, _, _, processor = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "   ", "confidence": "high"}])],
        )

        assert result == ["施加[目标]效果"]
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 0

    def test_hangul_backfill_is_rejected(self):
        result, _, _, processor = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "施加[目标]효과",
                         "confidence": "high"}])],
        )

        assert result == ["施加[目标]效果"]
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 0

    def test_low_confidence_is_rejected(self):
        result, _, _, processor = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "对目标 施加效果",
                         "confidence": "low"}])],
            config=_config(min_confidence="high"),
        )

        assert result == ["施加[目标]效果"]
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 0

    def test_parse_failure_keeps_original(self):
        result, _, _, processor = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([])],
        )

        assert result == ["施加[目标]效果"]
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 0


# ============================================================
# 3. 零调用（回归锚点）
# ============================================================

class TestNoCalls:
    def test_switch_off_makes_no_call(self):
        result, translator, _, _ = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "对目标 施加效果"}])],
            config=_config(enable_rule_repair=False),
        )

        assert result == ["施加[目标]效果"]
        assert translator.calls == []

    def test_zero_budget_makes_no_call(self):
        _, translator, _, _ = _run_repair(
            [_BLOCK_BRACKET], ["施加[目标]效果"],
            [_response([{"id": 1, "translation": "对目标 施加效果"}])],
            config=_config(rule_repair_max_calls=0),
        )

        assert translator.calls == []

    def test_no_violation_makes_no_call(self):
        _, translator, _, _ = _run_repair(
            [_BLOCK_EFFECT], ["施加3层燃烧 "], [_response([])],
        )

        assert translator.calls == []

    def test_compliant_translation_has_no_pending(self):
        validator = RuleBasedValidator(_AFFECTS)
        assert _pending(validator, [_BLOCK_EFFECT], ["施加3层燃烧 "]) == []
        assert _pending(validator, [_BLOCK_BRACKET], ["施加[Combustion]"]) == []


# ============================================================
# 4. 预算与分组
# ============================================================

class TestBudgetAndGrouping:
    def test_budget_limits_calls_and_reports_partial(self):
        blocks = [_BLOCK_BRACKET, _BLOCK_BRACKET]
        validator = RuleBasedValidator(_AFFECTS)
        pending = _pending(validator, blocks, ["施加[目标]效果", "施加[目标]效果"])
        assert len(pending) == 2

        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "对目标 施加效果",
                        "confidence": "high"}]),
        ])
        processor = _make_processor(
            _config(rule_repair_max_calls=1, retry_chunk_size=1), translator,
        )
        result = processor._repair_rule_violations(
            _RepairBuilder(), _SpyStrategy(),
            ["施加[目标]效果", "施加[目标]效果"],
            blocks, pending, validator, "xml_json",
        )

        assert len(translator.calls) == 1
        assert result[0] == "对目标 施加效果"
        assert result[1] == "施加[目标]效果"
        event = _repair_events(processor)[0]
        assert event["status"] == "partial"
        assert event["metadata"]["repairable"] == 2
        assert event["metadata"]["repaired"] == 1
        assert event["metadata"]["used"] == 1

    def test_out_of_range_block_ids_are_ignored(self):
        """越界 block_id 一律丢弃：宁可漏修，也不能索引越界打断整条链路。"""
        validator = RuleBasedValidator(_AFFECTS)
        translator = _FakeTranslator([])
        processor = _make_processor(_config(), translator)
        result = processor._repair_rule_violations(
            _RepairBuilder(), _SpyStrategy(), ["甲"],
            [_BLOCK_BRACKET],
            [{"rule": "bracketed_cn_buff", "message": "越界", "block_id": 9}],
            validator, "xml_json",
        )

        assert result == ["甲"]
        assert translator.calls == []

    def test_violating_blocks_are_grouped_together(self):
        blocks = [_BLOCK_BRACKET, _BLOCK_BRACKET]
        validator = RuleBasedValidator(_AFFECTS)
        pending = _pending(validator, blocks, ["施加[目标]效果", "施加[目标]效果"])

        builder = _RepairBuilder()
        translator = _FakeTranslator([
            _response([
                {"id": 1, "translation": "对目标 施加效果", "confidence": "high"},
                {"id": 2, "translation": "对目标 施加效果", "confidence": "high"},
            ]),
        ])
        processor = _make_processor(
            _config(rule_repair_max_calls=2, retry_chunk_size=8), translator,
        )
        result = processor._repair_rule_violations(
            builder, _SpyStrategy(), ["施加[目标]效果", "施加[目标]效果"],
            blocks, pending, validator, "xml_json",
        )

        assert len(translator.calls) == 1
        assert result == ["对目标 施加效果", "对目标 施加效果"]
        # 最小请求：一次调用只带这两个违规块，slim 且单独保留 affects
        assert builder.requests == [
            {"slim": True, "include_affects": True, "count": 2},
        ]
        assert _repair_events(processor)[0]["metadata"]["repaired"] == 2


# ============================================================
# 5. 请求构造
# ============================================================

class TestRequestConstruction:
    def test_repair_request_is_slim_but_keeps_affects(self):
        builder = _RepairBuilder()
        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "对目标 施加效果",
                        "confidence": "high"}]),
        ])
        strategy = _SpyStrategy()
        processor = _make_processor(_config(), translator)
        processor._repair_rule_violations(
            builder, strategy, ["施加[目标]效果"],
            [_BLOCK_BRACKET], _pending(
                RuleBasedValidator(_AFFECTS), [_BLOCK_BRACKET], ["施加[目标]效果"],
            ),
            RuleBasedValidator(_AFFECTS), "xml_json",
        )

        assert builder.requests == [
            {"slim": True, "include_affects": True, "count": 1},
        ]

    def test_legacy_builder_without_include_affects_still_works(self):
        builder = _LegacyBuilder()
        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "对目标 施加效果",
                        "confidence": "high"}]),
        ])
        processor = _make_processor(_config(), translator)
        validator = RuleBasedValidator(_AFFECTS)
        result = processor._repair_rule_violations(
            builder, _SpyStrategy(), ["施加[目标]效果"],
            [_BLOCK_BRACKET],
            _pending(validator, [_BLOCK_BRACKET], ["施加[目标]效果"]),
            validator, "xml_json",
        )

        assert result == ["对目标 施加效果"]
        assert builder.slim_flags == [True]

    def test_hint_uses_group_local_ids(self):
        processor = _make_processor(_config(), _FakeTranslator([]))
        hint = processor._build_repair_hint(
            [3, 7],
            {
                3: [{"rule": "effect_ref", "message": "缺 [Combustion]"}],
                7: [{"rule": "bracketed_cn_buff", "message": "方括号内是中文"}],
            },
        )

        assert "block 1: [effect_ref] 缺 [Combustion]" in hint
        assert "block 2: [bracketed_cn_buff] 方括号内是中文" in hint
        assert "block 4" not in hint and "block 8" not in hint
        assert hint.startswith("<violation_report>")
        assert hint.rstrip().endswith("</violation_report>")

    def test_hint_is_appended_to_rendered_prompt(self):
        builder = _RepairBuilder()
        request = {"metadata": {}, "reference": {}, "text_blocks": [_BLOCK_BRACKET]}
        base = FileProcessor._render_retry_prompt(builder, request, "xml_json")
        with_hint = FileProcessor._render_retry_prompt(
            builder, request, "xml_json", repair_hint="<violation_report>x</violation_report>",
        )

        assert base == '<block id="1">대상에게 부여</block>'
        assert with_hint == base + "\n<violation_report>x</violation_report>"

    def test_summarize_rules_counts_by_rule(self):
        assert FileProcessor._summarize_rules([]) == ""
        assert FileProcessor._summarize_rules([
            {"rule": "effect_ref"}, {"rule": "effect_ref"},
            {"rule": "bracketed_cn_buff"},
        ]) == "bracketed_cn_buff=1, effect_ref=2"


# ============================================================
# 6. build_part_request 的 reference 裁剪口径
# ============================================================

def _real_builder() -> RequestBuilder:
    builder = RequestBuilder.__new__(RequestBuilder)
    builder.unified_request = {
        "metadata": {"total_text_blocks": 1},
        "reference": {
            "proper_terms": [
                {"term": "A", "kr": "가", "cn": "甲"},
                {"term": "B", "kr": "나", "cn": "乙"},
            ],
            "affects": [
                {"id": "Combustion", "kr": "연소", "cn": "燃烧"},
                {"id": "Bleed", "kr": "출혈", "cn": "出血"},
            ],
            "models": [{"model": 1}],
            "model_docs": [{"doc": 1}],
            "skill_doc": "SKILL-DOC-CONTENT",
        },
        "text_blocks": [],
    }
    return builder


_BLOCKS_FOR_REFERENCE = [{
    "kr": "가 연소",
    "proper_refs": ["A"],
    "affect_refs": ["[Combustion]"],
}]


class TestPartRequestReference:
    def test_default_slim_false_keeps_everything(self):
        request = _real_builder().build_part_request(_BLOCKS_FOR_REFERENCE)
        reference = request["reference"]

        assert set(reference) == {
            "proper_terms", "affects", "models", "model_docs", "skill_doc",
        }
        assert [t["term"] for t in reference["proper_terms"]] == ["A"]
        assert [a["id"] for a in reference["affects"]] == ["Combustion"]
        assert "slim_reference" not in request["metadata"]

    def test_slim_true_drops_affects_too(self):
        request = _real_builder().build_part_request(_BLOCKS_FOR_REFERENCE, slim=True)

        assert set(request["reference"]) == {"proper_terms"}
        assert request["metadata"]["slim_reference"] is True
        assert "affects_only" not in request["metadata"]

    def test_slim_with_include_affects_keeps_only_affects(self):
        request = _real_builder().build_part_request(
            _BLOCKS_FOR_REFERENCE, slim=True, include_affects=True,
        )

        assert set(request["reference"]) == {"proper_terms", "affects"}
        assert [a["id"] for a in request["reference"]["affects"]] == ["Combustion"]
        assert request["metadata"]["slim_reference"] is True
        assert request["metadata"]["affects_only"] is True


# ============================================================
# 7. 配置口径
# ============================================================

class TestConfigDefaults:
    def test_translate_config_defaults_off(self):
        config = TranslateConfig()

        assert config.enable_rule_repair is False
        assert config.rule_repair_max_calls == 2

    def test_config_yaml_wires_the_new_keys(self):
        config_path = Path(__file__).resolve().parents[1] / "src" / "config.yaml"
        config = AppConfig.load(config_path)

        assert config.features.enable_rule_repair is False
        assert config.features.rule_repair_max_calls == 2


# ============================================================
# 8. 端到端：从 _translate 主链路触发
# ============================================================

class _E2EBuilder:
    """整链路替身：text_blocks / affects 都用真实结构，便于走真实规则校验。"""

    def __init__(self, *_args, **_kwargs):
        self.unified_request = {
            "metadata": {},
            "reference": {
                "proper_terms": [],
                "affects": list(_AFFECTS),
                "models": [],
                "model_docs": [],
                "skill_doc": "SKILL-DOC-CONTENT",
            },
            "text_blocks": [dict(_BLOCK_EFFECT)],
        }
        self.split_requests: list[dict] = []
        self.max_length = 20000
        self.part_requests: list[dict] = []

    def build(self, prompt_format="xml_json"):
        return None

    def get_request_text(self, prompt_format="xml_json"):
        return ["<user>stage1</user>"]

    def build_part_request(self, blocks, *, slim=False, include_affects=None):
        self.part_requests.append({
            "slim": slim, "include_affects": include_affects, "count": len(blocks),
        })
        return {"metadata": {}, "reference": {}, "text_blocks": list(blocks)}

    def _get_request_text(self, request, prompt_format):
        return "\n".join(
            f'<block id="{i + 1}">{b.get("kr", "")}</block>'
            for i, b in enumerate(request["text_blocks"])
        )

    def deBuild(self, translations):
        return translations


class _E2EStrategy:
    """整链路替身：阶段 2 可选，阶段 2 的改写内容由 self._stage_2_text 决定。"""

    self_check_enabled = False

    def __init__(self, _config):
        self.verbosities: list[str] = []

    def needs_disambiguation(self):
        return False

    def needs_self_check(self):
        return type(self).self_check_enabled

    def build_stage_1_prompt(self, file_type, prompt_format="xml_json", *, verbosity="full"):
        self.verbosities.append(verbosity)
        return f"system::{verbosity}"

    def parse_stage_1_result(self, response, prompt_format="xml_json"):
        return json.loads(response)["translations"]

    def consume_parse_errors(self):
        return []

    def consume_new_terms(self):
        return []

    def build_stage_2_prompt(self, file_type, prompt_format="xml_json"):
        return "system::stage_2"

    def split_stage_2_inputs(
        self, original_blocks, translations, *,
        prompt_format="xml_json", reference=None, max_length=20000,
    ):
        return [{
            "original_blocks": list(original_blocks),
            "translations": list(translations),
            "offset": 0,
            "reference": reference or {},
        }]

    def build_stage_2_user_prompt(
        self, original_blocks, translations, *,
        prompt_format="xml_json", reference=None,
    ):
        return "<user>stage2</user>"

    def parse_stage_2_result(self, response, prompt_format="xml_json"):
        return json.loads(response)["checked_translations"]


class _ScriptedTranslator:
    """按 system prompt / user prompt 特征分派响应，并记录实际经过的阶段。"""

    def __init__(self, *, stage_1: str, stage_2: str | None = None, repair: str | None = None):
        self._stage_1 = stage_1
        self._stage_2 = stage_2
        self._repair = repair
        self.system_prompt = ""
        self.seen: list[str] = []
        self.user_prompts: list[str] = []
        self.clear_cache_count = 0

    def update_config(self, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", self.system_prompt)

    def clear_cache(self):
        self.clear_cache_count += 1

    def translate(self, text, timeout=None):
        self.user_prompts.append(text)
        if "<violation_report>" in text:
            self.seen.append("rule_repair")
            return self._repair
        if "stage_2" in self.system_prompt:
            self.seen.append("stage_2")
            return self._stage_2
        self.seen.append("stage_1")
        return self._stage_1


def _e2e_processor(config: TranslateConfig, translator) -> FileProcessor:
    processor = _make_processor(config, translator)
    processor._engine = object()
    return processor


def _stage_2_response(text: str, *, changed: bool = True) -> str:
    return json.dumps({
        "checked_translations": [
            {"id": 1, "translation": text, "changed": changed},
        ],
    }, ensure_ascii=False)


def _validation_events(processor: FileProcessor) -> list[dict]:
    return [
        call for call in processor._api_calls
        if call.get("stage") == "rule_validation"
    ]


class TestEndToEnd:
    def test_translate_repairs_violation(self, monkeypatch):
        monkeypatch.setattr("translateFunc.processor.RequestBuilder", _E2EBuilder)
        monkeypatch.setattr("translateFunc.processor.StageStrategy", _E2EStrategy)

        translator = _ScriptedTranslator(
            stage_1=_response([{"id": 1, "translation": "施加3层火焰",
                                "confidence": "high"}]),
            repair=_response([{"id": 1, "translation": "施加3层燃烧 强度",
                               "confidence": "high"}]),
        )
        processor = _e2e_processor(
            _config(fallback=False, enable_self_check=False), translator,
        )

        translated, had_fallback = processor._translate({"dataList": []})

        assert translated == ["施加3层燃烧 强度"]
        assert had_fallback is False
        assert translator.seen == ["stage_1", "rule_repair"]
        # 修复档位是 slim（minimal 会丢掉被违反的 SKILL 规则）
        assert "slim" in translator.system_prompt
        # 「发还」的实质：请求里必须带上违规说明
        assert "<violation_report>" in translator.user_prompts[-1]
        assert "[effect_ref]" in translator.user_prompts[-1]
        assert translator.clear_cache_count == 1

    def test_repair_request_only_carries_violating_blocks(self, monkeypatch):
        monkeypatch.setattr("translateFunc.processor.RequestBuilder", _E2EBuilder)
        monkeypatch.setattr("translateFunc.processor.StageStrategy", _E2EStrategy)

        builder_holder: dict = {}
        original_init = _E2EBuilder.__init__

        def _capture(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            builder_holder["builder"] = self

        monkeypatch.setattr(_E2EBuilder, "__init__", _capture)

        translator = _ScriptedTranslator(
            stage_1=_response([{"id": 1, "translation": "施加3层火焰",
                                "confidence": "high"}]),
            repair=_response([{"id": 1, "translation": "施加3层燃烧 强度",
                               "confidence": "high"}]),
        )
        processor = _e2e_processor(
            _config(fallback=False, enable_self_check=False), translator,
        )
        processor._translate({"dataList": []})

        assert builder_holder["builder"].part_requests == [
            {"slim": True, "include_affects": True, "count": 1},
        ]

    def test_violation_introduced_by_stage_2_is_still_repaired(self, monkeypatch):
        """阶段 2 把合规译文改坏 → 修复前的重检必须发现它，否则这条会漏修。"""
        monkeypatch.setattr("translateFunc.processor.RequestBuilder", _E2EBuilder)
        monkeypatch.setattr("translateFunc.processor.StageStrategy", _E2EStrategy)
        monkeypatch.setattr(_E2EStrategy, "self_check_enabled", True)

        translator = _ScriptedTranslator(
            # 阶段 1 是合规的（含「燃烧」），阶段 2 反而改成不合规
            stage_1=_response([{"id": 1, "translation": "施加3层燃烧 强度",
                                "confidence": "high"}]),
            stage_2=_stage_2_response("施加3层火焰"),
            repair=_response([{"id": 1, "translation": "施加3层燃烧 强度",
                               "confidence": "high"}]),
        )
        processor = _e2e_processor(
            _config(fallback=False, enable_self_check=True), translator,
        )

        translated, _ = processor._translate({"dataList": []})

        assert translated == ["施加3层燃烧 强度"]
        assert translator.seen == ["stage_1", "stage_2", "rule_repair"]
        phases = [
            call["metadata"]["phase"] for call in _validation_events(processor)
        ]
        assert phases == ["pre_stage_2", "post_stage_2"]
        # 阶段 1 之后无违规，阶段 2 之后有 → 正是重检捞出来的
        repairable = [
            call["metadata"]["repairable"] for call in _validation_events(processor)
        ]
        assert repairable == [0, 1]

    def test_switch_off_keeps_single_validation_pass(self, monkeypatch):
        """回归锚点：开关关闭时不发修复请求，且不引入阶段 2 后的第二次校验。"""
        monkeypatch.setattr("translateFunc.processor.RequestBuilder", _E2EBuilder)
        monkeypatch.setattr("translateFunc.processor.StageStrategy", _E2EStrategy)
        monkeypatch.setattr(_E2EStrategy, "self_check_enabled", True)

        translator = _ScriptedTranslator(
            stage_1=_response([{"id": 1, "translation": "施加3层火焰",
                                "confidence": "high"}]),
            stage_2=_stage_2_response("施加3层火焰", changed=False),
        )
        processor = _e2e_processor(
            _config(
                fallback=False, enable_self_check=True, enable_rule_repair=False,
            ),
            translator,
        )

        translated, _ = processor._translate({"dataList": []})

        assert translated == ["施加3层火焰"]
        assert translator.seen == ["stage_1", "stage_2"]
        phases = [
            call["metadata"]["phase"] for call in _validation_events(processor)
        ]
        assert phases == ["pre_stage_2"]

