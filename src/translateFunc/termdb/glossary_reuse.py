"""工程化自 _diag_analysis/glossary_reuse.py：积压可复用率（0 次模型调用）。

只读；输出 docs/termdb/preflight/glossary_reuse.json（确定性）。
CLI: python -m translateFunc.termdb.glossary_reuse [--repo PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from . import config
from .lib import (CJK, HANGUL, flatten, index_named, load_json_file,
                  write_json, zh_rel_paths)
from .snapshot import extract_identity


def run(repo: Path, out: Path | None = None) -> dict:
    files = []
    seeds = defaultdict(set)
    for rel in zh_rel_paths(repo):
        docs = {}
        ok = True
        for lang in config.LANGS:
            p = repo / lang / rel
            if not p.is_file():
                ok = False
                break
            try:
                doc, _ = load_json_file(p)
            except Exception:
                ok = False
                break
            docs[lang] = doc
        if not ok:
            continue
        files.append((rel, docs))
        k_idx = index_named(docs["KR"], config.NAME_FIELDS)
        z_idx = index_named(docs[config.ZH], config.NAME_FIELDS)
        for key, (_, kr) in k_idx.items():
            z = z_idx.get(key)
            if not z:
                continue
            kr, zh = kr.strip(), z[1].strip()
            if kr and zh and HANGUL.search(kr) and CJK.search(zh) and kr != zh \
                    and len(kr) <= 40 and len(zh) <= 30:
                seeds[kr].add(zh)
    clean = {k: next(iter(sorted(v))) for k, v in seeds.items() if len(v) == 1}
    long_terms = sorted((k for k in clean if len(HANGUL.findall(k)) >= 2), key=len, reverse=True)

    kr_zh: dict[str, str] = {}
    for _, docs in files:
        fk, fz = flatten(docs["KR"]), flatten(docs[config.ZH])
        for path in sorted(set(fk) & set(fz), key=lambda p: ".".join(str(x) for x in p)):
            kr, zh = fk[path].strip(), fz[path].strip()
            if kr and zh and CJK.search(zh) and HANGUL.search(kr):
                kr_zh.setdefault(kr, zh)

    c = Counter()
    untranslated_distinct_kr = set()
    reusable_by_kind = Counter()
    for rel, docs in files:
        kind = config.file_kind(rel)
        fk, fz, fe = flatten(docs["KR"]), flatten(docs[config.ZH]), flatten(docs["EN"])
        for path in sorted(set(fk) & set(fz) & set(fe),
                           key=lambda p: ".".join(str(x) for x in p)):
            kr, zh, en = fk[path].strip(), fz[path].strip(), fe[path].strip()
            if not kr:
                continue
            c["units"] += 1
            translated = bool(CJK.search(zh)) or (zh and zh != en)
            if translated:
                continue
            c["untranslated"] += 1
            untranslated_distinct_kr.add(kr)
            if kr in kr_zh:
                c["exact_kr_has_cjk_elsewhere"] += 1
                reusable_by_kind[kind] += 1
            if any(t in kr for t in long_terms):
                c["contains_seed_term"] += 1
            if len(kr) <= 24:
                c["short_untranslated"] += 1

    denom = c["untranslated"] or 1
    result = {
        "source_snapshot": extract_identity(repo),
        "units": c["units"],
        "untranslated": c["untranslated"],
        "untranslated_distinct_kr": len(untranslated_distinct_kr),
        "untranslated_kr_reusable_exact": c["exact_kr_has_cjk_elsewhere"],
        "untranslated_reusable_rate": round(c["exact_kr_has_cjk_elsewhere"] / denom, 4),
        "untranslated_contains_seed_term": c["contains_seed_term"],
        "untranslated_contains_seed_rate": round(c["contains_seed_term"] / denom, 4),
        "untranslated_short_le24": c["short_untranslated"],
        "reusable_by_kind": dict(sorted(reusable_by_kind.items())),
        "seed_terms": len(clean),
        "seed_terms_multisyllable": len(long_terms),
    }
    if out:
        write_json(out, result)
    return result


def main() -> int:
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    root = config.PROJECT_ROOT
    repo = Path(args.repo).resolve() if args.repo else config.default_mirror(root)
    out = Path(args.out) if args.out else config.preflight_dir(root) / "glossary_reuse.json"
    print(json.dumps(run(repo, out), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
