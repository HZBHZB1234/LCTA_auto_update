"""
translateFunc/builder/examples.py
Few-shot 翻译示例，按文件类型分类。
在提示词 v2 中用于帮助 LLM 理解输出格式和翻译风格。

所有例句均取自 lang/LLC_zh-CN 熟肉（Cooked_LLC）与对应 KR 原文的真实对照，
演示术语表遵守（如 화상→烧伤）、Buff 名称尾随半角空格、
코인 위력→硬币威力、引擎事件标签 [WhenUse]/[OnSucceedAttack] 保留等既有惯例。
"""
from __future__ import annotations

# ---- STORY 文件示例 (剧情对话) ----

STORY_EXAMPLES: list[dict] = [
    {
        "in": "KR: 다시 교육팀 복도에 도착했다.",
        "reasoning": "叙事句，按中文叙事习惯补出主语；使用全角句号。",
        "translation": "我们回到了培训部的走廊。",
        "confidence": "high",
    },
    {
        "in": "KR: '이거 해볼만 한 것 같아요.'",
        "reasoning": "对白引语：原文的成对引号转为中文引号“”，保留口语语气。",
        "translation": "“看起来值得一试。”",
        "confidence": "high",
    },
    {
        "in": "KR: 매뉴얼을 열심히 읽던 {0} 수감자의 표정이 밝아졌다.",
        "reasoning": "保留{0}占位符原样；'수감자'按既有译文统一译为'罪人'，语序按中文习惯调整。",
        "translation": "看过守则之后，罪人{0}的脸上浮现出笑容。",
        "confidence": "high",
    },
]

# ---- SKILL 文件示例 (技能描述) ----
# 注意：示例译文中的 Buff 名称（守护/震颤/烧伤）后都紧跟一个半角空格，此空格是刻意保留的惯例。

SKILL_EXAMPLES: list[dict] = [
    {
        "in": "KR: 이전 턴에 피해를 받지 않은 경우 코인 위력 +3",
        "reasoning": "'코인 위력'译为'硬币威力'——技能/硬币的威力用'威力'，不用'强度'。",
        "translation": "若自身在上回合未受到伤害，则使本技能的硬币威力+3",
        "confidence": "high",
    },
    {
        "in": "KR: [WhenUse] 다음 턴에 보호 3 얻음",
        "reasoning": "[WhenUse]是引擎事件标签，原样保留；Buff名'守护'后跟一个半角空格，即使位于句尾。",
        "translation": "[WhenUse] 下回合使自身获得3层守护 ",
        "confidence": "high",
    },
    {
        "in": "KR: [OnSucceedAttackHead] 화상 1 부여",
        "reasoning": "'화상'按术语表译为'烧伤'（不译作'燃烧'），状态量写作'烧伤 强度'（名称+半角空格）。",
        "translation": "[OnSucceedAttackHead] 使目标增加1级烧伤 强度",
        "confidence": "high",
    },
    {
        "in": "KR: [OnSucceedAttackHead] 진동 횟수 2 증가",
        "reasoning": "'진동'译'震颤'，횟수→层数；Buff名'震颤'后跟半角空格。",
        "translation": "[OnSucceedAttackHead] 对目标施加2层震颤 ",
        "confidence": "high",
    },
]

# ---- UI 文件示例 (界面文本) ----

UI_EXAMPLES: list[dict] = [
    {
        "in": "KR: 일반 추출",
        "reasoning": "UI短标签，简洁不加标点。",
        "translation": "常规提取",
        "confidence": "high",
    },
    {
        "in": "KR: 3성 인격 확정 추출",
        "reasoning": "UI标签，保留★符号；使用全角冒号。",
        "translation": "必得提取：3★人格",
        "confidence": "high",
    },
    {
        "in": "KR: 특정 추출 그레고르",
        "reasoning": "人名'그레고르'按术语表译为'格里高尔'。",
        "translation": "定向提取：格里高尔",
        "confidence": "high",
    },
]

# ---- OTHER 文件示例 (通用) ----

OTHER_EXAMPLES: list[dict] = [
    {
        "in": "KR: 애써 고개를 돌린 [{0}] 수감자가 스파크가 튀는 발전기를 살피던 중, 고장난 부품을 어렵지 않게 발견했다.",
        "reasoning": "方括号占位符[{0}]原样保留，不得破坏；叙事文本使用全角标点。",
        "translation": "罪人[{0}]尽可能别过脸去，检查着火花四溅的发电机，轻松找出了故障零件。",
        "confidence": "high",
    },
]


def get_examples(file_type_name: str) -> list[dict] | None:
    """根据 FileType 名称获取对应的 few-shot 示例列表。"""
    mapping = {
        "STORY": STORY_EXAMPLES,
        "SKILL": SKILL_EXAMPLES,
        "UI": UI_EXAMPLES,
        "OTHER": OTHER_EXAMPLES,
    }
    return mapping.get(file_type_name)
