"""专有名词数据源加载。

优先读取自维护术语库的**扁平兼容导出**（docs/termdb/proper_terms.json，
裸 JSON 数组，每条含 term/translation/note，运行时不依赖存档仓库）；
导出不存在时回退远程 paratranz 表，保持旧行为与签名不变。

注意：term-v1 富格式（docs/termdb/terms.jsonl）仅供离线审校，
运行时绝不直接消费——其词面在 forms.kr，旧代码只认 term，直接吃会全盘失效。
"""
from __future__ import annotations

import json
from pathlib import Path

LOCAL_EXPORT = Path(__file__).resolve().parents[2] / "docs" / "termdb" / "proper_terms.json"


def _load_local(path: Path | None = None) -> list[dict] | None:
    p = Path(path) if path else LOCAL_EXPORT
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 导出损坏时回退远程，不阻塞翻译
        return None
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    return None


def fetch_local(path: Path | str | None = None, min_len: int = 0) -> list[dict]:
    """读取本地导出；不存在或非数组时返回空列表（不抛异常）。"""
    items = _load_local(Path(path) if path else None) or []
    if min_len > 0:
        return [t for t in items if len(str(t.get("term", ""))) >= min_len]
    return items


def fetch(min_len: int = 0, *, prefer_local: bool = True) -> list[dict]:
    """获取专有名词表。签名与旧版兼容（min_len 位置参数）。"""
    if prefer_local:
        local = _load_local()
        if local is not None:
            return [t for t in local
                    if len(str(t.get("term", ""))) >= min_len] if min_len > 0 else local

    import requests
    r = requests.get("https://web.lcta.top/proper_terms_paratranz.json", timeout=10)
    r.raise_for_status()
    data = r.json()
    if min_len > 0 and isinstance(data, list):
        return [t for t in data
                if isinstance(t, dict) and len(str(t.get("term", ""))) >= min_len]
    return data
