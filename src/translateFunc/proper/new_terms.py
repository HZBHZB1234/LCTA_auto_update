"""
translateFunc/proper/new_terms.py
模型回传的新专有名词 —— 收集、落盘与回流合并。

LLM 在阶段 1 的响应里附带 ``new_terms``（原文中出现、但 glossary 未收录的
专有名词）。本模块负责把它变成可复用的术语资产：

  1. ``NewTermCollector`` —— 噪音过滤 + 按 ``(kr, cn)`` 计票，线程安全
     （翻译主流程跑在 WorkerPool 里，多个文件会并发回传）；
  2. ``flush()`` —— 原子写盘到 ``proper_learned.json``（tmp + os.replace）；
  3. ``merge_terms()`` —— 下次运行启动时并入术语表，**远程表优先**：
     人工维护的 paratranz 一旦收录该词，本地建议自动失效，避免两边打架。

落盘文件中 ``terms`` 数组的 ``term``/``translation``/``note`` 三个字段与
``ProperAnalyzer.fetch_terms(proper_path=...)`` 的 schema 一致，因此这个文件
本身就可以直接当 ``proper_path`` 使用。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import logging
import os
import re
import threading

_logger = logging.getLogger("LCTA")  # 与 LogManager 一致，确保日志正确路由

SCHEMA_VERSION = 1

# 落盘文件名（放在 output_dir 下，跨运行累积）
DEFAULT_FILE_NAME = "proper_learned.json"

NOTE_TAG = "llm-learned"

VALID_CATEGORIES = {"person", "place", "org", "item", "skill", "other"}

# 候选里出现这些字符，基本可以判定是句子片段 / 占位符 / 富文本，不是专有名词
_REJECT_CHARS = re.compile(r"[{}\[\]<>\\\n\r\t\"“”]")
# 谚文音节 + 字母 + 兼容字母
_RE_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")
# KR 术语只接受：谚文 + 拉丁字母数字 + 少量连接符
_RE_TERM_SHAPE = re.compile(r"^[\w\uac00-\ud7a3\u3131-\u318e .·\-'&]+$")

MIN_TERM_LEN = 2
MAX_TERM_LEN = 24
MAX_CN_LEN = 40
MAX_SOURCES_PER_TERM = 5


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def sanitize_entry(entry: dict) -> dict | None:
    """把一条模型回传的候选规范化为 ``{term, translation, category}``。

    返回 ``None`` 表示判定为噪音，直接丢弃。
    """
    if not isinstance(entry, dict):
        return None

    kr = _clean(entry.get("kr", entry.get("term", "")))
    cn = _clean(entry.get("cn", entry.get("translation", "")))

    if not kr or not cn:
        return None
    if not (MIN_TERM_LEN <= len(kr) <= MAX_TERM_LEN):
        return None
    if len(cn) > MAX_CN_LEN:
        return None
    # 名词原形不应含占位符/富文本/引号等结构字符
    if _REJECT_CHARS.search(kr):
        return None
    if not _RE_TERM_SHAPE.match(kr):
        return None
    # 译文未中文化（原样回填或仍是韩文）时不算有效译名
    if cn == kr or _RE_HANGUL.search(cn):
        return None

    category = _clean(entry.get("category", "")).lower()
    if category not in VALID_CATEGORIES:
        category = "other"

    return {"term": kr, "translation": cn, "category": category}


class NewTermCollector:
    """线程安全的新专有名词收集器。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        min_votes: int = 1,
        min_term_len: int = MIN_TERM_LEN,
        max_term_len: int = MAX_TERM_LEN,
    ):
        self.path = Path(path) if path else None
        self._min_votes = max(1, int(min_votes))
        self._min_term_len = int(min_term_len)
        self._max_term_len = int(max_term_len)

        self._lock = threading.Lock()
        # (kr, cn) -> 票数 / 元数据
        self._votes: dict[tuple[str, str], int] = {}
        self._meta: dict[tuple[str, str], dict] = {}
        # 等待热更新的条目（已被收集但尚未并入 AC 自动机）
        self._pending_hot: dict[str, dict] = {}
        # 从落盘文件载入的既有词条，(kr, cn) 计票的初始值
        self._loaded_terms: set[str] = set()

        if self.path is not None and self.path.exists():
            try:
                for item in load_learned(self.path):
                    term = _clean(item.get("term", ""))
                    if not term:
                        continue
                    self._loaded_terms.add(term)
                    cn = _clean(item.get("translation", ""))
                    if cn:
                        key = (term, cn)
                        self._votes[key] = max(1, int(item.get("votes", 1) or 1))
                        self._meta[key] = {
                            "category": item.get("category", "other"),
                            "sources": list(item.get("sources", []))[:MAX_SOURCES_PER_TERM],
                        }
            except Exception as exc:  # noqa: BLE001 - 载入失败不阻塞翻译
                _logger.warning(f"新专有名词文件载入失败 ({self.path}): {exc}")

    # ----- 收集 -----

    def mark_known(self, terms) -> int:
        """把已在术语表中的词标记为已知，避免被当成"新词"重复学习。"""
        with self._lock:
            added = 0
            for term in terms or ():
                cleaned = _clean(term)
                if cleaned and cleaned not in self._loaded_terms:
                    self._loaded_terms.add(cleaned)
                    added += 1
            return added

    def add(
        self,
        entries: list[dict],
        *,
        source: str = "",
        known_terms: set[str] | None = None,
    ) -> int:
        """收录一批模型回传的候选，返回被接受的条数。"""
        if not entries:
            return 0

        known = known_terms or set()
        accepted = 0
        with self._lock:
            for entry in entries:
                item = sanitize_entry(entry)
                if item is None:
                    continue
                term = item["term"]
                if len(term) < self._min_term_len or len(term) > self._max_term_len:
                    continue
                # 已在术语表（远程或本地历史）中的词不再是「新词」
                if term in known or term in self._loaded_terms:
                    continue

                key = (term, item["translation"])
                self._votes[key] = self._votes.get(key, 0) + 1
                meta = self._meta.setdefault(key, {"category": item["category"], "sources": []})
                if source and source not in meta["sources"]:
                    if len(meta["sources"]) < MAX_SOURCES_PER_TERM:
                        meta["sources"].append(source)
                if item["category"] != "other" and meta.get("category") == "other":
                    meta["category"] = item["category"]
                accepted += 1

            if accepted:
                # 只有达到票数门槛的词才进入热更新候选
                for key, votes in self._votes.items():
                    if votes >= self._min_votes and key[0] not in self._pending_hot:
                        self._pending_hot[key[0]] = self._compose(key)
        return accepted

    def take_hot_update_batch(self, threshold: int) -> list[dict]:
        """取出累积到阈值的新词用于热更新；不足阈值时返回空列表。"""
        if threshold <= 0:
            return []
        with self._lock:
            if len(self._pending_hot) < threshold:
                return []
            batch = list(self._pending_hot.values())
            self._pending_hot.clear()
            return batch

    # ----- 产出 -----

    def snapshot(self) -> list[dict]:
        """当前收集到的全部词条（同一 KR 取票数最高的译名）。"""
        with self._lock:
            best: dict[str, tuple[tuple[str, str], int]] = {}
            for key, votes in self._votes.items():
                if votes < self._min_votes:
                    continue
                kr = key[0]
                current = best.get(kr)
                if current is None or votes > current[1]:
                    best[kr] = (key, votes)
            return [self._compose(key) for key, _ in best.values()]

    def _compose(self, key: tuple[str, str]) -> dict:
        """由 ``(kr, cn)`` 组装落盘条目。调用方需持有锁。"""
        meta = self._meta.get(key, {})
        return {
            "term": key[0],
            "translation": key[1],
            "note": NOTE_TAG,
            "category": meta.get("category", "other"),
            "votes": self._votes.get(key, 1),
            "sources": list(meta.get("sources", [])),
        }

    def flush(self) -> int:
        """原子写盘。返回写入的词条数。"""
        if self.path is None:
            return 0
        terms = self.snapshot()
        if not terms:
            return 0

        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "terms": terms,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响本次翻译
            _logger.warning(f"新专有名词落盘失败 ({self.path}): {exc}")
            return 0
        _logger.info(f"新专有名词已落盘 {len(terms)} 条 → {self.path}")
        return len(terms)

    def __len__(self) -> int:
        with self._lock:
            return len(self._votes)


def load_learned(path: Path | str) -> list[dict]:
    """读取落盘的新词文件，返回 ``terms`` 列表（兼容裸列表格式）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 文件损坏时按空处理
        return []
    if isinstance(data, dict):
        terms = data.get("terms", [])
    elif isinstance(data, list):
        terms = data
    else:
        return []
    return [t for t in terms if isinstance(t, dict)]


def merge_terms(base: list[dict], learned: list[dict]) -> tuple[list[dict], dict]:
    """把本地新词并入术语表，**远程表优先**。

    Args:
        base: 远程/人工维护的术语表（权威）
        learned: 本地累积的新词

    Returns:
        ``(合并后的术语表, {"added": n, "shadowed": m})``
        —— shadowed 是被远程表覆盖掉的本地建议数。
    """
    merged = list(base)
    existing = {_clean(t.get("term", "")) for t in merged}
    added = 0
    shadowed = 0

    for item in learned:
        term = _clean(item.get("term", ""))
        translation = _clean(item.get("translation", ""))
        if not term or not translation:
            continue
        if term in existing:
            shadowed += 1
            continue
        merged.append({
            "term": term,
            "translation": translation,
            "note": _clean(item.get("note", "")) or NOTE_TAG,
        })
        existing.add(term)
        added += 1

    return merged, {"added": added, "shadowed": shadowed}
