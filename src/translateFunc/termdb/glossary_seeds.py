"""工程化自 _diag_analysis/glossary_seeds.py：L0 结构名称对 + 冲突隔离 + 正文命中率。

只读；输出 docs/termdb/preflight/glossary_seeds.json（确定性）。
CLI: python -m translateFunc.termdb.glossary_seeds [--repo PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from . import config
from .lib import (CJK, HANGUL, flat_strings, index_records, load_json_file,
                  write_json, zh_rel_paths)
from .snapshot import extract_identity


def _pair_up(zh_doc, kr_doc, rel) -> list[dict]:
    zpath = index_records(zh_doc, config.NAME_FIELD_CANDIDATES)
    kpath = index_records(kr_doc, config.NAME_FIELD_CANDIDATES)
    out = []
    for path, zrec in zpath.items():
        krec = kpath.get(path)
        if krec is None:
            continue
        field = next((k for k in config.NAME_FIELD_CANDIDATES
                      if isinstance(zrec.get(k), str)), None)
        if not field:
            continue
        kr_name, zh_name = krec.get(field), zrec.get(field)
        if not isinstance(kr_name, str) or not isinstance(zh_name, str):
            continue
        kr_name, zh_name = kr_name.strip(), zh_name.strip()
        if not kr_name or not zh_name or not HANGUL.search(kr_name) or not CJK.search(zh_name):
            continue
        if kr_name == zh_name or len(kr_name) > 40 or len(zh_name) > 30:
            continue
        out.append({"file": rel, "id": zrec["id"], "field": field,
                    "kr": kr_name, "zh": zh_name})
    return out


def run(repo: Path, out: Path | None = None) -> dict:
    pairs = []
    for rel in zh_rel_paths(repo):
        krp = repo / "KR" / rel
        if not krp.is_file():
            continue
        try:
            zh_doc, _ = load_json_file(repo / config.ZH / rel)
            kr_doc, _ = load_json_file(krp)
        except Exception:
            continue
        pairs.extend(_pair_up(zh_doc, kr_doc, rel))

    kr2zh = defaultdict(set)
    kr_meta = defaultdict(list)
    for p in pairs:
        kr2zh[p["kr"]].add(p["zh"])
        kr_meta[p["kr"]].append(p)
    conflicts = {k: v for k, v in kr2zh.items() if len(v) > 1}
    clean = {k: next(iter(sorted(v))) for k, v in kr2zh.items() if len(v) == 1}

    files_hit = units_hit = units_total = 0
    hit_counter = Counter()
    terms_by_len = sorted(clean, key=len, reverse=True)
    for rel in zh_rel_paths(repo):
        krp = repo / "KR" / rel
        if not krp.is_file():
            continue
        try:
            kr_doc, _ = load_json_file(krp)
        except Exception:
            continue
        strs: list[str] = []
        flat_strings(kr_doc, strs)
        units_total += len(strs)
        local_hit = 0
        for s in strs:
            hit = [t for t in terms_by_len if t in s]
            if hit:
                local_hit += 1
                for t in hit:
                    hit_counter[t] += 1
        units_hit += local_hit
        if local_hit:
            files_hit += 1

    result = {
        "source_snapshot": extract_identity(repo),
        "entity_name_pairs": len(pairs),
        "distinct_kr_names": len(kr2zh),
        "clean_mapping": len(clean),
        "conflict_kr_names": len(conflicts),
        "conflict_rate": round(len(conflicts) / len(kr2zh), 4) if kr2zh else None,
        "conflict_sample": [
            {"kr": k, "zh_options": sorted(v)[:6],
             "id_sample": [m["id"] for m in kr_meta[k][:6]]}
            for k, v in sorted(conflicts.items())[:20]
        ],
        "common_clean_sample": [
            {"kr": k, "zh": clean[k]}
            for k, _ in Counter({t: hit_counter.get(t, 0) for t in clean}).most_common(20)
        ],
        "corpus_units_total": units_total,
        "corpus_units_with_seed_hit": units_hit,
        "corpus_unit_hit_rate": round(units_hit / units_total, 4) if units_total else None,
        "files_with_hit": files_hit,
        "top_hit_terms": hit_counter.most_common(25),
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
    out = Path(args.out) if args.out else config.preflight_dir(root) / "glossary_seeds.json"
    r = run(repo, out)
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("conflict_sample", "top_hit_terms", "common_clean_sample")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
