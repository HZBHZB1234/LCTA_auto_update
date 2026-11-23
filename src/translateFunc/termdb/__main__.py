"""tools.termdb 统一 CLI：python -m tools.termdb <subcommand>."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import align, config, preflight, refresh, snapshot, terms_build
from .lib import read_json, write_json
from .terms_store import DB_NAME, TermStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(root: Path) -> Path:
    return config.default_mirror(root)


def cmd_refresh(root: Path, args) -> dict:
    info = refresh.refresh(root, offline=getattr(args, "offline", False),
                           force=getattr(args, "force", False))
    try:
        info["mirror"] = Path(info["mirror"]).relative_to(root).as_posix()
    except (ValueError, KeyError):
        pass
    write_json(config.artifact_dir(root) / "refresh_result.json", info)
    return info


def cmd_snapshot(root: Path, args) -> dict:
    mirror = Path(getattr(args, "mirror", "")).resolve() if getattr(args, "mirror", "") else _resolve(root)
    rp = config.artifact_dir(root) / "refresh_result.json"
    info = read_json(rp) if rp.is_file() else None
    m = snapshot.build_snapshot(mirror, info)
    snapshot.write_snapshot(m, root)
    return {k: m[k] for k in ("identity", "counts")}


def cmd_align(root: Path, args) -> dict:
    mirror = Path(getattr(args, "mirror", "")).resolve() if getattr(args, "mirror", "") else _resolve(root)
    try:
        snap = snapshot.load_snapshot(root)
    except Exception:
        snap = None
    report, u, f = align.build_alignment(mirror, snap)
    align.write_alignment(report, root)
    return {
        "units": report["units"]["total"],
        "file_rate": report["files"]["file_alignment_rate"],
        "field_rate": report["fields"]["field_alignment_rate"],
        "stale_rate": report["stale"]["stale_rate"],
        "bom_fake_diff": report["bom"]["fake_diff_files"],
    }


def cmd_preflight(root: Path, args) -> dict:
    mirror = Path(getattr(args, "mirror", "")).resolve() if getattr(args, "mirror", "") else _resolve(root)
    r = preflight.run(mirror, root, skip_history=args.skip_history)
    return {
        "name_pairs": r["seeds"].get("entity_name_pairs"),
        "clean": r["seeds"].get("clean_mapping"),
        "conflicts": r["seeds"].get("conflict_kr_names"),
        "exported": r["name_pairs_count"],
        "history_skipped": r["history"].get("skipped", False),
    }


def cmd_verify(root: Path, args) -> dict:
    """重跑 snapshot+align 到临时产物，逐字节比对，证明幂等。"""
    mirror = Path(getattr(args, "mirror", "")).resolve() if getattr(args, "mirror", "") else _resolve(root)
    rp = config.artifact_dir(root) / "refresh_result.json"
    info = read_json(rp) if rp.is_file() else None
    snap = snapshot.build_snapshot(mirror, info)
    report, units, files = align.build_alignment(mirror, snap)
    targets = [
        (config.snapshot_manifest_path(root), json.dumps(snap, ensure_ascii=False,
                                                        indent=2, sort_keys=True) + "\n"),
        (config.alignment_report_path(root), json.dumps(report, ensure_ascii=False,
                                                       indent=2, sort_keys=True) + "\n"),
        (config.alignment_report_path(root).with_suffix(".md"), align.render_markdown(report)),
    ]
    checks = []
    for path, expected in targets:
        actual = path.read_text(encoding="utf-8") if path.is_file() else ""
        checks.append({"artifact": path.name, "identical": actual == expected,
                       "sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest()})
    return {
        "idempotent": all(c["identical"] for c in checks),
        "checks": checks,
        "bom_fake_diff": report["bom"]["fake_diff_files"],
        "file_rate_same_order": report["files"]["same_order_file"],
        "field_rate_same_order": report["fields"]["same_order_field"],
    }


def _term_db(root: Path) -> Path:
    return config.artifact_dir(root) / DB_NAME


def cmd_terms_build(root: Path, args) -> dict:
    mirror = Path(args.mirror).resolve() if getattr(args, "mirror", "") else None
    out = terms_build.build_all(
        root, mirror=mirror,
        l1=not getattr(args, "skip_l1", False),
        l2=not getattr(args, "skip_l2", False),
    )
    return {"db": out["db"], "flat_count": out["flat_count"],
            "counts": out["counts"], "l0": out["l0"]}


def cmd_terms_landing(root: Path, args) -> dict:
    """落位回流：读取 JSON 数组 [{term_key, landing, note?}]，自动状态转移并重导出。"""
    src = Path(args.file)
    records = json.loads(src.read_text(encoding="utf-8-sig"))
    if isinstance(records, dict):
        records = records.get("landings", [])
    results = []
    with TermStore(_term_db(root)) as store:
        for rec in records:
            results.append(terms_build.apply_landing(
                store, rec.get("term_key", ""), rec.get("landing", ""),
                rec.get("note", ""),
            ))
        terms_build.export_revision_jsonl(store, root)
        terms_build.export_compat(store, root)
        terms_build.write_terms_report(store, root, {"last_landing_batch": len(records)})
    return {"applied": len(results), "changed": sum(1 for r in results if r.get("changed")),
            "results": results[:50]}


def cmd_terms_advance(root: Path, args) -> dict:
    with TermStore(_term_db(root)) as store:
        stats = terms_build.advance_versions(store, int(args.gap), root)
        terms_build.export_revision_jsonl(store, root)
        terms_build.export_compat(store, root)
        terms_build.write_terms_report(store, root, {"last_version_gap": int(args.gap)})
    return {"gap": int(args.gap), "changed": stats}


def cmd_terms_verify(root: Path, args) -> dict:
    with TermStore(_term_db(root)) as store:
        return terms_build.verify_export(store, root)


def cmd_terms_import_learned(root: Path, args) -> dict:
    with TermStore(_term_db(root)) as store:
        n = terms_build.import_l3_learned(store, Path(args.path))
        terms_build.export_revision_jsonl(store, root)
    return {"imported": n}


def build_parser():
    import argparse
    ap = argparse.ArgumentParser(prog="python -m tools.termdb",
                                 description="官方汉化语料固化 / 对齐 / 前置步骤")
    ap.add_argument("--root", default="", help="workspace 根目录（默认自动探测）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("refresh", help="刷新归档镜像")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--force", action="store_true")

    sub.add_parser("snapshot", help="固化快照证据链")
    pa = sub.add_parser("align", help="四语对齐 + stale + BOM")
    pp = sub.add_parser("preflight", help="运行 glossary_* 前置步骤")
    pv = sub.add_parser("verify", help="重跑比对，验证幂等")
    for p_ in (pa, pp, pv):
        p_.add_argument("--mirror", default="")
    pp.add_argument("--skip-history", action="store_true")

    p = sub.add_parser("all", help="refresh → snapshot → align → preflight → verify")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--skip-history", action="store_true")

    pt = sub.add_parser("terms-build", help="L0/L1/L2 构建 term-v1 + 兼容导出")
    pt.add_argument("--mirror", default="")
    pt.add_argument("--skip-l1", action="store_true")
    pt.add_argument("--skip-l2", action="store_true")
    pta = sub.add_parser("terms-all", help="preflight → terms-build（完整自维护链路）")
    pta.add_argument("--mirror", default="")
    pta.add_argument("--skip-l1", action="store_true")
    pta.add_argument("--skip-l2", action="store_true")
    pta.add_argument("--skip-history", action="store_true")
    pl = sub.add_parser("terms-landing", help="落位回流（自动状态转移）")
    pl.add_argument("file", help="JSON 文件：[{term_key, landing, note?}]")
    pv2 = sub.add_parser("terms-advance", help="版本衰减")
    pv2.add_argument("gap", type=int, help="版本距离 Δversions")
    pi = sub.add_parser("terms-import-learned", help="导入 proper_learned.json 到 L3 候选池")
    pi.add_argument("path")
    sub.add_parser("terms-verify", help="验收自检：兼容契约 + 运行时回读")
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    config.artifact_dir(root).mkdir(parents=True, exist_ok=True)
    handlers = {
        "refresh": cmd_refresh, "snapshot": cmd_snapshot, "align": cmd_align,
        "preflight": cmd_preflight, "verify": cmd_verify,
        "terms-build": cmd_terms_build, "terms-landing": cmd_terms_landing,
        "terms-advance": cmd_terms_advance,
        "terms-verify": cmd_terms_verify,
        "terms-import-learned": cmd_terms_import_learned,
    }
    if args.cmd == "all":
        out = {"refresh": cmd_refresh(root, args), "snapshot": cmd_snapshot(root, args),
               "align": cmd_align(root, args), "preflight": cmd_preflight(root, args),
               "verify": cmd_verify(root, args)}
    elif args.cmd == "terms-all":
        out = {"preflight": cmd_preflight(root, args), "terms": cmd_terms_build(root, args)}
    else:
        out = handlers[args.cmd](root, args)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
