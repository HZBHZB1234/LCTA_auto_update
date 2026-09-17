"""
tests/test_prompt_style.py
Prompt 风格对齐与解析行为的回归测试：
- 阶段 0 空消歧列表是合法结果，不产生 parse_error
- 阶段 1 空翻译列表仍是错误（触发格式回退链）
- SKILL_DOC / 翻译规则与 Cooked_LLC 惯例一致（硬币威力、烧伤 强度、无「」）
- few-shot 示例遵循 Buff 名称尾随半角空格等既有惯例
"""
from __future__ import annotations

from translateFunc.builder.examples import (
    OTHER_EXAMPLES,
    SKILL_EXAMPLES,
    STORY_EXAMPLES,
    UI_EXAMPLES,
    get_examples,
)
from translateFunc.builder.prompt import PromptFactory
from translateFunc.enums import FileType
from translateFunc.translate_doc import SKILL_DOC


def _stage1_rules_text() -> str:
    """将阶段 1 全部规则（共通 + FileType 特有）拼接为纯文本，便于断言。"""
    parts = [rule["text"] for rule in PromptFactory._STAGE1_RULES_DATA]
    for rules in PromptFactory._FILETYPE_RULES.values():
        parts.extend(rule["text"] for rule in rules)
    return "\n".join(parts)


# ---------- B1: parse_response 阶段 0 空结果 ----------


def test_stage0_empty_disambiguations_is_legal() -> None:
    factory = PromptFactory()
    result = factory.parse_response('{"disambiguations": []}', stage=0, prompt_format="xml_json")
    assert result == []
    assert factory.consume_parse_errors() == []


def test_stage1_empty_translations_is_error() -> None:
    factory = PromptFactory()
    result = factory.parse_response('{"translations": []}', stage=1, prompt_format="xml_json")
    assert result == []
    errors = factory.consume_parse_errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "MissingOrEmptyField"


def test_stage2_empty_checked_translations_is_error() -> None:
    factory = PromptFactory()
    factory.parse_response('{"checked_translations": []}', stage=2, prompt_format="xml_json")
    errors = factory.consume_parse_errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "MissingOrEmptyField"


def test_stage0_nonempty_disambiguations_parses() -> None:
    factory = PromptFactory()
    payload = (
        '{"disambiguations": [{"term": "그레고르", "applies": true,'
        ' "actual_meaning": "人名", "reason": "ok"}]}'
    )
    result = factory.parse_response(payload, stage=0, prompt_format="xml_json")
    assert len(result) == 1
    assert result[0]["applies"] is True
    assert factory.consume_parse_errors() == []


# ---------- A1/A2: 术语惯例与 Cooked_LLC 对齐 ----------


def test_stage1_rules_separate_coin_power_from_status_potency() -> None:
    rules = _stage1_rules_text()
    assert "硬币威力" in rules
    assert "위력↔强度" not in rules  # 旧的一刀切规则已移除（它会诱导出"硬币强度"）


def test_skill_doc_uses_burn_not_combust() -> None:
    # Cooked_LLC 中 화상→烧伤（79 处），"燃烧"作为有效译名出现 0 次
    # （SKILL_DOC 反例中允许出现 `[燃烧]`，那是刻意标注的禁止形式）
    assert "烧伤" in SKILL_DOC
    assert "级燃烧" not in SKILL_DOC
    assert "层燃烧" not in SKILL_DOC
    assert "燃烧 强度" not in SKILL_DOC


def test_skill_doc_has_no_cn_bracket_form_example() -> None:
    # 示例不得出现 "[Combustion]强度" 这类正文保留英文ID的写法（正确形式：烧伤 强度）
    assert "[Combustion]强度" not in SKILL_DOC
    assert "烧伤 强度" in SKILL_DOC
    assert "充能 层数" in SKILL_DOC


def test_stage1_rules_forbid_corner_quotes() -> None:
    assert "「」" in _stage1_rules_text()


# ---------- A4: few-shot 示例遵循既有惯例 ----------


def test_skill_examples_keep_buff_trailing_space() -> None:
    # Buff 名称后必须紧跟一个半角空格，即使位于句尾（如"获得3层守护 "）
    translations = [ex["translation"] for ex in SKILL_EXAMPLES]
    assert any(t.endswith("守护 ") for t in translations)
    assert any(t.endswith("震颤 ") for t in translations)
    assert any("烧伤 强度" in t for t in translations)
    assert any("硬币威力" in t for t in translations)


def test_examples_have_no_corner_quotes() -> None:
    all_examples = STORY_EXAMPLES + SKILL_EXAMPLES + UI_EXAMPLES + OTHER_EXAMPLES
    for ex in all_examples:
        assert "「" not in ex["translation"], ex["translation"]


def test_examples_preserve_placeholders() -> None:
    kr_ins = [ex["in"] for ex in STORY_EXAMPLES + OTHER_EXAMPLES]
    translations = [ex["translation"] for ex in STORY_EXAMPLES + OTHER_EXAMPLES]
    assert any("{0}" in kr for kr in kr_ins)
    assert any("{0}" in t for t in translations)


def test_get_examples_mapping_intact() -> None:
    for name in ("STORY", "SKILL", "UI", "OTHER"):
        examples = get_examples(name)
        assert examples, name
        for ex in examples:
            assert set(ex) == {"in", "reasoning", "translation", "confidence"}
            assert ex["confidence"] in ("high", "medium", "low")


def test_skill_filetype_system_prompt_contains_examples() -> None:
    factory = PromptFactory()
    prompt = factory.build_system_prompt(file_type=FileType.SKILL, stage=1, prompt_format="xml_json")
    assert "烧伤 强度" in prompt
    assert "硬币威力" in prompt


# ---------- 阶段 1 提示词：新专有名词回传与降级档位 ----------


def test_stage1_prompt_requests_new_terms() -> None:
    """模型翻译时要顺带回传 glossary 未收录的专有名词，供下次输入合并。"""
    factory = PromptFactory()
    prompt = factory.build_system_prompt(
        file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
    )
    assert "new_terms" in prompt
    assert "glossary未收录" in prompt


def test_minimal_verbosity_drops_optional_new_terms() -> None:
    """降级到极简档时不再索取 new_terms，响应规则要压到最小。"""
    factory = PromptFactory()
    minimal = factory.build_system_prompt(
        file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
        verbosity="minimal",
    )
    assert "new_terms" not in minimal
    assert "[Bleed]" in minimal          # 硬保护规则仍在


def test_full_verbosity_is_the_default_snapshot() -> None:
    """full 是默认档，显式传 full 必须与默认输出完全一致。"""
    factory = PromptFactory()
    default = factory.build_system_prompt(
        file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
    )
    explicit = factory.build_system_prompt(
        file_type=FileType.SKILL, stage=1, prompt_format="xml_json",
        verbosity="full",
    )
    assert default == explicit
