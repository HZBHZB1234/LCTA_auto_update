"""Jev System One 客户端与阶段 0 逐位置消歧的单元测试。

覆盖：
  1. JevClient —— 问题构建、Answer 解析（noul/choice/score）、
     answers_to_occurrences 映射与置信度门限；
  2. _apply_jev_disambiguation —— 逐位置移除（只删判定不适用处）、
     reference 术语裁剪、零排除时的保持；
  3. _try_jev_disambiguation —— 无 key 回退、请求失败回退、成功路径应用。

真实端点冒烟（live_*）默认跳过；设置 JEV_TEST_LIVE=1 且存在 .env 时才运行，
用于验证官方与个人端点在典型难例上的输出结构（开销极小）。
"""
import json
import os
from pathlib import Path

import pytest

from translateFunc.config import TranslateConfig
from translateFunc.processor import FileProcessor
from translateFunc.proper.jev import JevAnswer, JevChoice, JevClient


# ============================================================
# 1. JevClient —— 问题与解析
# ============================================================

class TestJevClientSchema:
    def test_choice_question_shape(self):
        q = JevClient.choice_question("choose meaning", {"a": "A", "b": "B"})
        assert q["type"] == "choice"
        assert q["criteria"] == {"a": "A", "b": "B"}

    def test_noul_question_shape(self):
        q = JevClient.noul_question("is it a name?")
        assert q["type"] == "noul"
        assert "instructions" in q and "criteria" not in q

    def test_parse_noul_answer(self):
        ans = JevClient._parse_answer("q1", {"type": "noul", "noul": 0.8})
        assert ans.qtype == "noul"
        assert ans.noul == pytest.approx(0.8)
        assert ans.choice is None

    def test_parse_choice_answer(self):
        ans = JevClient._parse_answer("q2", {
            "type": "choice",
            "choice": "blood_pack_item",
            "confidence": 1.0,
            "probabilities": {"a": 0.0, "b": 1.0},
        })
        assert ans.qtype == "choice"
        assert ans.choice.chosen == "blood_pack_item"
        assert ans.choice.confidence == pytest.approx(1.0)
        assert ans.is_high_confidence  # 0.9 阈值下高置信度

    def test_parse_score_answer(self):
        ans = JevClient._parse_answer("q3", {"type": "score", "score": 7})
        assert ans.qtype == "score"
        assert ans.score == pytest.approx(7.0)

    def test_parse_unknown_type_raises(self):
        with pytest.raises(Exception):
            JevClient._parse_answer("q4", {"type": "boo"})


class TestAnswersToOccurrences:
    def _occurrences(self):
        return {
            "q0": {"term": "이상", "block_index": 0},
            "q1": {"term": "이상", "block_index": 1},
        }

    def test_maps_full_answers(self):
        answers = {
            "q0": JevAnswer("q0", "choice",
                            choice=JevChoice("not_applicable", 0.99, {})),
            "q1": JevAnswer("q1", "choice",
                            choice=JevChoice("applicable", 1.0, {})),
        }
        result = JevClient.answers_to_occurrences(self._occurrences(), answers)
        assert result[(0, "이상")] == "not_applicable"
        assert result[(1, "이상")] == "applicable"

    def test_low_confidence_is_none(self):
        answers = {
            "q0": JevAnswer("q0", "choice",
                            choice=JevChoice("not_applicable", 0.5, {})),
        }
        occ = {"q0": {"term": "이상", "block_index": 3}}
        result = JevClient.answers_to_occurrences(occ, answers, min_confidence=0.9)
        assert result[(3, "이상")] is None

    def test_missing_answer_is_none(self):
        result = JevClient.answers_to_occurrences(
            {"q0": {"term": "a", "block_index": 0}}, {}
        )
        assert result[(0, "a")] is None


# ============================================================
# 2. _apply_jev_disambiguation —— 逐位置语义
# ============================================================

def _builder_with(text_blocks, proper_terms):
    class _B:
        pass
    b = _B()
    b.unified_request = {
        "text_blocks": text_blocks,
        "reference": {"proper_terms": proper_terms},
    }
    return b


class TestApplyJevDisambiguation:
    def _processor(self):
        p = FileProcessor.__new__(FileProcessor)
        p._config = TranslateConfig()
        p._recorder = None
        return p

    def test_removes_only_disputed_position(self):
        """同一术语在块0不适用、块1适用：块0引用被移除，块1保留。"""
        p = self._processor()
        builder = _builder_with(
            [
                {"id": 1, "kr": "이상의 힘!", "proper_refs": ["이상"]},
                {"id": 2, "kr": "이상이 말했다.", "proper_refs": ["이상"]},
            ],
            [{"term": "이상", "translation": "李箱", "note": ""}],
        )
        n = p._apply_jev_disambiguation(
            builder, {(0, "이상"): "not_applicable"}
        )
        assert n == 1
        blocks = builder.unified_request["text_blocks"]
        assert blocks[0].get("proper_refs") in (None, [])
        assert blocks[1]["proper_refs"] == ["이상"]
        # reference 仍保留（块1 还在用）
        assert builder.unified_request["reference"]["proper_terms"] == [
            {"term": "이상", "translation": "李箱", "note": ""}
        ]

    def test_prunes_term_when_no_block_references_it(self):
        """术语在所有出现位置都不适用：从 reference 裁剪。"""
        p = self._processor()
        builder = _builder_with(
            [{"id": 1, "kr": "hello", "proper_refs": ["피주머니"]}],
            [{"term": "피주머니", "translation": "血袋", "note": ""}],
        )
        p._apply_jev_disambiguation(builder, {(0, "피주머니"): "not_applicable"})
        assert builder.unified_request["reference"]["proper_terms"] == []

    def test_empty_excluded_keeps_everything(self):
        p = self._processor()
        builder = _builder_with(
            [{"id": 1, "kr": "hi", "proper_refs": ["a"]}],
            [{"term": "a", "translation": "A"}],
        )
        n = p._apply_jev_disambiguation(builder, {})
        assert n == 0
        assert builder.unified_request["text_blocks"][0]["proper_refs"] == ["a"]
        assert builder.unified_request["reference"]["proper_terms"] == [
            {"term": "a", "translation": "A"}
        ]

    def test_other_terms_untouched(self):
        """排除 A 不影响同一块其他术语 B。"""
        p = self._processor()
        builder = _builder_with(
            [{"id": 1, "kr": "hi", "proper_refs": ["A", "B"]}],
            [{"term": "A", "translation": "A"}, {"term": "B", "translation": "B"}],
        )
        p._apply_jev_disambiguation(builder, {(0, "A"): "not_applicable"})
        assert builder.unified_request["text_blocks"][0]["proper_refs"] == ["B"]


# ============================================================
# 3. _try_jev_disambiguation —— 回退与成功路径
# ============================================================

class _FakeJevClient:
    """可注入答案或失败的 JevClient 替身。"""

    def __init__(self, *, answers=None, raise_on=None):
        self._answers = answers or {}
        self._raise_on = raise_on
        self.calls = []

    def evaluate(self, state, questions):
        self.calls.append((state, questions))
        if self._raise_on is not None:
            raise self._raise_on
        return self._answers


def _fake_client_factory(impl):
    import translateFunc.proper.jev as jev_mod

    class _Patch:
        def __init__(self, *a, **kw):
            self._impl = impl
        def evaluate(self, state, questions):
            return self._impl.evaluate(state, questions)
    jev_mod.JevClient = _Patch
    return (jev_mod, _Patch)


class TestTryJevDisambiguation:
    def _processor(self, monkeypatch, config=None):
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
        p = FileProcessor.__new__(FileProcessor)
        p._config = config or TranslateConfig(
            jev_enabled=True, jev_api_key_env="TYPESAFE_API_KEY"
        )
        p._recorder = None

        class _PathConfig:
            real_name = "Sample.json"
        p.path_config = _PathConfig()
        return p

    def _builder_with_ambiguous(self, ambiguous_terms, blocks, proper_terms):
        class _B:
            pass
        b = _B()
        b.unified_request = {
            "text_blocks": blocks,
            "reference": {"proper_terms": proper_terms},
        }
        b.max_length = 20000
        return b

    def test_disabled_returns_false(self, monkeypatch):
        p = self._processor(monkeypatch, TranslateConfig(jev_enabled=False))
        # 不构造 JevClient：应直接返回 False
        assert p._try_jev_disambiguation(None, []) is False

    def test_missing_key_returns_false(self, monkeypatch):
        p = self._processor(monkeypatch)
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)  # 处理器创建后再移除
        assert p._try_jev_disambiguation(None, []) is False

    def test_success_applies_decisions(self, monkeypatch):
        blocks = [
            {"id": 1, "kr": "이상의 힘!", "proper_refs": ["이상"]},
            {"id": 2, "kr": "이상이 말했다.", "proper_refs": ["이상"]},
        ]
        ambiguous = [{
            "kr": "이상", "cn": "李箱", "note": "",
            "text_block_indices": [0, 1],
        }]
        proper_terms = [{"term": "이상", "translation": "李箱", "note": ""}]
        builder = self._builder_with_ambiguous(ambiguous, blocks, proper_terms)

        # fake Jev：q0 = 块0 不适用，q1 = 块1 适用
        fake = _FakeJevClient()
        calls = {}

        import translateFunc.proper.jev as jev_mod
        real = jev_mod.JevClient

        class _Patch(real):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                fake_base = "x"
                self._api_key = "x"
            def evaluate(self, state, questions):
                calls["state"] = state
                calls["questions"] = questions
                fake.evaluate(state, questions)
                # 解析问题集，构造对应答案
                answers = {}
                for i, qid in enumerate(sorted(questions)):
                    answers[qid] = JevAnswer(
                        qid, "choice",
                        choice=JevChoice(
                            "not_applicable" if i == 0 else "applicable",
                            1.0, {},
                        ),
                    )
                return answers

        monkeypatch.setattr(jev_mod, "JevClient", _Patch)
        p = self._processor(monkeypatch)
        ok = p._try_jev_disambiguation(builder, ambiguous)
        assert ok is True
        # 块0 的 이상 引用被移除，块1 保留
        assert builder.unified_request["text_blocks"][0].get("proper_refs") in (None, [])
        assert builder.unified_request["text_blocks"][1]["proper_refs"] == ["이상"]
        # 问题集按出现位置构建
        assert len(calls["questions"]) == 2

    def test_failure_falls_back(self, monkeypatch):
        blocks = [{"id": 1, "kr": "hi", "proper_refs": ["a"]}]
        ambiguous = [{"kr": "a", "cn": "A", "note": "", "text_block_indices": [0]}]
        builder = self._builder_with_ambiguous(
            ambiguous, blocks,
            [{"term": "a", "translation": "A", "note": ""}],
        )

        import translateFunc.proper.jev as jev_mod
        real = jev_mod.JevClient

        class _EvalError(real):
            def __init__(self, *a, **kw):
                self._dummy = None
            def evaluate(self, state, questions):
                return None  # 请求失败

        monkeypatch.setattr(jev_mod, "JevClient", _EvalError)
        p = self._processor(monkeypatch)
        assert p._try_jev_disambiguation(builder, ambiguous) is False
        # 回退路径不修改任何块
        assert builder.unified_request["text_blocks"][0]["proper_refs"] == ["a"]


# ============================================================
# 4. 真实端点冒烟（默认跳过；JEV_TEST_LIVE=1 时运行）
# ============================================================

def _load_env() -> dict:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return {}
    out = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


LIVE = pytest.mark.skipif(
    os.getenv("JEV_TEST_LIVE", "") != "1",
    reason="真实端点冒烟：设置 JEV_TEST_LIVE=1 且 .env 存在时运行",
)


@LIVE
def test_live_official_endpoint_schema():
    """官方端点难例组合：이상 / 피주머니 / 호위무사。"""
    env = _load_env()
    key = env.get("JEV_OFFICIAL_API_KEY", "")
    assert key, "缺少 JEV_OFFICIAL_API_KEY"
    client = JevClient(key, base_url=env.get(
        "JEV_OFFICIAL_BASE_URL", JevClient.DEFAULT_BASE_URL
    ))
    state = {
        "blocks": [
            {"kr": "이상의 힘으로 적을 공격한다.", "jp": "", "en": ""},
            {"kr": "이상이 말했다.", "jp": "", "en": ""},
            {"kr": "호위무사가 상자에서 피주머니를 꺼냈다.", "jp": "", "en": ""},
        ]
    }
    questions = {
        "q0": JevClient.choice_question(
            "判断 `blocks[0].kr` 中 `이상` 是否表示人名「李箱」",
            {"applicable": "人名李箱", "not_applicable": "数值比较/其他"},
        ),
        "q1": JevClient.choice_question(
            "判断 `blocks[1].kr` 中 `이상` 是否表示人名「李箱」",
            {"applicable": "人名李箱", "not_applicable": "数值比较/其他"},
        ),
        "q2": JevClient.choice_question(
            "判断 `blocks[2].kr` 中 `피주머니` 是否表示词表译名「血袋」",
            {"applicable": "血袋", "not_applicable": "其他含义"},
        ),
    }
    answers = client.evaluate(state, questions)
    assert answers, f"官方端点无答案: {answers}"
    assert len(answers) == 3
    # 结构断言（不锁死语义，Jev 多语言能力需实测校准）
    for qid, ans in answers.items():
        assert ans.qtype == "choice"
        assert ans.choice is not None
        assert ans.choice.chosen in ("applicable", "not_applicable")


@LIVE
def test_live_personal_endpoint_schema():
    """个人代理端点（nekopeer）输出结构与官方一致。"""
    env = _load_env()
    key = env.get("JEV_PERSONAL_API_KEY", "")
    base = env.get("JEV_PERSONAL_BASE_URL", "")
    assert key and base, "缺少 JEV_PERSONAL_API_KEY / JEV_PERSONAL_BASE_URL"
    client = JevClient(key, base_url=base, verify=False)
    answers = client.evaluate(
        {"kr": "이상이 말했다.", "jp": "", "en": ""},
        {
            "q0": JevClient.choice_question(
                "判断 `kr` 中 `이상` 是否表示人名「李箱」",
                {"applicable": "人名李箱", "not_applicable": "其他"},
            ),
        },
    )
    assert answers and answers["q0"].qtype == "choice"