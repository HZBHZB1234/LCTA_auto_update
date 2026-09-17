"""失败降级阶梯的回归测试。

对应本次改造：
  1. 翻译失败不再直接回退韩文，而是 L1 切更小块 → L2 精简提示词
     → L3 剥离复杂响应规则，逐级加码；
  2. 调用次数受 retry_max_calls_per_part 硬预算约束，且为后续级别留出额度；
  3. 每一级只处理上一级仍未解决的索引；
  4. 每次重试前必须清缓存，否则会命中同一份坏结果；
  5. 关掉开关后行为退化为旧版（不做任何额外调用）。
"""
from __future__ import annotations

import json
from pathlib import Path

from translateFunc.builder.prompt import PromptFactory
from translateFunc.config import TranslateConfig
from translateFunc.diagnostics import HttpResponseObserver
from translateFunc.enums import FileType
from translateFunc.processor import FileProcessor, _chunk_evenly


# ============================================================
# 测试替身
# ============================================================

class _FakePathConfig:
    real_name = "Test.json"
    rel_path = Path("Test.json")


class _FakeTranslator:
    """按脚本依次返回响应的假翻译器，并记录每次调用的 system prompt。"""

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
    """记录每一级使用的提示词档位。"""

    def __init__(self):
        self.verbosities: list[str] = []
        self.new_terms: list[list[dict]] = []

    def build_stage_1_prompt(self, file_type, prompt_format="xml_json", *, verbosity="full"):
        self.verbosities.append(verbosity)
        return f"system::{verbosity}::{prompt_format}"

    def parse_stage_1_result(self, response, prompt_format="xml_json"):
        return json.loads(response)["translations"]

    def consume_parse_errors(self):
        return []

    def consume_new_terms(self):
        if self.new_terms:
            return self.new_terms.pop(0)
        return []


class _FakeBuilder:
    """记录子请求的裁剪方式（是否 slim）。"""

    def __init__(self):
        self.slim_flags: list[bool] = []
        self.group_sizes: list[int] = []

    def build_part_request(self, blocks, *, slim=False):
        self.slim_flags.append(slim)
        self.group_sizes.append(len(blocks))
        return {"metadata": {}, "reference": {}, "text_blocks": list(blocks)}

    def _get_request_text(self, request, prompt_format):
        return f"user::{len(request['text_blocks'])}"


def _response(entries) -> str:
    return json.dumps({"translations": entries}, ensure_ascii=False)


def _make_processor(config: TranslateConfig, translator: _FakeTranslator) -> FileProcessor:
    processor = FileProcessor.__new__(FileProcessor)
    processor._config = config
    processor._translator = translator
    processor._recorder = None
    processor._api_calls = []
    processor._last_failed_call = None
    processor._input_text_blocks = []
    processor._input_reference = {}
    processor._new_term_collector = None
    processor.path_config = _FakePathConfig()
    processor.is_story = False
    processor.is_skill = False
    processor._http_observer = HttpResponseObserver(translator)
    return processor


# ============================================================
# 1. 未解决集合的判定口径
# ============================================================

class TestCollectUnresolved:
    def test_classifies_three_kinds(self):
        processor = _make_processor(TranslateConfig(), _FakeTranslator([_response([])]))
        blocks = [
            {"kr": "리나모비블"},   # 0: 韩文原样回填 → 漏翻
            {"kr": "번역할 문장"},  # 1: 译文为空 → 未解决
            {"kr": "정상 문장"},    # 2: 正常
            {"kr": ""},             # 3: KR 本来就是空，译文空属正常
        ]
        part_result = ["리나모비블", "", "正常句子", ""]

        unresolved = processor._collect_unresolved(part_result, blocks)

        assert unresolved == {0: "untranslated_hangul", 1: "empty_translation"}

    def test_seeded_reasons_are_kept(self):
        processor = _make_processor(TranslateConfig(), _FakeTranslator([_response([])]))
        blocks = [{"kr": "가"}, {"kr": "나"}]

        unresolved = processor._collect_unresolved(
            ["甲", "乙"], blocks, {1: "low_confidence"},
        )

        assert unresolved == {1: "low_confidence"}

    def test_short_result_does_not_crash(self):
        """解析结果比文本块短时，缺失部分按未解决处理。"""
        processor = _make_processor(TranslateConfig(), _FakeTranslator([_response([])]))
        blocks = [{"kr": "가"}, {"kr": "나"}]

        unresolved = processor._collect_unresolved(["甲"], blocks)

        assert unresolved == {1: "empty_translation"}


# ============================================================
# 2. 分组规划：预算要留给后续级别
# ============================================================

class TestPlanRetryGroups:
    def test_chunk_evenly_keeps_order_and_count(self):
        assert _chunk_evenly([0, 1, 2, 3, 4], 2) == [[0, 1, 2], [3, 4]]
        assert _chunk_evenly([0, 1, 2], 5) == [[0], [1], [2]]
        assert _chunk_evenly([], 3) == []

    def test_l1_reserves_budget_for_later_levels(self):
        """L1 最多把预算用到只剩 2 次，保证 L2/L3 还有机会被试到。"""
        config = TranslateConfig(retry_chunk_size=8, retry_max_calls_per_part=4)
        processor = _make_processor(config, _FakeTranslator([_response([])]))
        targets = list(range(40))

        groups = processor._plan_retry_groups(targets, level=1, budget_left=4)

        assert len(groups) == 2          # 4 - 2 = 2 组
        assert sorted(i for g in groups for i in g) == targets

    def test_l2_l3_use_remaining_budget(self):
        config = TranslateConfig(retry_chunk_size=8, retry_max_calls_per_part=4)
        processor = _make_processor(config, _FakeTranslator([_response([])]))

        groups = processor._plan_retry_groups(list(range(40)), level=2, budget_left=2)

        assert len(groups) == 2

    def test_group_size_respects_chunk_size(self):
        config = TranslateConfig(retry_chunk_size=8, retry_max_calls_per_part=10)
        processor = _make_processor(config, _FakeTranslator([_response([])]))

        groups = processor._plan_retry_groups(list(range(16)), level=2, budget_left=10)

        assert all(len(g) <= 8 for g in groups)


# ============================================================
# 3. 逐级加码：L1 → L2 → L3
# ============================================================

class TestEscalationLadder:
    def test_escalates_verbosity_level_by_level(self):
        """L1 失败后换 L2 的精简提示词，而不是用同样的参数重试。"""
        config = TranslateConfig(retry_max_calls_per_part=4)
        translator = _FakeTranslator([
            _response([]),                                          # L1：解析不出条目
            _response([{"id": 1, "translation": "莉娜莫维尔", "confidence": "high"}]),
        ])
        processor = _make_processor(config, translator)
        strategy = _SpyStrategy()
        builder = _FakeBuilder()
        blocks = [{"kr": "리나모비블"}]
        part_result = ["리나모비블"]

        fixed, attempts = processor._escalate_retry(
            builder, strategy, {"text_blocks": blocks}, part_result,
            {0: "untranslated_hangul"}, 0, "xml_json",
        )

        assert fixed == {0: "L2"}
        assert part_result == ["莉娜莫维尔"]
        assert strategy.verbosities == ["full", "slim"]
        assert [a["status"] for a in attempts] == ["parse_error", "ok"]

    def test_third_level_strips_reference_and_uses_minimal_prompt(self):
        config = TranslateConfig(retry_max_calls_per_part=4)
        translator = _FakeTranslator([
            _response([]),
            _response([]),
            _response([{"id": 1, "translation": "莉娜莫维尔", "confidence": "high"}]),
        ])
        processor = _make_processor(config, translator)
        strategy = _SpyStrategy()
        builder = _FakeBuilder()
        part_result = ["리나모비블"]

        fixed, _ = processor._escalate_retry(
            builder, strategy, {"text_blocks": [{"kr": "리나모비블"}]}, part_result,
            {0: "missing_translation"}, 0, "xml_json",
        )

        assert fixed == {0: "L3"}
        assert strategy.verbosities == ["full", "slim", "minimal"]
        assert builder.slim_flags == [False, False, True]
        assert part_result == ["莉娜莫维尔"]

    def test_clears_cache_before_every_attempt(self):
        """缓存键只含 user_text，不清缓存会让阶梯整体失效。"""
        config = TranslateConfig(retry_max_calls_per_part=3)
        translator = _FakeTranslator([_response([])])
        processor = _make_processor(config, translator)
        part_result = ["리나모비블"]

        processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            part_result, {0: "missing_translation"}, 0, "xml_json",
        )

        assert len(translator.calls) == 3
        assert translator.clear_cache_count == 3

    def test_budget_stops_the_ladder(self):
        config = TranslateConfig(retry_max_calls_per_part=1)
        translator = _FakeTranslator([_response([])])
        processor = _make_processor(config, translator)

        fixed, attempts = processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            ["리나모비블"], {0: "missing_translation"}, 0, "xml_json",
        )

        assert fixed == {}
        assert len(translator.calls) == 1
        assert len(attempts) == 1

    def test_disabled_switch_makes_no_extra_calls(self):
        config = TranslateConfig(enable_retry_escalation=False)
        translator = _FakeTranslator([_response([])])
        processor = _make_processor(config, translator)

        fixed, attempts = processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            ["리나모비블"], {0: "missing_translation"}, 0, "xml_json",
        )

        assert fixed == {} and attempts == []
        assert translator.calls == []

    def test_zero_level_makes_no_extra_calls(self):
        config = TranslateConfig(retry_max_level=0)
        translator = _FakeTranslator([_response([])])
        processor = _make_processor(config, translator)

        fixed, _ = processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            ["리나모비블"], {0: "missing_translation"}, 0, "xml_json",
        )

        assert fixed == {}
        assert translator.calls == []


# ============================================================
# 4. 回填校验：坏结果不算"救回来了"
# ============================================================

class TestRetryPayload:
    def test_korean_passthrough_is_not_accepted_as_fix(self):
        """重试仍然回填韩文时不能算修复，否则漏翻会被写进产出。"""
        config = TranslateConfig()
        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "리나모비블", "confidence": "high"}]),
        ])
        processor = _make_processor(config, translator)
        part_result = [""]

        fixed, _ = processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            part_result, {0: "empty_translation"}, 0, "xml_json",
        )

        assert fixed == {}
        assert part_result == [""]

    def test_low_confidence_is_not_accepted_as_fix(self):
        config = TranslateConfig(min_confidence="high")
        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "莉娜莫维尔", "confidence": "low"}]),
        ])
        processor = _make_processor(config, translator)
        part_result = [""]

        fixed, _ = processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            part_result, {0: "empty_translation"}, 0, "xml_json",
        )

        assert fixed == {}

    def test_partial_fix_keeps_only_recovered_indices(self):
        config = TranslateConfig(retry_max_calls_per_part=3)
        translator = _FakeTranslator([
            _response([
                {"id": 1, "translation": "莉娜莫维尔", "confidence": "high"},
                {"id": 2, "translation": "번역할 문장", "confidence": "high"},
            ]),
            # L2 只送第 2 个块，模型仍然回填韩文原文
            _response([{"id": 1, "translation": "번역할 문장", "confidence": "high"}]),
        ])
        processor = _make_processor(config, translator)
        blocks = [{"kr": "리나모비블"}, {"kr": "번역할 문장"}]
        part_result = ["", ""]

        fixed, _ = processor._escalate_retry(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": blocks}, part_result,
            {0: "missing_translation", 1: "missing_translation"}, 0, "xml_json",
        )

        assert fixed == {0: "L1"}                 # 第 2 条仍是韩文，不算修复
        assert part_result == ["莉娜莫维尔", ""]

    def test_retry_missing_entries_wrapper_still_works(self):
        """旧签名入口保持可用，内部走阶梯。"""
        config = TranslateConfig()
        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "莉娜莫维尔", "confidence": "high"}]),
        ])
        processor = _make_processor(config, translator)
        part_result = ["리나모비블"]

        fixed = processor._retry_missing_entries(
            _FakeBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            part_result, [0], ["xml_json"], 0,
        )

        assert fixed == 1
        assert part_result == ["莉娜莫维尔"]

    def test_legacy_builder_without_part_request_is_supported(self):
        """只实现旧接口的 builder 不应让阶梯整体失效。"""

        class _LegacyBuilder:
            def get_request_text(self, prompt_format):
                return ["legacy user text"]

        config = TranslateConfig()
        translator = _FakeTranslator([
            _response([{"id": 1, "translation": "莉娜莫维尔", "confidence": "high"}]),
        ])
        processor = _make_processor(config, translator)
        part_result = [""]

        fixed, _ = processor._escalate_retry(
            _LegacyBuilder(), _SpyStrategy(), {"text_blocks": [{"kr": "리나모비블"}]},
            part_result, {0: "empty_translation"}, 0, "xml_json",
        )

        assert fixed == {0: "L1"}
        assert translator.calls[0]["prompt"] == "legacy user text"


# ============================================================
# 5. 提示词档位
# ============================================================

class TestPromptVerbosity:
    def test_full_is_default_and_unchanged(self):
        factory = PromptFactory()
        default = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
        )
        explicit = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
            verbosity="full",
        )
        assert default == explicit

    def test_slim_drops_p2_rules_and_examples(self):
        from translateFunc.builder.examples import SKILL_EXAMPLES
        from translateFunc.enums import FileType

        factory = PromptFactory()
        slim = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
            examples=SKILL_EXAMPLES, verbosity="slim",
        )
        full = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
            examples=SKILL_EXAMPLES,
        )

        assert "省略号" not in slim          # P2 风格规则被裁掉
        assert "<examples>" not in slim      # few-shot 示例被裁掉
        assert "富文本标签" in slim          # P1 保护规则保留
        assert "<examples>" in full
        assert len(slim) < len(full)

    def test_minimal_keeps_only_hard_protection(self):
        from translateFunc.enums import FileType

        factory = PromptFactory()
        minimal = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
            verbosity="minimal",
        )

        assert "[Bleed]" in minimal          # 硬保护规则在
        assert "翻译优先级" not in minimal    # 全量规则表不在
        assert "省略号" not in minimal
        assert "<examples>" not in minimal
        assert "reasoning" not in minimal     # 极简 schema 不要推理字段
        assert '"translation"' in minimal

    def test_minimal_format_per_response_type(self):
        from translateFunc.enums import FileType

        factory = PromptFactory()
        for prompt_format, marker in (
            ("xml_json", '"translations"'),
            ("json_json", '"response_type"'),
            ("xml_xml", "<translations>"),
        ):
            prompt = factory.build_system_prompt(
                file_type=FileType.OTHER, stage=1, prompt_format=prompt_format,
                verbosity="minimal",
            )
            assert marker in prompt, prompt_format

    def test_unknown_verbosity_falls_back_to_full(self):
        from translateFunc.enums import FileType

        factory = PromptFactory()
        weird = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
            verbosity="nonsense",
        )
        full = factory.build_system_prompt(
            file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
        )
        assert weird == full


# ============================================================
# 6. new_terms 附加产出的解析
# ============================================================

class TestNewTermsParsing:
    def test_stage1_format_requests_new_terms(self):
        factory = PromptFactory()
        prompt = factory.build_system_prompt(
            file_type=FileType.OTHER, stage=1, prompt_format="xml_json",
        )
        assert "new_terms" in prompt

    def test_json_response_extracts_new_terms(self):
        factory = PromptFactory()
        payload = json.dumps({
            "translations": [{"id": 1, "translation": "译文", "confidence": "high"}],
            "new_terms": [
                {"kr": "리나모비블", "cn": "莉娜莫维尔", "category": "person"},
            ],
        }, ensure_ascii=False)

        result = factory.parse_response(payload, stage=1, prompt_format="xml_json")

        assert len(result) == 1
        assert factory.consume_new_terms() == [
            {"kr": "리나모비블", "cn": "莉娜莫维尔", "category": "person"},
        ]
        assert factory.consume_new_terms() == []      # 取走即清空

    def test_broken_new_terms_never_break_translations(self):
        """new_terms 是附加产出：字段坏掉不能影响 translations 的解析。"""
        factory = PromptFactory()
        payload = json.dumps({
            "translations": [{"id": 1, "translation": "译文", "confidence": "high"}],
            "new_terms": "不是数组",
        }, ensure_ascii=False)

        result = factory.parse_response(payload, stage=1, prompt_format="xml_json")

        assert len(result) == 1
        assert factory.consume_new_terms() == []

    def test_xml_response_extracts_new_terms(self):
        factory = PromptFactory()
        payload = (
            "<translations>"
            '<item id="1"><translation>译文</translation>'
            "<confidence>high</confidence></item>"
            "<new_terms><term><kr>리나모비블</kr><cn>莉娜莫维尔</cn>"
            "<category>person</category></term></new_terms>"
            "</translations>"
        )

        result = factory.parse_response(payload, stage=1, prompt_format="xml_xml")

        assert len(result) == 1
        assert factory.consume_new_terms() == [
            {"kr": "리나모비블", "cn": "莉娜莫维尔", "category": "person"},
        ]

    def test_minimal_verbosity_does_not_ask_for_new_terms(self):
        """降级到极简档时不再索取 new_terms —— 响应规则要压到最小。"""
        from translateFunc.enums import FileType

        factory = PromptFactory()
        minimal = factory.build_system_prompt(
            file_type=FileType.OTHER, stage=1, prompt_format="xml_json",
            verbosity="minimal",
        )
        assert "new_terms" not in minimal


# ============================================================
# 7. 端到端：主格式全失败时不再直接回退韩文
# ============================================================

def _build_full_processor(tmp_path: Path, translator, config: TranslateConfig):
    """按生产目录布局落盘 KR/JP/EN，返回可直接 process() 的处理器。"""
    from translateFunc.config import FilePathConfig, PathConfig
    from translateFunc.matcher.engine import MatcherEngine

    data = {"dataList": [{"id": "A", "name": "리나모비블", "desc": "번역할 문장"}]}
    kr_root = tmp_path / "kr"
    jp_root = tmp_path / "jp"
    en_root = tmp_path / "en"
    kr_root.mkdir(parents=True, exist_ok=True)
    jp_root.mkdir(parents=True, exist_ok=True)
    en_root.mkdir(parents=True, exist_ok=True)

    kr_path = kr_root / "KR_Test.json"
    for path in (kr_path, jp_root / "JP_Test.json", en_root / "EN_Test.json"):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    base = PathConfig(
        target_path=tmp_path / "out",
        llc_base_path=tmp_path / "llc",
        KR_base_path=kr_root,
        JP_base_path=jp_root,
        EN_base_path=en_root,
    )
    return FileProcessor(
        path_config=FilePathConfig(KR_path=kr_path, _PathConfig=base, has_prefix=True),
        engine=MatcherEngine(),
        translate_config=config,
        translator=translator,
    )


class TestEndToEndFallbackAvoided:
    def test_all_formats_failed_is_rescued_by_ladder(self, tmp_path):
        """主格式链全部解析失败 → 阶梯救回 → 产出是中文而非韩文。"""
        from translateFunc.enums import ProcessResult

        config = TranslateConfig(fallback=True, retry_max_calls_per_part=4)
        good = _response([
            {"id": 1, "translation": "莉娜莫维尔", "confidence": "high"},
            {"id": 2, "translation": "待翻译的句子", "confidence": "high"},
        ])
        # 3 种格式各失败一次，第 4 次（L1 降级重试）才成功
        translator = _FakeTranslator(["不是 JSON", "不是 JSON", "不是 JSON", good])
        processor = _build_full_processor(tmp_path, translator, config)

        outcome = processor.process()

        assert outcome.result == ProcessResult.SUCCESS_SAVED
        assert len(translator.calls) == 4

        saved = json.loads(
            (tmp_path / "out" / "Test.json").read_text(encoding="utf-8-sig")
        )
        entry = saved["dataList"][0]
        assert entry["name"] == "莉娜莫维尔"
        assert entry["desc"] == "待翻译的句子"

    def test_ladder_exhausted_keeps_old_fallback_semantics(self, tmp_path):
        """阶梯穷尽后仍是老语义：缺失条目回退 KR，结果标记为 fallback。"""
        from translateFunc.enums import ProcessResult

        config = TranslateConfig(fallback=True, retry_max_calls_per_part=2)
        translator = _FakeTranslator(["不是 JSON"])
        processor = _build_full_processor(tmp_path, translator, config)

        outcome = processor.process()

        assert outcome.result == ProcessResult.FALLBACK_TO_ORIGINAL
        # 主格式 3 次 + 阶梯 2 次（受预算约束）
        assert len(translator.calls) == 5

        saved = json.loads(
            (tmp_path / "out" / "Test.json").read_text(encoding="utf-8-sig")
        )
        entry = saved["dataList"][0]
        assert entry["name"] == "리나모비블"
        assert entry["desc"] == "번역할 문장"

    def test_disabling_escalation_restores_old_call_count(self, tmp_path):
        """关掉开关后调用次数退回旧版（只有格式回退链，无阶梯）。"""
        from translateFunc.enums import ProcessResult

        config = TranslateConfig(fallback=True, enable_retry_escalation=False)
        translator = _FakeTranslator(["不是 JSON"])
        processor = _build_full_processor(tmp_path, translator, config)

        outcome = processor.process()

        assert outcome.result == ProcessResult.FALLBACK_TO_ORIGINAL
        assert len(translator.calls) == 3
