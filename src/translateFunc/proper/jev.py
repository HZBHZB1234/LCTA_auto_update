"""
translateFunc/proper/jev.py
Jev System One 决策模型客户端 —— 给翻译管线提供廉价的"判断"能力。

Jev 是 TypeSafe 的 System One 决策模型：输入文本 state，输出结构化概率
答案（Noul: 0~1 概率；Choice: 选项 + confidence + 概率分布；Score: 等级评分），
不产生成式文本。价位 $0.042 / 1M 输入 token、输出免费（2026-09 官方定价）。

本项目用途（与 docs/2026-09-25 归档路线一致）：
  1. 阶段 0 逐位置消歧：对每个 text_block 中每个 proper_ref，
     判断该术语在此处是否适用、属于哪个含义（替换生成模型消歧）。
  2. 新词候选验证、正式例句 rerank（后续阶段）。

本模块只做"客户端与代数"部分，不依赖任何 translateFunc 内部状态，
以便单测独立运行；与管线的胶合在 processor.py 中完成。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

_logger = logging.getLogger("LCTA")


class JevError(Exception):
    """Jev 请求失败（网络、HTTP 或 schema 异常）。"""


@dataclass(frozen=True)
class JevChoice:
    """Choice 问题的答案。"""
    chosen: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class JevAnswer:
    """对单个问题的回答。"""
    question_id: str
    qtype: str              # "noul" | "choice" | "score"
    noul: Optional[float] = None
    choice: Optional[JevChoice] = None
    score: Optional[float] = None

    @property
    def is_high_confidence(self) -> bool:
        """Choice 类型是否高置信度（0.9 阈值，调用方可另行判定）。"""
        if self.qtype == "choice" and self.choice is not None:
            return self.choice.confidence >= 0.9
        return False


class JevClient:
    """TypeSafe Jev /v1/systemone 客户端。

    - 一次请求可携带多个问题（Jev 并行独立评估，一次请求 ≈ 一次请求延迟）；
    - 支持官方端点与任何兼容代理（如 nekopeer 个人端点）；
    - 请求级失败返回 None（或抛 JevError），由调用方决定回退。
    """

    # Jev 官方端点（代理端点可覆盖 base_url）
    DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
    # 钉死模型版本：jev-latest 会随官方发布漂移，推理结果和阈值都依赖版本
    DEFAULT_MODEL = "jev-1.13.0"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        verify: bool = True,
        cache: Optional[dict] = None,
    ):
        if not api_key:
            raise JevError("Jev API key 为空")
        self._api_key = api_key
        # base_url 可含完整 /systemone 路径（官方或代理端点），不要重复追加
        self._base_url = base_url.rstrip("/")
        if not self._base_url.endswith("/systemone"):
            self._base_url += "/systemone"
        if not self._base_url.startswith("http"):
            self._base_url = "https://" + self._base_url
        self._model = model
        self._timeout = timeout
        self._verify = verify

        # 传入共享缓存 dict 则可跨请求缓存 (state, questions, model) -> answers
        # 缓存键为 todo 规范化 JSON；由调用方决定是否共享/持久化
        self._cache: dict = cache if cache is not None else {}

    # ----- 请求构建 -----

    @staticmethod
    def _question(type_: str, instructions: str, criteria: dict | None):
        q: dict[str, Any] = {"type": type_, "instructions": instructions}
        if criteria is not None:
            q["criteria"] = criteria
        return q

    @staticmethod
    def noul_question(instructions: str) -> dict:
        return JevClient._question("noul", instructions, None)

    @staticmethod
    def choice_question(instructions: str, criteria: dict) -> dict:
        """criteria: {选项id: 选项含义描述}。选项数 ≤ 255（官方上限）。"""
        return JevClient._question("choice", instructions, criteria)

    @staticmethod
    def score_question(instructions: str, criteria: dict) -> dict:
        """criteria: {等级id: 等级描述}，等级数 2~10（官方上限）。"""
        return JevClient._question("score", instructions, criteria)

    # ----- 核心调用 -----

    def evaluate(
        self,
        state: Any,
        questions: dict[str, dict],
        *,
        model: str | None = None,
    ) -> dict[str, JevAnswer] | None:
        """对同一 state 并行评估 map<问题id, 问题>。

        Returns:
            {问题id: JevAnswer}；请求失败返回 None。
        """
        if not questions:
            return {}
        model = model or self._model

        cache_key = json.dumps(
            {"state": state, "questions": questions, "model": model},
            ensure_ascii=False, sort_keys=True,
        )
        if cache_key in self._cache:
            return self._cache[cache_key]

        payload = {
            "state": state,
            "model": model,
            "questions": questions,
        }
        answer_map = self._post(payload)
        if answer_map is None:
            return None

        parsed: dict[str, JevAnswer] = {}
        for qid, raw in answer_map.items():
            parsed[qid] = self._parse_answer(qid, raw)
        self._cache[cache_key] = parsed
        return parsed

    def _post(self, payload: dict) -> dict[str, Any] | None:
        """POST /systemone。网络/HTTP 失败返回 None，schema 严重异常抛 JevError。"""
        try:
            resp = requests.post(
                self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
                verify=self._verify,
            )
        except requests.RequestException as exc:
            _logger.warning("Jev 请求失败 (%s %s): %s", self._base_url, self._model, exc)
            return None

        if resp.status_code != 200:
            _logger.warning(
                "Jev HTTP %s (%s %s): %s",
                resp.status_code, self._base_url, self._model,
                resp.text[:500],
            )
            return None

        try:
            body = resp.json()
            answers = body.get("answers")
            if not isinstance(answers, dict):
                _logger.warning("Jev 响应缺少 answers 映射: %s", str(body)[:300])
                return None
            return answers
        except ValueError as exc:
            raise JevError(f"Jev 响应非 JSON: {exc}") from exc

    @staticmethod
    def _parse_answer(qid: str, raw: Any) -> JevAnswer:
        if not isinstance(raw, dict):
            raise JevError(f"问题 {qid!r} 的回答不是对象: {raw!r}")
        qtype = raw.get("type", "")
        if qtype == "noul":
            noul = raw.get("noul")
            return JevAnswer(qid, "noul", noul=float(noul) if noul is not None else None)
        if qtype == "choice":
            choice = JevChoice(
                chosen=str(raw.get("choice", "")),
                confidence=float(raw.get("confidence", 0.0)),
                probabilities=dict(raw.get("probabilities", {})),
            )
            return JevAnswer(qid, "choice", choice=choice)
        if qtype == "score":
            return JevAnswer(qid, "score", score=float(raw.get("score")))
        raise JevError(f"问题 {qid!r} 未知类型: {qtype!r}")

    # ----- 便捷工厂：本项目场景 -----

    @staticmethod
    def build_block_state(blocks: list[dict]) -> dict:
        """把待消歧的文本块打包成 Jev state 结构。

        每个块保留 kr/jp/en 三语文本（如有），并用 index 定位。
        问题指令中用 backtick 引用，例如：`blocks[0].kr`
        """
        return {
            "blocks": [
                {
                    "kr": b.get("kr", ""),
                    "jp": b.get("jp", ""),
                    "en": b.get("en", ""),
                }
                for b in blocks
            ]
        }

    @staticmethod
    def build_occurrence_questions(
        state: dict,
        occurrences: list[dict],
    ) -> dict[str, dict]:
        """为逐位置消歧构建 Jev Choice 问题集（单一 state 上并行评估）。

        Args:
            state: build_block_state() 的输出 —— 全部文本块
            occurrences: [{
                "term": "이상",
                "block_index": 0,        # 对应 state["blocks"][i]
                "candidates": {"applicable": "该术语此处适用，使用词表译名",
                               "not_applicable": "此处不适用（非该含义）", ...},
                "instructions": "判断 `이상` 在 `blocks[0].kr` 中的含义。",
            }, ...]

        Returns:
            {"q0": Choice问题, "q1": ...}  —— 输出必须绑定位置与术语。
        """
        questions: dict[str, dict] = {}
        for i, occ in enumerate(occurrences):
            questions[f"q{i}"] = JevClient.choice_question(
                occ.get("instructions", ""),
                occ.get("candidates", {}),
            )
        return questions

    @staticmethod
    def occurrence_ids(occurrences: list[dict]) -> dict[str, dict]:
        """生成 {"q<i>": occurrence} 映射，用于把答案对齐回 occurrence。"""
        return {f"q{i}": occ for i, occ in enumerate(occurrences)}

    @staticmethod
    def answers_to_occurrences(
        qid_to_occ: dict[str, dict],
        answers: dict[str, JevAnswer],
        *,
        min_confidence: float = 0.0,
    ) -> dict[tuple[int, str], str | None]:
        """把 Jev answers 映射回每个 (block_index, term) 的最终选定含义。

        Args:
            qid_to_occ: {"q0": occurrence, ...}
            answers: evaluate() 的返回值
            min_confidence: Choice 置信度低于此值的判定不采纳（返回 None）

        Returns:
            {(block_index, term): 选定含义id 或 None(无答案/低置信度)}
        """
        result: dict[tuple[int, str], str | None] = {}
        for qid, occ in qid_to_occ.items():
            ans = answers.get(qid)
            chosen: str | None = None
            if ans is not None and ans.qtype == "choice" and ans.choice is not None:
                if ans.choice.confidence >= min_confidence:
                    chosen = ans.choice.chosen
            result[(occ.get("block_index", -1), occ.get("term", ""))] = chosen
        return result