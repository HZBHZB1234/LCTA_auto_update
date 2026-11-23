"""工程化自 _diag_analysis/glossary_history.py：跨 commit 的 KR→zh 稳定性量化。

只读（git show / ls-tree）；输出 docs/termdb/preflight/glossary_history.json。
BOM 改进：对 git show 返回的文本先 lstrip('\\ufeff') 再 json.loads，
避免带 BOM 的历史 blob 被静默跳过（对应 plan §2.1 坑）。
CLI: python -m translateFunc.termdb.glossary_history [--repo PATH] [--per-commit N]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from . import config
from .lib import (CJK, HANGUL, flat_strings, git, index_named, load_json_lenient,
                  write_json)
from .snapshot import extract_identity


def _name_pairs(kr_doc, zh_doc, rel):
    k_idx, z_idx = index_named(kr_doc, config.NAME_FIELDS), index_named(zh_doc, config.NAME_FIELDS)
    out = []
    for key, (rid, kr) in k_idx.items():
        z = z_idx.get(key)
        if not z:
            continue
        zh = z[1]
        kr, zh = kr.strip(), zh.strip()
        if not kr or not zh or not HANGUL.search(kr) or not CJK.search(zh):
            continue
        if kr == zh or len(kr) > 40 or len(zh) > 30:
            continue
        out.append((f"{rel}|{rid}|{key[1]}", kr, zh))
    return out


def _prose_pairs(kr_doc, zh_doc):
    ks, zs = [], []
    flat_strings(kr_doc, ks)
    flat_strings(zh_doc, zs)
    if len(ks) != len(zs):
        return []
    out = []
    for kr, zh in zip(ks, zs):
        kr, zh = kr.strip(), zh.strip()
        if len(kr) < 12 or len(zh) < 4:
            continue
        if not HANGUL.search(kr) or not CJK.search(zh):
            continue
        out.append((kr, zh))
    return out


def run(repo: Path, out: Path | None = None, per_commit: int = 6) -> dict:
    shas = [s for s in git(repo, "rev-list", "HEAD").split() if s]
    shas.reverse()

    name_variants: dict[str, set[str]] = defaultdict(set)
    prose_variants: dict[str, set[str]] = defaultdict(set)
    rev_events = []
    total_changes = 0
    files_seen = Counter()

    for ci, sha in enumerate(shas):
        names = sorted(f for f in git(repo, "ls-tree", "-r", "--name-only", sha, config.ZH).splitlines()
                       if f.endswith(".json") and any(k in f for k in
                                                       ("BattleKeywords", "Skills", "Passives")))
        if not names:
            continue
        n = per_commit
        start = (ci * n) % len(names)
        picked = [names[(start + i) % len(names)] for i in range(min(n, len(names)))]
        for rel_zh in picked:
            rel_kr = rel_zh.replace(config.ZH + "/", "KR/", 1)
            bz = git(repo, "show", f"{sha}:{rel_zh}")
            bk = git(repo, "show", f"{sha}:{rel_kr}")
            if not bz or not bk:
                continue
            try:
                zh_doc, kr_doc = load_json_lenient(bz), load_json_lenient(bk)
            except Exception:
                continue
            files_seen[rel_zh] += 1
            for key, kr, zh in _name_pairs(kr_doc, zh_doc, rel_zh):
                old = name_variants[key]
                if old and zh not in old:
                    total_changes += 1
                    rev_events.append({"sha": sha[:8], "key": key,
                                       "old": sorted(old), "new": zh})
                old.add(zh)
            for kr, zh in _prose_pairs(kr_doc, zh_doc):
                prose_variants[kr[:160]].add(zh)

    multi = {k: v for k, v in name_variants.items() if len(v) > 1}
    prose_total = len(prose_variants)
    prose_multi = sum(1 for v in prose_variants.values() if len(v) > 1)
    result = {
        "source_snapshot": extract_identity(repo),
        "commits_scanned": len(shas),
        "files_sampled_distinct": len(files_seen),
        "name_keys": len(name_variants),
        "name_keys_unstable": len(multi),
        "name_instability_rate": round(len(multi) / len(name_variants), 4) if name_variants else None,
        "name_revision_events": total_changes,
        "name_variant_hist": dict(sorted(Counter(len(v) for v in name_variants.values()).items())),
        "prose_strings": prose_total,
        "prose_strings_multi_rendering": prose_multi,
        "prose_multi_rate": round(prose_multi / prose_total, 4) if prose_total else None,
        "name_unstable_samples": [{"key": k, "variants": sorted(v)}
                                  for k, v in sorted(multi.items())[:20]],
        "name_revision_events_sample": rev_events[:20],
        "top_sampled_files": files_seen.most_common(6),
    }
    if out:
        write_json(out, result)
    return result


def main() -> int:
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--per-commit", type=int, default=6)
    args = ap.parse_args()
    root = config.PROJECT_ROOT
    repo = Path(args.repo).resolve() if args.repo else config.default_mirror(root)
    out = Path(args.out) if args.out else config.preflight_dir(root) / "glossary_history.json"
    r = run(repo, out, args.per_commit)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("name_unstable_samples", "name_revision_events_sample")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
