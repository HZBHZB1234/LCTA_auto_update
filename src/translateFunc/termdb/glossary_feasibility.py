"""工程化自 _diag_analysis/glossary_feasibility.py：文件/单元/未翻译/去重/占位符统计。

只读；输出 docs/termdb/preflight/glossary_feasibility.json（确定性）。
CLI: python -m tools.termdb.glossary_feasibility [--repo PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from . import config
from .lib import (CJK, HANGUL, KANA, LATIN, PLACEHOLDER, flatten, load_json_file,
                  write_json, zh_rel_paths)
from .snapshot import extract_identity


def run(repo: Path, out: Path | None = None) -> dict:
    stats = Counter()
    kind_units = Counter()
    kind_terms = Counter()
    kr_reuse = Counter()
    quadruples = []
    missing_counterpart = []
    path_mismatch = []

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
            docs[lang] = flatten(doc)
        if not ok:
            missing_counterpart.append(rel)
            continue
        stats["aligned_files"] += 1
        common = set.intersection(*[set(docs[l]) for l in config.LANGS])
        only = set().union(*[set(d) for d in docs.values()]) - common
        if only:
            path_mismatch.append((rel, len(only)))
        kind = config.file_kind(rel)
        for path in sorted(common, key=lambda p: ".".join(str(x) for x in p)):
            vals = {lang: docs[lang][path] for lang in config.LANGS}
            quadruples.append((rel, path, vals))
            kind_units[kind] += 1
            if config.is_name_path(path):
                kind_terms[kind] += 1
            kr_reuse[vals["KR"]] += 1

    total = len(quadruples)
    c = Counter()
    for _, _, v in quadruples:
        kr, jp, en, zh = v["KR"], v["JP"], v["EN"], v[config.ZH]
        if not kr.strip():
            c["kr_empty"] += 1
        if not jp.strip():
            c["jp_empty"] += 1
        if not en.strip():
            c["en_empty"] += 1
        if not zh.strip():
            c["zh_empty"] += 1
        elif not CJK.search(zh):
            c["zh_no_cjk"] += 1
            if zh.strip() == en.strip():
                c["zh_equal_en_untranslated"] += 1
        if HANGUL.search(kr):
            c["kr_has_hangul"] += 1
        if KANA.search(jp):
            c["jp_has_kana"] += 1
        if LATIN.search(en):
            c["en_has_latin"] += 1
        if PLACEHOLDER.search(kr) or PLACEHOLDER.search(zh):
            c["has_placeholder"] += 1

    distinct_kr = len(kr_reuse)
    once = sum(1 for n in kr_reuse.values() if n == 1)
    result = {
        "repo": str(repo.resolve()),
        "source_snapshot": extract_identity(repo),
        "files": {
            "zh_total": len(zh_rel_paths(repo)),
            "aligned_four_way": stats["aligned_files"],
            "missing_counterpart": len(missing_counterpart),
        },
        "units_total": total,
        "units_by_kind": dict(sorted(kind_units.items())),
        "name_path_units_by_kind": dict(sorted(kind_terms.items())),
        "coverage_flags": dict(sorted(c.items())),
        "kr_distinct": distinct_kr,
        "kr_seen_once": once,
        "kr_seen_twice_plus": distinct_kr - once,
        "units_using_repeated_kr": sum(n for n in kr_reuse.values() if n >= 2),
        "chars_kr": sum(len(v["KR"]) for _, _, v in quadruples),
        "chars_en": sum(len(v["EN"]) for _, _, v in quadruples),
        "missing_counterpart_sample": missing_counterpart[:10],
        "path_mismatch_files": len(path_mismatch),
        "path_mismatch_sample": [{"file": r, "only_fields": n} for r, n in path_mismatch[:10]],
        "kr_reuse_top": kr_reuse.most_common(15),
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
    out = Path(args.out) if args.out else config.preflight_dir(root) / "glossary_feasibility.json"
    r = run(repo, out)
    print(json.dumps({k: v for k, v in r.items() if k != "kr_reuse_top"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
