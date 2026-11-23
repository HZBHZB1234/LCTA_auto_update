"""快照固化：按 (LLC tag, raw token, commit SHA, blob sha256) 建立证据链。

产物：docs/termdb/snapshot_manifest.json（确定性、可重跑无 diff）

per-file 记录里同时保留：
  * git_blob_sha1 —— git 对象 id（来自 ls-tree，权威内容身份）
  * sha256        —— committed blob 原始字节 sha256（含 BOM），跨 OS 稳定
  * bom           —— 是否 UTF-8 BOM（从 committed blob 判定）
  * drift         —— 工作区字节按 CRLF→LF 归一后是否偏离 committed blob（应为 false）

corpus_sha256 = 所有纳入文件的 (path, sha256) 稳定拼接哈希，作为整包指纹。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import config
from .lib import (cat_file_batch, commit_meta, git, head_sha, ls_tree,
                  normalize_newlines, read_json, write_json)


MANIFEST_SCHEMA = "termdb-snapshot/1"


def _safe_json(mirror: Path, rel: str):
    p = mirror / rel
    if not p.is_file():
        return None, None
    try:
        from .lib import load_json_file
        return load_json_file(p)
    except Exception:
        return None, None


def extract_identity(mirror: Path) -> dict:
    """从镜像状态文件提取 LLC 版本与 raw token。"""
    tstate, _ = _safe_json(mirror, config.STATE_TRANSLATION)
    rstate, _ = _safe_json(mirror, config.STATE_RAW)
    version, _ = _safe_json(mirror, config.VERSION_FILE)

    tag = None
    if isinstance(tstate, dict) and tstate.get("tag"):
        tag = str(tstate["tag"])
    elif isinstance(version, dict) and version.get("version") is not None:
        tag = str(version["version"])

    return {
        "llc_tag": tag,
        "llc_version": version.get("version") if isinstance(version, dict) else None,
        "raw_token": rstate.get("token") if isinstance(rstate, dict) else None,
        "raw_created_at": rstate.get("created_at") if isinstance(rstate, dict) else None,
        "raw_synced_at": rstate.get("synced_at") if isinstance(rstate, dict) else None,
        "translation_asset": tstate.get("asset") if isinstance(tstate, dict) else None,
        "translation_synced_at": tstate.get("synced_at") if isinstance(tstate, dict) else None,
        "state_files_present": {
            "translation_state": isinstance(tstate, dict),
            "raw_state": isinstance(rstate, dict),
            "version": isinstance(version, dict),
        },
    }


def _in_scope(path: str) -> bool:
    first = path.split("/", 1)[0]
    if first in config.LANGS and path.lower().endswith(".json"):
        return True
    return path in (config.STATE_TRANSLATION, config.STATE_RAW, config.VERSION_FILE)


def build_snapshot(mirror: Path, refresh_info: dict | None = None) -> dict:
    if not mirror.is_dir():
        raise FileNotFoundError(f"镜像不存在: {mirror}")
    sha = head_sha(mirror)
    meta = commit_meta(mirror, sha)
    tree = git(mirror, "rev-parse", f"{sha}^{{tree}}").strip()
    tree_map = ls_tree(mirror, sha)

    scoped = [p for p in sorted(tree_map) if _in_scope(p)]
    blobs = cat_file_batch(mirror, [tree_map[p]["sha"] for p in scoped])

    files = []
    for path in scoped:
        expected = tree_map[path]["sha"]
        raw = blobs.get(expected)
        if raw is None:
            continue
        sha256 = hashlib.sha256(raw).hexdigest()
        bom = raw.startswith(b"\xef\xbb\xbf")
        full = mirror / path
        # 真实漂移判定：工作区字节按 CRLF→LF 归一后必须等于 committed blob。
        # 只看原始字节会把 Windows autocrlf 的换行转换误判为整库假 diff。
        drift = True
        if full.is_file():
            work = full.read_bytes()
            drift = normalize_newlines(work) != normalize_newlines(raw)
        files.append({
            "path": path,
            "git_blob_sha1": expected,
            "sha256": sha256,       #  committed blob 原始字节（含 BOM），跨 OS 稳定
            "bytes": len(raw),
            "bom": bom,
            "drift": drift,
        })

    fingerprint_input = "".join(f"{f['path']}\0{f['sha256']}\0" for f in files)
    corpus_sha256 = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()

    identity = extract_identity(mirror)
    identity.update({
        "commit_sha": sha,
        "commit_short": meta.get("short"),
        "commit_subject": meta.get("subject"),
        "committed_at": meta.get("committed_at"),
        "tree_sha": tree,
        "corpus_sha256": corpus_sha256,
        "source_repo": config.ARCHIVE_REPO
                       if (refresh_info or {}).get("mirror_name") == config.MIRROR_DIR
                       else config.FALLBACK_MIRROR,
    })

    by_lang: dict[str, int] = {}
    for f in files:
        lang = f["path"].split("/", 1)[0]
        by_lang[lang] = by_lang.get(lang, 0) + 1

    return {
        "schema": MANIFEST_SCHEMA,
        "identity": identity,
        "refresh": refresh_info or {},
        "counts": {
            "files_total": len(files),
            "files_by_lang": dict(sorted(by_lang.items())),
            "bom_files": sum(1 for f in files if f["bom"]),
            "drift_files": sum(1 for f in files if f["drift"]),
        },
        "files": files,
    }


def write_snapshot(manifest: dict, root: Path | None = None) -> Path:
    root = root or config.PROJECT_ROOT
    out = config.snapshot_manifest_path(root)
    write_json(out, manifest)
    return out


def load_snapshot(root: Path | None = None) -> dict:
    return read_json(config.snapshot_manifest_path(root))


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="固化语料快照证据链")
    ap.add_argument("--root", default="")
    ap.add_argument("--mirror", default="")
    ap.add_argument("--refresh-json", default="")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    mirror = Path(args.mirror).resolve() if args.mirror else root / config.MIRROR_DIR
    if not (mirror / ".git").exists():
        mirror = root / config.FALLBACK_MIRROR
    refresh_info = None
    if args.refresh_json:
        refresh_info = read_json(Path(args.refresh_json))
    m = build_snapshot(mirror, refresh_info)
    out = write_snapshot(m, root)
    print(f"wrote {out}")
    print(json.dumps({k: m[k] for k in ("identity", "counts")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
