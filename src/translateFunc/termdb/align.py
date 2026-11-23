"""四语字段对齐 + stale 标记 + utf-8-sig/BOM 保真 + 对齐报告。

输出
----
1. docs/termdb/alignment_report.json / .md   —— 可核验对齐报告（确定性，重跑无 diff）
2. <mirror>/_derived/aligned_units.jsonl     —— 紧凑四语单元（供 termdb 下游消费）
3. <mirror>/_derived/aligned_files.json      —— 文件级证据（BOM/blob/stale/版本）

口径与 _diag_analysis/glossary_feasibility.py 一致（plan §2.1 / §11）。

stale 定义（plan §2.2）：中文侧只在 Translation Sync 提交里更新，raw 只在 RAW Update
提交里更新。若某 zh 文件最后一次同步时看到的 KR blob ≠ 当前最新 raw commit 的 KR blob，
说明原文在之后变过而译文未跟上 → stale=true。锚点来自 git 对象 id，不靠文件 mtime。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import config
from .lib import (CJK, HANGUL, KANA, PLACEHOLDER, commit_meta, evidence_bundle,
                  flatten, git, git_ok, is_ancestor, load_json_file, ls_tree,
                  path_key, read_json, write_json, write_text, zh_rel_paths)
from .config import file_kind, is_name_path


REPORT_SCHEMA = "termdb-alignment/1"


def _last_zh_commits(mirror: Path) -> dict[str, str]:
    """rel -> 最近一次改动该 zh 文件的 commit（git log 一次扫描）。"""
    fmt = "@@%H"
    out = git(mirror, "log", f"--format={fmt}", "--name-only", "--", config.ZH)
    result: dict[str, str] = {}
    current = ""
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("@@"):
            current = line[2:]
        elif current and line.endswith(".json") and line not in result:
            result[line.removeprefix(config.ZH + "/")] = current
    return result


def _raw_head(mirror: Path) -> str:
    sha = git(mirror, "log", "-1", "--format=%H", f"--grep={config.RAW_SUBJECT}", "HEAD").strip()
    return sha


def _kr_blob_map(mirror: Path, sha: str) -> dict[str, str]:
    """rel -> KR blob sha（去掉 KR/ 前缀）。"""
    return {p.removeprefix("KR/"): v["sha"]
            for p, v in ls_tree(mirror, sha, "KR").items()
            if p.startswith("KR/")}


def _stale_context(mirror: Path, rels: list[str]) -> dict:
    raw_head = _raw_head(mirror)
    last = _last_zh_commits(mirror)
    kr_raw = _kr_blob_map(mirror, raw_head) if raw_head else {}
    cache: dict[str, dict[str, str]] = {}
    ctx = {}
    for rel in rels:
        zh_commit = last.get(rel, "")
        kr_at_raw = kr_raw.get(rel)
        kr_at_zh = None
        if zh_commit:
            if zh_commit not in cache:
                cache[zh_commit] = _kr_blob_map(mirror, zh_commit)
            kr_at_zh = cache[zh_commit].get(rel)
        if not kr_at_raw or not kr_at_zh:
            stale = None
        else:
            stale = kr_at_zh != kr_at_raw
        ctx[rel] = {
            "stale": stale,
            "zh_last_commit": zh_commit,
            "raw_head_commit": raw_head,
            "kr_blob_at_zh_sync": kr_at_zh,
            "kr_blob_at_raw_head": kr_at_raw,
            "raw_is_ancestor_of_zh_sync": bool(raw_head and zh_commit and
                                              is_ancestor(mirror, raw_head, zh_commit)),
        }
    return ctx


def _raw_only_files(mirror: Path) -> dict:
    """raw 侧存在但 zh 无对应文件的清单 —— 明确不是“待补译”。"""
    sets = {}
    for lang in config.LANGS:
        tree = ls_tree(mirror, "HEAD", lang)
        sets[lang] = {p.removeprefix(lang + "/") for p in tree
                      if p.startswith(lang + "/") and p.endswith(".json")}
    zh = sets[config.ZH]
    out = {}
    for lang in ("KR", "JP", "EN"):
        only = sorted(sets[lang] - zh)
        out[lang] = {
            "count": len(only),
            "sample": only[:10],
            "semantics": "raw 文件无官方中文资产对应（新增/未本地化资源），不计入待补译",
        }
    out["file_counts"] = {lang: len(s) for lang, s in sorted(sets.items())}
    return out


def _unit_flags(vals: dict[str, str]) -> dict[str, bool]:
    kr, jp, en, zh = (vals["KR"], vals["JP"], vals["EN"], vals[config.ZH])
    return {
        "kr_empty": not kr.strip(),
        "jp_empty": not jp.strip(),
        "en_empty": not en.strip(),
        "zh_empty": not zh.strip(),
        "zh_no_cjk": bool(zh.strip()) and not bool(CJK.search(zh)),
        "zh_equal_en": bool(zh.strip()) and zh.strip() == en.strip(),
        "untranslated": bool(zh.strip()) and not CJK.search(zh) and zh.strip() == en.strip(),
        "kr_has_hangul": bool(HANGUL.search(kr)),
        "jp_has_kana": bool(KANA.search(jp)),
        "has_placeholder": bool(PLACEHOLDER.search(kr) or PLACEHOLDER.search(zh)),
    }


def build_alignment(mirror: Path, snapshot: dict | None = None) -> tuple[dict, Path, Path]:
    """执行对齐；返回 (报告 dict, aligned_units.jsonl, aligned_files.json)。"""
    if not mirror.is_dir():
        raise FileNotFoundError(f"镜像不存在: {mirror}")
    rels = zh_rel_paths(mirror)
    stale_ctx = _stale_context(mirror, rels)
    ident = (snapshot or {}).get("identity", {})
    ev_core = {
        "llc_tag": ident.get("llc_tag"),
        "raw_token": ident.get("raw_token"),
        "commit_sha": ident.get("commit_sha"),
    }

    # 证据哈希统一取 snapshot 的 committed blob sha256（不受 Windows CRLF 检出影响）
    snap_lookup = {}
    for f in (snapshot or {}).get("files", []):
        parts = f["path"].split("/", 1)
        if len(parts) == 2 and parts[0] in config.LANGS:
            snap_lookup[(parts[0], parts[1])] = f

    derived = config.derived_dir(mirror)
    derived.mkdir(parents=True, exist_ok=True)
    units_path = config.aligned_units_path(mirror)
    files_path = derived / "aligned_files.json"

    stats = Counter()
    per_kind = Counter()
    bom_by_lang = Counter()
    field_common_total = 0
    field_union_total = 0
    aligned_files = 0
    missing_counterpart: list[str] = []
    path_mismatch: list[tuple[str, int]] = []
    file_evidence = []
    samples = []

    with units_path.open("w", encoding="utf-8", newline="\n") as fh:
        for rel in rels:
            docs: dict[str, dict] = {}
            bom: dict[str, bool] = {}
            blob: dict[str, str] = {}
            ok = True
            for lang in config.LANGS:
                p = mirror / lang / rel
                if not p.is_file():
                    ok = False
                    break
                try:
                    doc, tf = load_json_file(p)
                except Exception:
                    ok = False
                    break
                docs[lang] = flatten(doc)
                bf = snap_lookup.get((lang, rel))
                bom[lang] = bf["bom"] if bf else tf.bom
                blob[lang] = bf["sha256"] if bf else tf.sha256
            if not ok:
                missing_counterpart.append(rel)
                continue
            aligned_files += 1
            for lang in config.LANGS:
                if bom[lang]:
                    bom_by_lang[lang] += 1

            sets = [set(docs[l]) for l in config.LANGS]
            common = set.intersection(*sets)
            union = set().union(*sets)
            only = union - common
            if only:
                path_mismatch.append((rel, len(only)))
            field_common_total += len(common)
            field_union_total += len(union)

            kind = file_kind(rel)
            sinfo = stale_ctx.get(rel, {})
            file_evidence.append({
                "file": rel,
                "kind": kind,
                "bom": bom,
                "blob_sha256": blob,
                "evidence": evidence_bundle(ev_core["llc_tag"], ev_core["raw_token"],
                                            ev_core["commit_sha"], blob),
                "stale": sinfo.get("stale"),
                "zh_last_commit": sinfo.get("zh_last_commit"),
                "raw_head_commit": sinfo.get("raw_head_commit"),
                "kr_blob_at_zh_sync": sinfo.get("kr_blob_at_zh_sync"),
                "kr_blob_at_raw_head": sinfo.get("kr_blob_at_raw_head"),
                "raw_is_ancestor_of_zh_sync": sinfo.get("raw_is_ancestor_of_zh_sync"),
                "fields_common": len(common),
                "fields_union": len(union),
            })

            for path in sorted(common, key=path_key):
                vals = {l: docs[l][path] for l in config.LANGS}
                flags = _unit_flags(vals)
                name = is_name_path(path)
                stale = sinfo.get("stale")
                unit = {
                    "file": rel,
                    "path": path_key(path),
                    "kind": kind,
                    "kr": vals["KR"], "jp": vals["JP"], "en": vals["EN"],
                    "zh": vals[config.ZH],
                    "flags": {k: v for k, v in flags.items() if v},
                    "name": name,
                    "stale": stale,
                }
                fh.write(json.dumps(unit, ensure_ascii=False, separators=(",", ":")) + "\n")
                stats["units_total"] += 1
                per_kind[kind] += 1
                for f, v in flags.items():
                    if v:
                        stats[f] += 1
                if name:
                    stats["name_units"] += 1
                if stale is True:
                    stats["stale_units"] += 1
                    per_kind[kind + "_stale"] += 1
                if len(samples) < 5 and name and not any(flags.values()):
                    samples.append({k: unit[k] for k in ("file", "path", "kr", "zh", "name", "stale")})

    write_json(files_path, {
        "schema": "termdb-aligned-files/1",
        "identity": ev_core,
        "files": file_evidence,
    })

    file_rate = aligned_files / len(rels) if rels else 0.0
    field_rate = field_common_total / field_union_total if field_union_total else 0.0
    raw_only = _raw_only_files(mirror)
    stale_known = stats["stale_units"]
    stale_total = stats["units_total"] - sum(
        1 for f in file_evidence if f["stale"] is None)
    # stale 比例按「有 stale 判定的单元」计
    stale_rate = stale_known / stale_total if stale_total else None

    report = {
        "schema": REPORT_SCHEMA,
        "identity": ev_core,
        "files": {
            "zh_total": len(rels),
            "aligned_four_way": aligned_files,
            "missing_counterpart": len(missing_counterpart),
            "path_mismatch_files": len(path_mismatch),
            "file_alignment_rate": round(file_rate, 5),
            "baseline_file_alignment_rate": config.BASELINE_FILE_ALIGN_RATE,
            "same_order_file": abs(file_rate - config.BASELINE_FILE_ALIGN_RATE) <= 0.02,
            "missing_counterpart_sample": missing_counterpart[:10],
            "path_mismatch_sample": [{"file": r, "only_fields": n} for r, n in path_mismatch[:10]],
        },
        "coverage_semantics": {
            "buckets": {
                "zh_missing_raw_counterpart": {
                    "count": len(missing_counterpart),
                    "meaning": "有官方中文、但某语 raw 无同名文件（无法做四语对照）",
                    "is_pending_translation": False,
                },
                "raw_without_zh_counterpart": {
                    "by_lang": {lang: raw_only[lang]["count"] for lang in ("KR", "JP", "EN")},
                    "meaning": "有 raw、无官方中文资产（新增/未本地化资源）",
                    "is_pending_translation": False,
                },
                "path_mismatch_in_aligned_file": {
                    "count": len(path_mismatch),
                    "meaning": "四语文件都存在，但字段路径集合不一致 —— 这才是字段缺失",
                    "is_pending_translation": False,
                },
            },
            "note": "三类计数互斥且均不等于「待补译」；不要把 raw-only 文件误判成待补译",
            "raw_only_detail": raw_only,
        },
        "fields": {
            "common_total": field_common_total,
            "union_total": field_union_total,
            "field_alignment_rate": round(field_rate, 6),
            "baseline_field_alignment_rate": config.BASELINE_FIELD_ALIGN_RATE,
            "same_order_field": abs(field_rate - config.BASELINE_FIELD_ALIGN_RATE) <= 0.002,
        },
        "units": {
            "total": stats["units_total"],
            "by_kind": dict(sorted((k, v) for k, v in per_kind.items() if not k.endswith("_stale"))),
            "name_units": stats["name_units"],
            "flags": {k: v for k, v in sorted(stats.items())
                      if k not in ("units_total", "name_units", "stale_units")},
            "samples": samples,
        },
        "stale": {
            "context": {
                "raw_head_commit": next((f["raw_head_commit"] for f in file_evidence
                                         if f.get("raw_head_commit")), ""),
                "zh_last_commits_distinct": len({f["zh_last_commit"] for f in file_evidence
                                                 if f.get("zh_last_commit")}),
                "definition": "KR blob at zh-file-last-sync != KR blob at latest RAW commit",
            },
            "units_stale_true": stale_known,
            "units_with_stale_verdict": stale_total,
            "stale_rate": round(stale_rate, 5) if stale_rate is not None else None,
            "by_kind_stale": dict(sorted((k[:-6], v) for k, v in per_kind.items()
                                         if k.endswith("_stale"))),
            "files_stale_true": sum(1 for f in file_evidence if f["stale"] is True),
        },
        "bom": {
            "files_by_lang": dict(sorted(bom_by_lang.items())),
            "total_files_with_any_bom": sum(1 for f in file_evidence if any(f["bom"].values())),
            "fake_diff_files": 0,  # 下方用 snapshot.files[].drift 严格覆盖
            "policy": "源文件只读 + utf-8-sig 解码 + 哈希取 committed blob 原始字节；"
                      "drift 按 CRLF→LF 归一后比较工作区与 blob；不写回，故无假 diff",
        },
        "artifacts": {
            "aligned_units": units_path.relative_to(mirror.parent).as_posix(),
            "aligned_files": files_path.relative_to(mirror.parent).as_posix(),
        },
    }

    # BOM 假 diff 严格核验：以 snapshot 的 drift 为准（工作区字节 == HEAD blob）
    if snapshot and snapshot.get("files"):
        drift_bom = [f["path"] for f in snapshot["files"] if f["bom"] and f["drift"]]
        report["bom"]["fake_diff_files"] = len(drift_bom)
        report["bom"]["fake_diff_sample"] = drift_bom[:10]
        report["bom"]["verified_against"] = "snapshot_manifest.files[].drift"

    return report, units_path, files_path


def render_markdown(report: dict) -> str:
    f = report["files"]
    d = report["fields"]
    s = report["stale"]
    b = report["bom"]
    u = report["units"]
    ident = report["identity"]
    lines = [
        "# 官方汉化语料对齐报告",
        "",
        f"> 生成器：`translateFunc.termdb.align` · schema `{report['schema']}`",
        "",
        "## 快照身份（证据链锚点）",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| LLC tag | `{ident.get('llc_tag')}` |",
        f"| raw token | `{ident.get('raw_token')}` |",
        f"| commit SHA | `{ident.get('commit_sha')}` |",
        "",
        "## 对齐率",
        "",
        "| 指标 | 实测 | 基线(plan) | 同量级 |",
        "|---|---|---|---|",
        f"| 文件四语齐全 | {f['aligned_four_way']}/{f['zh_total']} = {f['file_alignment_rate']:.2%} "
        f"| {f['baseline_file_alignment_rate']:.2%} | {'✅' if f['same_order_file'] else '❌'} |",
        f"| 字段路径对齐 | {d['common_total']}/{d['union_total']} = {d['field_alignment_rate']:.4%} "
        f"| {d['baseline_field_alignment_rate']:.4%} | {'✅' if d['same_order_field'] else '❌'} |",
        f"| 缺对照文件 | {f['missing_counterpart']} | — | — |",
        f"| 字段路径不齐文件 | {f['path_mismatch_files']} | — | — |",
        "",
        "## 覆盖口径（三类计数互斥，均不等于「待补译」）",
        "",
        "| 桶 | 数量 | 语义 |",
        "|---|---|---|",
        f"| zh 缺 raw 对照 | {f['missing_counterpart']} | 有官方中文、raw 无同名文件 |",
        f"| raw 无 zh 对照(KR/JP/EN) | "
        f"{report['coverage_semantics']['buckets']['raw_without_zh_counterpart']['by_lang']} "
        f"| 有 raw、无官方中文资产；**不是待补译** |",
        f"| 四语文件齐但字段路径不齐 | {f['path_mismatch_files']} | 这才是字段缺失 |",
        "",
        f"各语文件数（git ls-tree HEAD）：{report['coverage_semantics']['raw_only_detail']['file_counts']}",
        "",
        "## 单元与标记",
        "",
        f"- 四语对齐单元：**{u['total']:,}**；其中名称字段 {u['name_units']:,}",
        f"- 域分布：{json.dumps(u['by_kind'], ensure_ascii=False)}",
        f"- stale：{s['units_stale_true']:,}/{s['units_with_stale_verdict']:,} "
        f"= {s['stale_rate']:.2%}（影响文件 {s['files_stale_true']}）",
        f"- BOM：{b['total_files_with_any_bom']} 个文件含 BOM；假 diff **{b['fake_diff_files']}**"
        f"（核验源：{b.get('verified_against', 'n/a')}）",
        f"- 占位符单元：{u['flags'].get('has_placeholder', 0):,}；"
        f"未翻译(无CJK且=EN)：{u['flags'].get('untranslated', 0):,}",
        "",
        "## stale 判据",
        "",
        f"- 最新 RAW commit：`{s['context']['raw_head_commit'][:12] or 'n/a'}`",
        f"- 中文侧最近改动 commit 去重：{s['context']['zh_last_commits_distinct']}",
        f"- 规则：{s['context']['definition']}",
        "",
        "## BOM 保真策略",
        "",
        b["policy"],
        "",
        "## 可复现",
        "",
        "```powershell",
        "python -m translateFunc.termdb all",
        "python -m translateFunc.termdb verify   # 重跑比对产物哈希，确认幂等",
        "```",
        "",
    ]
    return "\n".join(lines)


def write_alignment(report: dict, root: Path | None = None) -> tuple[Path, Path]:
    root = root or config.PROJECT_ROOT
    jp = config.alignment_report_path(root)
    mp = jp.with_suffix(".md")
    write_json(jp, report)
    write_text(mp, render_markdown(report))
    return jp, mp


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="四语字段对齐 + stale + BOM 保真")
    ap.add_argument("--root", default="")
    ap.add_argument("--mirror", default="")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    mirror = Path(args.mirror).resolve() if args.mirror else root / config.MIRROR_DIR
    if not (mirror / ".git").exists():
        mirror = root / config.FALLBACK_MIRROR
    snap = None
    try:
        snap = read_json(config.snapshot_manifest_path(root))
    except Exception:
        pass
    report, u, f = build_alignment(mirror, snap)
    jp, mp = write_alignment(report, root)
    print(f"units={report['units']['total']:,} file_rate={report['files']['file_alignment_rate']:.4f} "
          f"field_rate={report['fields']['field_alignment_rate']:.6f} "
          f"stale={report['stale']['stale_rate']} bom_fake_diff={report['bom']['fake_diff_files']}")
    print(f"wrote {jp}\nwrote {mp}\nwrote {u}\nwrote {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
