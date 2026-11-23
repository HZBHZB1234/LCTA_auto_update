"""P0 前置步骤编排：运行五个工程化 glossary_* 脚本 + 导出带证据的名称对池。

产物（docs/termdb/）：
  preflight/glossary_feasibility.json
  preflight/glossary_seeds.json
  preflight/glossary_history.json
  preflight/glossary_unknown.json
  preflight/glossary_reuse.json
  name_pairs_with_evidence.jsonl    ← L0 名称对 + (tag, commit, raw_token, 日期, blob, stale)

CLI: python -m tools.termdb preflight [--repo PATH] [--skip-history]
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, glossary_feasibility, glossary_history, glossary_reuse
from . import glossary_seeds, glossary_unknown
from .lib import CJK, HANGUL, load_json_file, read_json, write_text, zh_rel_paths
from .snapshot import extract_identity


def export_name_pairs(repo: Path, root: Path | None = None) -> Path:
    """导出供 held-out 选取的名称对候选池（每行 = 一条 L0 证据）。"""
    root = root or config.PROJECT_ROOT
    ident = extract_identity(repo)
    files_ev: dict[str, dict] = {}
    aligned = config.derived_dir(repo) / "aligned_files.json"
    if aligned.is_file():
        try:
            files_ev = {f["file"]: f for f in read_json(aligned)["files"]}
        except Exception:
            files_ev = {}

    out = config.artifact_dir(root) / "name_pairs_with_evidence.jsonl"
    n = 0
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for rel in zh_rel_paths(repo):
            krp = repo / "KR" / rel
            if not krp.is_file():
                continue
            try:
                zh_doc, _ = load_json_file(repo / config.ZH / rel)
                kr_doc, _ = load_json_file(krp)
            except Exception:
                continue
            zrecs: dict[tuple, dict] = {}

            def walk(obj, path=()):  # noqa: ANN001 - 内部递归收集 zh 侧含 id 记录
                if isinstance(obj, dict):
                    if obj.get("id") is not None:
                        zrecs[path] = obj
                    for k, v in obj.items():
                        walk(v, path + (k,))
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        walk(v, path + (i,))

            walk(zh_doc)
            krecs: dict[tuple, dict] = {}

            def walk_kr(obj, path=()):
                if isinstance(obj, dict):
                    if obj.get("id") is not None:
                        krecs[path] = obj
                    for k, v in obj.items():
                        walk_kr(v, path + (k,))
                elif isinstance(obj, list):
                    for i, v in enumerate(obj):
                        walk_kr(v, path + (i,))

            walk_kr(kr_doc)
            fe = files_ev.get(rel, {})
            blobs = fe.get("blob_sha256", {})
            evidence = {
                "llc_tag": ident.get("llc_tag"),
                "commit_sha": ident.get("commit_sha"),
                "committed_at": ident.get("committed_at"),
                "raw_token": ident.get("raw_token"),
                "raw_created_at": ident.get("raw_created_at"),
                "synced_at": ident.get("translation_synced_at"),
                "blob_sha256": {k: blobs[k] for k in ("KR", config.ZH) if k in blobs},
                "stale": fe.get("stale"),
                "zh_last_commit": fe.get("zh_last_commit"),
            }
            for path, zrec in zrecs.items():
                krec = krecs.get(path)
                if krec is None:
                    continue
                field = next((k for k in config.NAME_FIELD_CANDIDATES
                              if isinstance(zrec.get(k), str)), None)
                if not field:
                    continue
                kr, zh = krec.get(field), zrec.get(field)
                if not isinstance(kr, str) or not isinstance(zh, str):
                    continue
                kr, zh = kr.strip(), zh.strip()
                if not (kr and zh and HANGUL.search(kr) and CJK.search(zh)):
                    continue
                if kr == zh or len(kr) > 40 or len(zh) > 30:
                    continue
                rec = {
                    "kr": kr, "zh": zh, "field": field,
                    "entity_id": zrec["id"],
                    "file": rel,
                    "path": ".".join(str(x) for x in path),
                    "kind": config.file_kind(rel),
                    **evidence,
                }
                fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1
    return out


def run(repo: Path, root: Path | None = None, *, skip_history: bool = False) -> dict:
    root = root or config.PROJECT_ROOT
    outdir = config.preflight_dir(root)
    outdir.mkdir(parents=True, exist_ok=True)
    results = {}
    jobs = [
        ("feasibility", glossary_feasibility.run, "glossary_feasibility.json"),
        ("seeds", glossary_seeds.run, "glossary_seeds.json"),
        ("history", glossary_history.run, "glossary_history.json"),
        ("unknown", glossary_unknown.run, "glossary_unknown.json"),
        ("reuse", glossary_reuse.run, "glossary_reuse.json"),
    ]
    for name, fn, filename in jobs:
        if name == "history" and skip_history:
            results[name] = {"skipped": True}
            continue
        results[name] = fn(repo, outdir / filename)
    pairs = export_name_pairs(repo, root)
    results["name_pairs_file"] = str(pairs)
    results["name_pairs_count"] = sum(1 for _ in pairs.open(encoding="utf-8"))
    return results


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="运行 glossary_* 前置步骤")
    ap.add_argument("--root", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--skip-history", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    repo = Path(args.repo).resolve() if args.repo else config.default_mirror(root)
    r = run(repo, root, skip_history=args.skip_history)
    print(json.dumps({
        "units": r["feasibility"].get("units_total"),
        "name_pairs": r["seeds"].get("entity_name_pairs"),
        "clean": r["seeds"].get("clean_mapping"),
        "conflicts": r["seeds"].get("conflict_kr_names"),
        "name_instability": r["history"].get("name_instability_rate"),
        "prose_multi": r["history"].get("prose_multi_rate"),
        "unknown_signal": r["unknown"].get("untranslated_with_proper_noun_signal"),
        "reusable_rate": r["reuse"].get("untranslated_reusable_rate"),
        "name_pairs_exported": r["name_pairs_count"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
