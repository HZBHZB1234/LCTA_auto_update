"""工程化自 _diag_analysis/glossary_unknown.py：未翻译专名信号与种子覆盖率。

只读；输出 docs/termdb/preflight/glossary_unknown.json（确定性）。
CLI: python -m translateFunc.termdb.glossary_unknown [--repo PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import config
from .lib import (CJK, HANGUL, KATAKANA, flatten, index_named, load_json_file,
                  write_json, zh_rel_paths)
from .snapshot import extract_identity


def _syll_count(s: str) -> int:
    return len(HANGUL.findall(s))


def run(repo: Path, out: Path | None = None) -> dict:
    seeds = defaultdict(set)
    for rel in zh_rel_paths(repo):
        krp = repo / "KR" / rel
        if not krp.is_file():
            continue
        try:
            zh_doc, _ = load_json_file(repo / config.ZH / rel)
            kr_doc, _ = load_json_file(krp)
        except Exception:
            continue
        k_idx, z_idx = index_named(kr_doc, config.NAME_FIELDS), index_named(zh_doc, config.NAME_FIELDS)
        for key, (_, kr) in k_idx.items():
            z = z_idx.get(key)
            if not z:
                continue
            kr, zh = kr.strip(), z[1].strip()
            if (kr and zh and HANGUL.search(kr) and CJK.search(zh) and kr != zh
                    and len(kr) <= 40 and len(zh) <= 30):
                seeds[kr].add(zh)

    clean = {k: next(iter(sorted(v))) for k, v in seeds.items() if len(v) == 1}
    len_hist = Counter(_syll_count(k) for k in clean)
    long_terms = sorted((k for k in clean if _syll_count(k) >= 2), key=len, reverse=True)

    tot = zh_untranslated = 0
    coverage = Counter()
    hit_by_len = Counter()
    cap_surfaces = Counter()
    kana_surfaces = Counter()
    unknown_kr = Counter()
    untranslated_by_dir = Counter()
    untranslated_units = units_with_signal = 0

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
            continue
        common = set.intersection(*[set(docs[l]) for l in config.LANGS])
        for path in sorted(common, key=lambda p: ".".join(str(x) for x in p)):
            kr, jp, en, zh = (docs["KR"][path], docs["JP"][path],
                              docs["EN"][path], docs[config.ZH][path])
            if not kr.strip():
                continue
            tot += 1
            translated = bool(CJK.search(zh)) or (zh.strip() and zh.strip() != en.strip())
            if not translated:
                zh_untranslated += 1
                untranslated_by_dir[rel.split("/")[0] if "/" in rel else "."] += 1
            local_hits = [t for t in long_terms if t in kr]
            if local_hits:
                coverage["units_with_seed"] += 1
                for t in local_hits[:5]:
                    hit_by_len[min(_syll_count(t), 6)] += 1
            if not translated:
                untranslated_units += 1
                signal = False
                for m in config.CAP_GROUP.finditer(en):
                    s = m.group(0).strip()
                    if s and s not in config.STOP_CAP and not s[0].islower():
                        cap_surfaces[s] += 1
                        signal = True
                for m in KATAKANA.finditer(jp):
                    kana_surfaces[m.group(0)] += 1
                    signal = True
                for tok in re.findall(r"[\uac00-\ud7a3]{3,}", kr):
                    if tok not in long_terms and tok not in clean:
                        unknown_kr[tok] += 1
                if signal:
                    units_with_signal += 1

    result = {
        "source_snapshot": extract_identity(repo),
        "seed_names_total": len(seeds),
        "seed_names_clean": len(clean),
        "seed_syllable_hist": dict(sorted(len_hist.items())),
        "seed_single_syllable": len_hist.get(1, 0),
        "units_total": tot,
        "units_seed_covered": coverage.get("units_with_seed", 0),
        "units_seed_covered_rate": round(coverage.get("units_with_seed", 0) / tot, 4) if tot else None,
        "units_untranslated": zh_untranslated,
        "untranslated_share": round(zh_untranslated / tot, 4) if tot else None,
        "untranslated_with_proper_noun_signal": units_with_signal,
        "untranslated_signal_share": round(units_with_signal / untranslated_units, 4) if untranslated_units else None,
        "untranslated_by_dir": untranslated_by_dir.most_common(12),
        "distinct_capitalized_en_surfaces": len(cap_surfaces),
        "distinct_katakana_surfaces": len(kana_surfaces),
        "distinct_unknown_kr_tokens": len(unknown_kr),
        "top_cap_surfaces": cap_surfaces.most_common(20),
        "top_kana_surfaces": kana_surfaces.most_common(20),
        "top_unknown_kr": unknown_kr.most_common(20),
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
    out = Path(args.out) if args.out else config.preflight_dir(root) / "glossary_unknown.json"
    r = run(repo, out)
    print(json.dumps({k: v for k, v in r.items()
                      if not k.startswith("top_")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
