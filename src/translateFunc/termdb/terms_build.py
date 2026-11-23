"""termdb 离线构建：L0 结构化名称入库 / L1 正文候选报告 / L2-L3 候选池 / 导出。

唯一写入源是 SQLite（terms_store.TermStore）；JSONL revision 与
proper_terms.json 扁平视图均为只读派生导出。全流程零模型、零人工审批。
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from . import config, terms_official
from .lib import read_json, write_json
from .terms_schema import (
    MATCH_EXACT_SENTENCE, MATCH_NAME_ONLY, STATE_ACTIVE, STATE_CONFLICT, STATE_DEPRECATED,
    TIER_CANDIDATE, TIER_HARD, TIER_SOFT, entity_key, evidence_id,
    is_single_syllable, make_term, sense_from_field,
)
from .terms_score import TermLifecycle
from .terms_store import DB_NAME, TermStore

# 产物文件名
TERMS_JSONL = "terms.jsonl"
COMPAT_JSON = "proper_terms.json"
L1_REPORT_JSON = "l1_prose_candidates.json"
TERMS_REPORT_JSON = "terms_report.json"

# L1 扫描上限（离线报告，只读；限制表面数保证构建耗时可控）
L1_MAX_SURFACES = 3000
L1_MIN_LEN = 2
L1_MAX_REPORT = 300

# SkillTag 资产（BLACK/WHITE/AMBER/NAVY/...）的 KR 词面是**颜色名**，官方中文
# 却是**罪种/属性名**（검정색→哀叹、흰색→疯狂、노란색→怠惰…）。这是同名不同
# 语义的对齐，不是同义词；只能出现在名称字段，正文「흰색 옷」绝不能套「疯狂」。
COLOR_TAG_TERMS = frozenset({
    "검정색", "흰색", "노란색", "남색", "보라색",
    "빨간색", "주황색", "초록색", "파란색",
})

# 仅为 title 字段出现过的词条：标题语境的译名（如 기사→司机、지도→地图）
# 不适用于正文，统一降为 name_only，由运行时按字段挡在正文之外。
TITLE_ONLY_FIELDS = frozenset({"title"})


# --------------------------------------------------------------------------
# L0：结构化名称入库
# --------------------------------------------------------------------------

def _load_name_pairs(root: Path) -> list[dict]:
    path = config.artifact_dir(root) / "name_pairs_with_evidence.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def namespaced_entity_id(row: dict) -> str:
    """id 在同一文件内也可能重复（不同 path 的同名记录），必须带 file+path。"""
    eid = str(row.get("entity_id") or "")
    file = str(row.get("file") or "").replace("\\", "/")
    path = str(row.get("path") or "")
    kind = str(row.get("kind", "") or "")
    stem = f"{kind}:{file}:{eid}" if file else f"{kind}:{eid}"
    return f"{stem}:{path}" if path else stem


def _group_rows(rows: Iterable[dict]) -> dict[str, dict[tuple, list[dict]]]:
    """kr -> {(namespaced_entity_id, sense_id): [row, ...]}。"""
    out: dict[str, dict[tuple, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        kr = (r.get("kr") or "").strip()
        if not kr:
            continue
        eid = namespaced_entity_id(r)
        sense = sense_from_field(r.get("field"))
        out[kr][(eid, sense)].append(r)
    return out


def _identity(root: Path) -> dict:
    try:
        snap = read_json(config.snapshot_manifest_path(root))
        return snap.get("identity", {}) or {}
    except Exception:
        return {}


def ingest_l0(store: TermStore, root: Path) -> dict:
    """从 t2 的 name_pairs_with_evidence.jsonl 构建 L0 词条。

    stale 判定：只要该 (kr, entity, sense) 组内任一行 stale=true，整条标 stale，
    由 tier_for 收口为 soft/candidate，绝不进入 active/hard。
    """
    ident = _identity(root)
    llc_tag = str(ident.get("llc_tag", "") or "")
    commit_sha = str(ident.get("commit_sha", "") or "")
    raw_token = str(ident.get("raw_token", "") or "")

    grouped = _group_rows(_load_name_pairs(root))
    stats = Counter()
    seen_keys: set[str] = set()

    for kr, by_entity in sorted(grouped.items()):
        all_rows = [r for rows in by_entity.values() for r in rows]
        zh_all = {(r.get("zh") or "").strip() for r in all_rows}
        zh_all.discard("")
        is_conflict = len(zh_all) > 1
        single = is_single_syllable(kr)
        match = MATCH_NAME_ONLY if single else "unbound"

        # 冲突词：按 kr 收敛为一条隔离词条，保留全部译法在 allowed_variants
        if is_conflict:
            subgroups: dict[tuple, list[dict]] = {
                (f"conflict:{kr}", "conflict"): all_rows,
            }
        else:
            subgroups = by_entity

        for (eid, sense), rows in sorted(subgroups.items()):
            key = entity_key(eid, sense)
            seen_keys.add(key)
            zh_pref = sorted(zh_all)[0] if is_conflict else (rows[0].get("zh") or "").strip()
            variants = sorted(zh_all)[1:] if is_conflict else []
            stale = any(bool(r.get("stale")) for r in rows)

            ev_ids: list[str] = []
            blob_kr = blob_zh = ""
            kind = ""
            for r in rows:
                blobs = r.get("blob_sha256") or {}
                bk = str(blobs.get("KR", "") or "")
                bz = str(blobs.get("LLC_zh-CN", "") or "")
                blob_kr = blob_kr or bk
                blob_zh = blob_zh or bz
                ev_ids.append(evidence_id(r.get("file"), r.get("path"),
                                          r.get("field"), bz))
                kind = kind or str(r.get("kind", "") or "")

            doc = make_term(
                entity_id=eid, sense_id=sense, revision=1,
                kr=kr, zh=zh_pref, kind=kind,
                match=match, evidence=sorted(set(ev_ids)),
                llc_tag=llc_tag, commit_sha=commit_sha, raw_token=raw_token,
                blob_kr=blob_kr, blob_zh=blob_zh,
                status="conflict" if is_conflict else "authoritative",
                stale=stale,
            )
            if variants:
                doc["zh"]["allowed_variants"] = variants

            # 复用历史：保留落位反馈；冲突/衰减词条遇新权威证据可复活
            existing = store.get(key)
            life = TermLifecycle.from_evidence(
                struct=1.0, consensus=1.0 / max(1, len(zh_all)),
                stale=stale, match=match, version_gap=0, revision=1,
            )
            if existing:
                old_life = TermLifecycle.from_dict(existing["lifecycle"])
                life.components.place = old_life.components.place
                life.consecutive_fail = old_life.consecutive_fail
                old_doc = existing["doc"]
                same_zh = old_doc.get("zh", {}).get("preferred") == zh_pref
                if (
                    same_zh and not stale and not is_conflict
                    and old_life.state in (STATE_CONFLICT, STATE_DEPRECATED)
                ):
                    life.confirm_official_name()  # 新版名称字段复活
                old_rev = int(old_doc.get("revision", 1))
                if old_doc.get("zh", {}).get("preferred") != zh_pref or old_doc.get("stale") != stale:
                    life.revision = old_rev + 1
                    life.supersedes_revision = old_rev
                else:
                    life.revision = old_rev
                    life.supersedes_revision = old_doc.get("supersedes_revision")
                doc["revision"] = life.revision
                doc["supersedes_revision"] = life.supersedes_revision

            if is_conflict:
                life.state = STATE_CONFLICT

            store.upsert(
                term_key=key, entity_id=eid, sense_id=sense,
                revision=life.revision, state=life.state, tier=life.tier,
                score=life.score, lifecycle=life.as_dict(), doc=doc,
                source="corpus",
                created_revision=llc_tag,
            )
            stats[f"state_{life.state}"] += 1
            stats[f"tier_{life.tier}"] += 1
            if stale:
                stats["stale"] += 1
            if single:
                stats["name_only"] += 1
            if is_conflict:
                stats["conflict"] += 1

    # 语料中已消失的 corpus 词条自动撤回（不删 landing_events / L2 / L3）
    for rec in list(store.all(source="corpus")):
        old_key = _term_key_of(rec)
        if old_key not in seen_keys:
            store.conn.execute("DELETE FROM terms WHERE term_key=?", (old_key,))
            store.conn.commit()
            stats["withdrawn"] += 1

    return dict(stats)


# --------------------------------------------------------------------------
# L1：正文诱导候选（只读报告）+ L2 候选池
# --------------------------------------------------------------------------

def build_l1_report(store: TermStore, root: Path, mirror: Path | None = None) -> dict:
    """在已译对上做 KR 词面 × zh 子串共现；只产出报告 + L2 候选池，不进强制档。"""
    repo = mirror or config.default_mirror(root)
    units_path = config.aligned_units_path(repo)
    surfaces: dict[str, tuple[str, str]] = {}  # kr -> (term_key, zh)
    for rec in store.all():
        if rec["tier"] == TIER_CANDIDATE:
            continue
        doc = rec["doc"]
        kr = (doc.get("forms", {}).get("kr") or [""])[0]
        if len(kr) < L1_MIN_LEN:
            continue
        zh = doc.get("zh", {}).get("preferred", "")
        if kr and zh:
            surfaces[kr] = (f"{doc['entity_id']}|{doc['sense_id']}", zh)
    picked = sorted(surfaces.items(), key=lambda kv: (-len(kv[0]), kv[0]))[:L1_MAX_SURFACES]
    if not picked:
        out = {"scanned_units": 0, "surface_count": 0, "candidates": []}
        write_json(config.artifact_dir(root) / L1_REPORT_JSON, out)
        return out

    # 用 Aho-Corasick 一次遍历取代大正则（3k 备选的 | 正则在 27 万行上会退化）
    import sys
    src = config.PROJECT_ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from translateFunc.matcher.ac_automaton import AcAutomaton  # noqa: WPS433
    ac = AcAutomaton()
    for surface, _ in picked:
        ac.add_pattern(surface, data=surface)
    ac.build()
    meta = dict(picked)
    scanned = 0
    hit = Counter()
    landed = Counter()
    samples: dict[str, dict] = {}

    if units_path.is_file():
        with units_path.open(encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    u = json.loads(line)
                except json.JSONDecodeError:
                    continue
                scanned += 1
                kr_text = u.get("kr", "") or ""
                if not kr_text:
                    continue
                found = {m.pattern for m in ac.search(kr_text)}
                if not found:
                    continue
                zh_text = u.get("zh", "") or ""
                for s in found:
                    hit[s] += 1
                    tk, zh = meta[s]
                    if zh and zh in zh_text:
                        landed[s] += 1
                    elif s not in samples:
                        samples[s] = {
                            "kr": s, "zh": zh, "term_key": tk,
                            "kr_sample": kr_text[:120], "zh_sample": zh_text[:120],
                            "file": u.get("file", ""), "path": u.get("path", ""),
                        }

    candidates = []
    for s, n in hit.most_common(L1_MAX_REPORT):
        tk, zh = meta[s]
        candidates.append({
            "kr": s, "zh": zh, "term_key": tk,
            "surface_hits": n, "zh_landed": landed.get(s, 0),
            "land_rate": round(landed.get(s, 0) / n, 4) if n else 0.0,
            "sample": samples.get(s),
        })

    out = {
        "scanned_units": scanned,
        "surface_count": len(picked),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "note": "L1 只读报告：candidate 不进 proper_terms.json，不进翻译强制区",
    }
    write_json(config.artifact_dir(root) / L1_REPORT_JSON, out)
    return out


def import_l2_candidates(store: TermStore, root: Path) -> int:
    """把 L1 报告中 land_rate < 0.5 的表面并入 L2 候选池（state=candidate）。"""
    path = config.artifact_dir(root) / L1_REPORT_JSON
    if not path.is_file():
        return 0
    report = read_json(path)
    added = 0
    for c in report.get("candidates", []):
        if c.get("land_rate", 0.0) >= 0.5:
            continue
        kr = c.get("kr", "")
        zh = c.get("zh", "")
        if not (kr and zh):
            continue
        key = f"l2|{kr}"
        doc = make_term(
            entity_id=f"l2:{kr}", sense_id="prose", revision=1,
            kr=kr, zh=zh, match="unbound", status="pending",
            evidence=[f"l1-report:{kr}"],
        )
        life = TermLifecycle(state="candidate")
        store.upsert(
            term_key=key, entity_id=f"l2:{kr}", sense_id="prose", revision=1,
            state=life.state, tier=life.tier, score=life.score,
            lifecycle=life.as_dict(), doc=doc, source="l2",
        )
        added += 1
    return added


def import_l3_learned(store: TermStore, path: Path | str) -> int:
    """模型回传新词（proper_learned.json）只进 L3 候选池，永不进强制档。"""
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return 0
    terms = data.get("terms", []) if isinstance(data, dict) else data
    if not isinstance(terms, list):
        return 0
    added = 0
    for t in terms:
        if not isinstance(t, dict):
            continue
        kr = (t.get("term") or t.get("kr") or "").strip()
        zh = (t.get("translation") or t.get("cn") or "").strip()
        if not (kr and zh):
            continue
        key = f"l3|{kr}"
        doc = make_term(
            entity_id=f"l3:{kr}", sense_id="model", revision=1,
            kr=kr, zh=zh, match=MATCH_NAME_ONLY if is_single_syllable(kr) else "unbound",
            status="pending", evidence=[f"model:{t.get('source', 'learned')}"],
        )
        doc["zh"]["allowed_variants"] = list(t.get("sources", []))[:5]
        life = TermLifecycle(state="candidate")
        store.upsert(
            term_key=key, entity_id=f"l3:{kr}", sense_id="model", revision=1,
            state=life.state, tier=life.tier, score=life.score,
            lifecycle=life.as_dict(), doc=doc, source="l3",
        )
        added += 1
    return added


# --------------------------------------------------------------------------
# 自动矫正入口（落位回流 / 版本衰减）
# --------------------------------------------------------------------------

def apply_landing(store: TermStore, term_key: str, landing: str, note: str = "") -> dict:
    """回流一次落位结果；状态转移时回写 SQLite。"""
    rec = store.get(term_key)
    if rec is None:
        return {"found": False, "term_key": term_key}
    life = TermLifecycle.from_dict(rec["lifecycle"])
    before = life.as_dict()
    changed = life.observe_landing(landing)
    store.record_landing(term_key, landing, note)
    # S_place / 连续失败计数即使未触发状态转移也必须回写，
    # 否则下一次观测会从上一次的旧值重新计数，永远到不了第 3 次降级。
    after = life.as_dict()
    if after != before:
        doc = rec["doc"]
        doc["status"] = "conflict" if life.state == "conflict" else doc.get("status", "authoritative")
        store.upsert(
            term_key=term_key, entity_id=doc["entity_id"], sense_id=doc["sense_id"],
            revision=life.revision, state=life.state, tier=life.tier,
            score=life.score, lifecycle=after, doc=doc,
            source=rec["source"],
        )
    return {
        "found": True, "term_key": term_key, "landing": landing,
        "state": life.state, "tier": life.tier, "score": life.score,
        "consecutive_fail": life.consecutive_fail, "changed": changed,
    }


def advance_versions(store: TermStore, version_gap: int, root: Path) -> dict:
    """快照版本前进：S_fresh 衰减，掉出强制档。"""
    stats = Counter()
    for rec in list(store.all()):
        life = TermLifecycle.from_dict(rec["lifecycle"])
        before = life.as_dict()
        changed = life.advance_version(version_gap)
        after = life.as_dict()
        # freshness 变化也要回写，即便档位未变
        if after != before:
            doc = rec["doc"]
            store.upsert(
                term_key=f"{doc['entity_id']}|{doc['sense_id']}",
                entity_id=doc["entity_id"], sense_id=doc["sense_id"],
                revision=life.revision, state=life.state, tier=life.tier,
                score=life.score, lifecycle=after, doc=doc,
                source=rec["source"],
            )
            if changed:
                stats[f"state_{life.state}"] += 1
    return dict(stats)


# --------------------------------------------------------------------------
# 导出（只读派生）
# --------------------------------------------------------------------------

def _term_key_of(rec: dict) -> str:
    doc = rec["doc"]
    return f"{doc.get('entity_id', '')}|{doc.get('sense_id', '')}"


def export_revision_jsonl(store: TermStore, root: Path) -> Path:
    """富格式 term-v1 导出：每行一个 JSON 对象，稳定排序、可重放。"""
    out = config.artifact_dir(root) / TERMS_JSONL
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in store.all():
            doc = dict(rec["doc"])
            doc["state"] = rec["lifecycle"]["state"]
            doc["tier"] = rec["tier"]
            doc["score"] = rec["lifecycle"]["score"]
            doc["score_components"] = rec["lifecycle"]["components"]
            doc["consecutive_fail"] = rec["lifecycle"]["consecutive_fail"]
            fh.write(json.dumps(doc, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")) + "\n")
    return out


def build_flat_view(store: TermStore) -> list[dict]:
    """扁平兼容视图：JSON 数组，一 term 一行，只含 hard/soft + active/provisional。

    每条必含 term(KR词面)/translation(中文)/note，镜像 kr/cn；
    额外挂 tier/score/state/scope/sense/category（旧代码忽略未知字段）。

    **domains 取同一词面所有合格记录的并集**：拍平时按 (score, entity|sense)
    只留一条，若直接沿用该条的 domains，会把同时出现在 mechanic 与
    ui_or_data 的官方词（如 적응/부적응）锁死在单一域，运行时域门就永远
    命不中。并集只放宽作用域，不改变译名与档位。
    """
    best: dict[str, tuple[float, str, dict]] = {}
    domains_by_kr: dict[str, set[str]] = defaultdict(set)
    fields_by_kr: dict[str, set[str]] = defaultdict(set)
    for rec in store.all():
        if rec["tier"] not in (TIER_HARD, TIER_SOFT):
            continue
        if rec["state"] not in ("active", "provisional"):
            continue
        doc = rec["doc"]
        kr = (doc.get("forms", {}).get("kr") or [""])[0]
        cn = doc.get("zh", {}).get("preferred", "")
        if not (kr and cn):
            continue
        scope = doc.get("scope") or {}
        for domain in scope.get("domains") or []:
            if domain:
                domains_by_kr[kr].add(str(domain))
        for field in scope.get("fields") or []:
            if field:
                fields_by_kr[kr].add(str(field))
        life = rec["lifecycle"]
        flat = {
            "term": kr,
            "kr": kr,
            "translation": cn,
            "cn": cn,
            "note": f"termdb:{rec['tier']}|{rec['state']}|v{doc.get('revision', 1)}|"
                    f"{';'.join((doc.get('evidence_ids') or [])[:1])}",
            "tier": rec["tier"],
            "score": life["score"],
            "state": rec["state"],
            "scope": dict(scope),
            "sense": doc.get("sense_id", ""),
            "category": (scope.get("domains") or ["other"])[0],
            "entity_id": doc.get("entity_id", ""),
            "revision": doc.get("revision", 1),
            "stale": bool(doc.get("stale", False)),
        }
        key = kr
        cand = (float(life["score"]), f"{doc.get('entity_id', '')}|{doc.get('sense_id', '')}", flat)
        if key not in best or cand[:2] > best[key][:2]:
            best[key] = cand

    view: list[dict] = []
    for key, (_, _, flat) in sorted(best.items()):
        merged = domains_by_kr.get(key)
        if merged:
            flat["scope"]["domains"] = sorted(merged)
        # 字段严格化（不改 match/tier，避免硬档被降成 soft）：
        #   * 软档且全部证据只来自 title 字段 → 只在 title 类字段可见
        #     （기사→司机 这类只在标题语境成立的译名不得进正文）；
        #     硬档不套用：硬档标题多为地区/等级名（11구→11区），正文里通常
        #     也能直接译对，限制反而降低连贯性；语法残片由前界校验兜底。
        #   * SkillTag 颜色词 → 只在名称字段可见（흰색 옷 不得套「疯狂」）。
        fields_union = fields_by_kr.get(key) or set()
        if (
            flat["tier"] == TIER_SOFT
            and fields_union and fields_union <= TITLE_ONLY_FIELDS
        ):
            flat["scope"]["fields"] = sorted(TITLE_ONLY_FIELDS)
            flat["scope"]["field_strict"] = True
        elif key in COLOR_TAG_TERMS:
            flat["scope"]["fields"] = list(flat["scope"].get("fields") or ["name"])
            flat["scope"]["field_strict"] = True
        view.append(flat)
    return view


def export_compat(store: TermStore, root: Path) -> Path:
    """裸 JSON 数组（无包装）——运行时 get_proper/analyze/build_proper 直接遍历。"""
    out = config.artifact_dir(root) / COMPAT_JSON
    items = build_flat_view(store)
    out.write_text(
        json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n",
    )
    return out


def write_terms_report(store: TermStore, root: Path, extra: dict | None = None) -> Path:
    report = {
        "schema_version": 1,
        "counts": store.count(),
        "flat_export_count": len(build_flat_view(store)),
        "identity": _identity(root),
    }
    if extra:
        report.update(extra)
    out = config.artifact_dir(root) / TERMS_REPORT_JSON
    write_json(out, report)
    return out


def build_all(root: Path, *, mirror: Path | None = None,
              l1: bool = True, l2: bool = True) -> dict:
    """完整离线构建：打开 SQLite（唯一写入源）→ L0 → L1 → L2 → 导出。"""
    root = Path(root).resolve()
    db = config.artifact_dir(root) / DB_NAME
    out: dict[str, Any] = {"db": str(db)}
    with TermStore(db) as store:
        out["l0"] = ingest_l0(store, root)
        if l1:
            out["l1"] = build_l1_report(store, root, mirror)
        if l2:
            out["l2_candidates"] = import_l2_candidates(store, root)
        # N1/N3：官方整句级 hard 权威（字段级证据，零 stale）
        out["official"] = terms_official.ingest_official(store, root, mirror)
        out["export_jsonl"] = str(export_revision_jsonl(store, root))
        out["export_compat"] = str(export_compat(store, root))
        out["flat_count"] = len(build_flat_view(store))
        out["counts"] = store.count()
        write_terms_report(store, root, {
            "l0": out["l0"],
            "flat_count": out["flat_count"],
            "l1_candidates": (out.get("l1") or {}).get("candidate_count", 0),
            "official": out["official"],
        })
        store.set_meta("last_build", {"llc_tag": _identity(root).get("llc_tag")})
    return out


def verify_export(store: TermStore, root: Path, mirror: Path | None = None) -> dict:
    """验收自检：兼容契约 + stale/单体词/冲突隔离 + 运行时回读零丢失。"""
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": bool(ok), "detail": detail})

    flat_path = config.artifact_dir(root) / COMPAT_JSON
    raw = flat_path.read_text(encoding="utf-8")
    flat = json.loads(raw)
    check("compat_is_json_array", isinstance(flat, list), f"type={type(flat).__name__}")
    bad = [x for x in flat if not (
        isinstance(x, dict) and x.get("term") and x.get("translation")
        and isinstance(x.get("note"), str)
        and x.get("kr") == x.get("term")
        and x.get("cn") == x.get("translation")
    )]
    check("compat_required_fields", not bad, f"bad={len(bad)}")
    check("compat_one_row_per_term",
          len(flat) == len({x["term"] for x in flat}),
          f"rows={len(flat)} unique={len({x['term'] for x in flat})}")
    bad_tier = [x for x in flat if x.get("tier") not in (TIER_HARD, TIER_SOFT)
                or x.get("state") not in ("active", "provisional")]
    check("compat_only_active_provisional_hard_soft", not bad_tier, f"bad={len(bad_tier)}")

    # SQLite 内部不变量
    conn = store.conn
    hard_rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT doc FROM terms WHERE tier='hard'")]
    check("no_stale_in_hard", not [x for x in hard_rows if x.get("stale")],
          f"hard={len(hard_rows)}")
    check("no_name_only_in_hard",
          not [x for x in hard_rows if x.get("scope", {}).get("match") == MATCH_NAME_ONLY])
    conflict_rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT doc FROM terms WHERE state='conflict'")]
    check("conflicts_isolated", bool(conflict_rows)
          and all(x.get("status") == "conflict" for x in conflict_rows),
          f"conflicts={len(conflict_rows)}")

    # 运行时回读：ProperAnalyzer.analyze / build_proper / render_glossary 词面零丢失
    try:
        import sys
        src = config.PROJECT_ROOT / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from translateFunc.matcher.proper import ProperAnalyzer  # noqa: WPS433
        from translateFunc.matcher.engine import MatcherEngine  # noqa: WPS433
        from translateFunc.builder.prompt import PromptFactory  # noqa: WPS433
        analyzer = ProperAnalyzer()
        terms = analyzer.analyze(flat)
        check("runtime_analyze_term_surface", len(terms) == len(flat),
              f"terms={len(terms)} flat={len(flat)}")
        engine = MatcherEngine()
        engine.build_proper(flat)
        check("runtime_ac_pattern_count", len(engine.proper_terms) == len(flat),
              f"patterns={len(engine.proper_terms)}")
        glossary = PromptFactory().render_glossary(flat)
        check("runtime_glossary_nonempty", "<kr>" in glossary and "<cn>" in glossary)
    except Exception as exc:  # noqa: BLE001 - 验收要明确暴露异常
        check("runtime_playback", False, f"{type(exc).__name__}: {exc}")

    # N1/N3：官方整句级 hard 条目与证据链
    official_rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT doc FROM terms WHERE source='official'")]
    for spec in terms_official.OFFICIAL_SPECS:
        hit = [d for d in official_rows
               if d.get("zh", {}).get("preferred") == spec["zh"]]
        check(f"official_hard_{spec['slug']}",
              len(hit) == 1 and hit[0].get("stale") is False
              and hit[0].get("scope", {}).get("match") == MATCH_EXACT_SENTENCE,
              f"hits={len(hit)}")
        if hit:
            sb = hit[0].get("source_binding", {})
            check(f"official_evidence_{spec['slug']}",
                  sb.get("llc_tag") == "2026111901"
                  and bool(sb.get("commit_sha")) and bool(sb.get("source_fingerprint"))
                  and bool(hit[0].get("evidence_ids")),
                  f"llc_tag={sb.get('llc_tag')} commit={str(sb.get('commit_sha'))[:8]}")

    # P0 / iteration-03：官方整句必须在 RG-BUFF-TRAILING-SPACE 的真实注入中命中。
    # 复刻 A/B harness 的 select_glossary：按 term 在 KR/JP/EN 上的子串命中，
    # 按 (-len(term), term) 排序后取前 N（长词面优先）。
    try:
        golden = read_json(config.PROJECT_ROOT / "tests" / "golden" / "regression_cases.json")
        target = next((c for c in golden.get("cases", [])
                       if c.get("case_id") == "RG-BUFF-TRAILING-SPACE"), None)
        if target:
            # 单 block：直接拼三语文本；不能对字符串再 join（会逐字符插换行）
            hay = " ".join(str(target.get(k) or "")
                           for k in ("kr", "jp", "en"))
            hit_terms = [t for t in flat if t.get("term") and t["term"] in hay]
            hit_terms.sort(key=lambda t: (-len(t["term"]), t["term"]))
            injected = hit_terms[:14]
            landed = [t for t in injected
                      if t.get("cn") == "充能 层数额外+1层" and t.get("tier") == "hard"]
            check("official_lands_in_RG_BUFF_TRAILING_SPACE",
                  len(landed) == 1,
                  f"injected={len(injected)} landed={len(landed)} "
                  f"top={ascii([t['term'] for t in injected[:3]])}")
    except Exception as exc:  # noqa: BLE001 - 验收要明确暴露异常
        check("official_lands_in_RG_BUFF_TRAILING_SPACE", False,
              f"{type(exc).__name__}: {exc}")

    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "flat_count": len(flat),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="构建自维护术语库（term-v1）")
    ap.add_argument("--root", default="")
    ap.add_argument("--mirror", default="")
    ap.add_argument("--skip-l1", action="store_true", help="跳过 L1 正文扫描（快速重建）")
    ap.add_argument("--skip-l2", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    mirror = Path(args.mirror).resolve() if args.mirror else None
    out = build_all(root, mirror=mirror, l1=not args.skip_l1, l2=not args.skip_l2)
    print(json.dumps({
        "db": out["db"], "flat_count": out["flat_count"],
        "counts": out["counts"], "l0": out["l0"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
