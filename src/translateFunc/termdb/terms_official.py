"""N1/N3：官方整句级 hard 权威取证（零模型、可复核）。

从 t2 的 aligned_units.jsonl 取 KR/JP/EN/zh 对齐单元；优先 non-stale 证据，
若某字段所在文件整体 stale，则用 committed blob（kr_blob_at_zh_sync 与
kr_blob_at_raw_head）做**字段级未变证明**——字段未变则官方译文仍适用，
不把整文件的 stale 标记带入词条（避免"3 个月前旧译名当当前权威"）。

产物写入 SQLite（source='official'），经 build_flat_view 派生 hard 词条。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config
from .lib import cat_file_batch, load_json_lenient, read_json
from .terms_schema import (
    MATCH_EXACT_SENTENCE, entity_key, evidence_id, make_term,
)
from .terms_score import TermLifecycle
from .terms_store import TermStore

# 三条 N1/N3 官方措辞：KR 锚点 + 官方中文（含尾随半角空格的原样保留）
OFFICIAL_SPECS: tuple[dict, ...] = (
    {
        "slug": "burn-trailing-space",
        "kr": "화상 횟수 1 증가",
        "zh": "对攻击者施加1层烧伤 ",
        "note": "N1：Buff 状态名后保留半角空格，禁止 strip",
    },
    {
        "slug": "charge-count-layer",
        "kr": "충전 횟수",
        "zh": "充能 层数",
        "note": "N3：官方措辞为『充能 层数』而非『充能次数』",
    },
    {
        "slug": "coin-power-fixed-1",
        "kr": "코인 위력이 1이 됨",
        "zh": "硬币威力固定为1",
        "note": "N3：官方整句『硬币威力固定为1』，非『变为1』",
    },
    # ---- t23 / iteration-02 §7 第 3 项：quote 官方整句硬锁 ----
    {
        "slug": "quote-announcer-matthias-kira",
        "kr": ("딱 한 번, 아빠의 짱쎈검을 휘두르면 전부 죽을 모양새인데…\n"
               "후들후들 흔들리는데 버티고 있구만… 근성있네.\n"
               "저런 녀석들이 마지막 한 방이다…! 하면서 힘을 모으면 최종보스를 쓰러트리긴 하더라."),
        "zh": ("明明只要老爸轻轻一挥那把超强的剑，对面就全死了……\n"
               "那种即使踉踉跄跄也依然咬牙忍耐的样子……不是挺有毅力的吗。\n"
               "那种人大概是一边说着“这就是最后一击了……！”一边蓄力，最后打倒大BOSS的类型吧。"),
        "note": "P1 quote：官方用中文双引号“”，全文逐字锁定，禁「」",
    },
    {
        "slug": "quote-someday-short",
        "kr": "언젠가라는 말을 남용한 것만 같소.",
        "zh": "太过频繁地提起“总有一天”了。",
        "note": "P1 quote 短句：官方措辞『太过频繁地提起“总有一天”了。』",
    },
    # ---- t23 / iteration-02 §7 第 4 项：BUFF desc 贴合 ----
    {
        "slug": "buff-charge-count-extra",
        "kr": "충전 횟수 +1",
        "zh": "充能 层数额外+1层",
        "note": "P1 BUFF desc：官方为『充能 层数额外+1层』，非『充能 层数 +1』",
    },
    {
        "slug": "buff-charge-potency",
        "kr": "충전 위력",
        "zh": "充能 强度",
        "note": "P1 BUFF desc：官方为『充能 强度』，非『充能威力』",
    },
)


def _field_value(doc: Any, path: str) -> Any:
    """沿 'dataList.#59.summary' 路径取值（#N 为列表索引）。"""
    cur = doc
    for seg in path.split("."):
        if seg.startswith("#"):
            cur = cur[int(seg[1:])]
        else:
            cur = cur[seg]
    return cur


def field_unchanged(repo: Path, file_ev: dict, path: str) -> tuple[bool, str, str]:
    """字段级未变证明：返回 (是否未变, zh同步时KR blob, raw HEAD 的KR blob)。"""
    old = str(file_ev.get("kr_blob_at_zh_sync", "") or "")
    new = str(file_ev.get("kr_blob_at_raw_head", "") or "")
    if not (old and new):
        return False, old, new
    if old == new:
        return True, old, new
    try:
        blobs = cat_file_batch(repo, [old, new])
        a = _field_value(load_json_lenient(blobs[old].decode("utf-8")), path)
        b = _field_value(load_json_lenient(blobs[new].decode("utf-8")), path)
        return a == b, old, new
    except Exception:  # noqa: BLE001 - 证明失败即不采信，不引入 stale
        return False, old, new


def _identity(root: Path) -> dict:
    try:
        return read_json(config.snapshot_manifest_path(root)).get("identity", {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _load_units(repo: Path) -> list[dict]:
    path = config.aligned_units_path(repo)
    if not path.is_file():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                u = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(u, dict):
                out.append(u)
    return out


def ingest_official(store: TermStore, root: Path, mirror: Path | None = None) -> dict:
    """取证并入库三条官方整句级 hard 词条，返回逐条证据报告。"""
    repo = mirror or config.default_mirror(root)
    units = _load_units(repo)
    files_path = config.derived_dir(repo) / "aligned_files.json"
    file_map: dict[str, dict] = {}
    if files_path.is_file():
        try:
            file_map = {f["file"]: f for f in read_json(files_path)["files"]}
        except Exception:  # noqa: BLE001
            file_map = {}
    ident = _identity(root)
    llc_tag = str(ident.get("llc_tag", "") or "")
    commit_sha = str(ident.get("commit_sha", "") or "")
    raw_token = str(ident.get("raw_token", "") or "")

    stats: dict[str, Any] = {"ingested": 0, "evidence": []}
    for spec in OFFICIAL_SPECS:
        hits = [u for u in units
                if spec["zh"] in (u.get("zh") or "")
                and spec["kr"] in (u.get("kr") or "")]
        choices: list[tuple[int, dict, dict, tuple[bool, str, str]]] = []
        for u in hits:
            fe = file_map.get(u.get("file", ""), {})
            ok, old, new = field_unchanged(repo, fe, u.get("path", ""))
            # 有效证据 = 原本 non-stale，或 stale 但字段级未变
            effective_stale = bool(u.get("stale")) and not ok
            choices.append((int(effective_stale), u, fe, (ok, old, new)))
        # 必须先排序再判定：语料顺序里的第一个命中可能是 stale 且字段已变，
        # 但后面还有 non-stale 命中；未排序就判空会误丢有效证据。
        choices.sort(key=lambda x: (x[0], str(x[1].get("file")), str(x[1].get("path"))))
        if not choices or choices[0][0] != 0:
            # 该措辞没有任何有效（非 stale 或字段未变）证据 → 不入库
            stats["evidence"].append({
                "slug": spec["slug"], "status": "no_valid_evidence",
                "candidates": len(hits),
            })
            continue
        _, u, fe, (ok, old, new) = choices[0]

        blobs = fe.get("blob_sha256") or {}
        blob_kr = str(blobs.get("KR", "") or "")
        blob_zh = str(blobs.get("LLC_zh-CN", "") or "")
        path = str(u.get("path", "") or "")
        file = str(u.get("file", "") or "")
        key = entity_key(f"official:{spec['slug']}", MATCH_EXACT_SENTENCE)

        ev_ids = [
            evidence_id(file, path, "exact_sentence", blob_zh),
            f"kr-field-invariant:{old}:{new}:{path}" if ok else f"non-stale:{file}:{path}",
        ]
        doc = make_term(
            entity_id=f"official:{spec['slug']}",
            sense_id=MATCH_EXACT_SENTENCE,
            revision=1,
            kr=spec["kr"],
            zh=spec["zh"],
            jp=str(u.get("jp", "") or ""),
            en=str(u.get("en", "") or ""),
            kind=str(u.get("kind", "") or ""),
            domains=[str(u.get("kind", "") or "mechanic"), "official-sentence"],
            match=MATCH_EXACT_SENTENCE,
            evidence=ev_ids,
            llc_tag=llc_tag, commit_sha=commit_sha, raw_token=raw_token,
            blob_kr=blob_kr, blob_zh=blob_zh,
            status="authoritative",
            stale=False,  # non-stale 或字段级未变证明后才入库
        )
        doc["scope"]["fields"] = [path]
        doc["scope"]["entity_refs"] = [f"{file}:{path}"]
        doc["source_binding"].update({
            "kr_blob_at_zh_sync": old,
            "kr_blob_at_raw_head": new,
            "field_unchanged": ok,
            "zh_last_commit": str(fe.get("zh_last_commit", "") or ""),
        })
        doc["note"] = spec["note"]

        life = TermLifecycle.from_evidence(
            struct=1.0, consensus=1.0, stale=False,
            match=MATCH_EXACT_SENTENCE, version_gap=0,
        )
        store.upsert(
            term_key=key, entity_id=f"official:{spec['slug']}",
            sense_id=MATCH_EXACT_SENTENCE, revision=1,
            state=life.state, tier=life.tier, score=life.score,
            lifecycle=life.as_dict(), doc=doc, source="official",
            created_revision=llc_tag,
        )
        stats["ingested"] += 1
        stats["evidence"].append({
            "slug": spec["slug"], "status": "hard",
            "kr": spec["kr"], "zh": spec["zh"],
            "file": file, "path": path,
            "unit_stale": bool(u.get("stale")),
            "field_unchanged": ok,
            "llc_tag": llc_tag, "commit_sha": commit_sha,
            "blob_kr": blob_kr, "blob_zh": blob_zh,
            "tier": life.tier, "state": life.state, "score": life.score,
        })
    return stats


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="官方整句级 hard 取证")
    ap.add_argument("--root", default="")
    ap.add_argument("--mirror", default="")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    mirror = Path(args.mirror).resolve() if args.mirror else None
    db = config.artifact_dir(root) / "termdb.sqlite3"
    with TermStore(db) as store:
        result = ingest_official(store, root, mirror)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
