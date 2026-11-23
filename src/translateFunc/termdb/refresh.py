"""镜像刷新：以 HZBHZB1234/LimbusLocalizeAchieve 为准；失败沿用既有快照并标注日期。

设计要点
--------
* 镜像目录：workspace/LimbusLocalizeAchieve（gitignore 覆盖）。
* 幂等：本地 HEAD 与远端 HEAD 一致时不做任何写操作；重跑无 diff。
* 降级：clone/fetch 任一失败 → 使用既有 LocalizeLimbusCompany 快照，
  status='fallback'，note 明确写出快照 commit 日期（plan §2.3 风险缓解）。
* 纯只读消费：刷新完成后不改写任何语料文件（BOM/字节完全来自上游）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import config
from .lib import commit_meta, git, git_ok, head_sha


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=900)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def _remote_head(mirror: Path) -> str | None:
    ok, out = _run(["git", "-C", str(mirror), "ls-remote", "origin", "HEAD"])
    if not ok or not out:
        return None
    return out.split()[0] if out.split() else None


def _describe(repo: Path, status: str, note: str = "", error: str = "") -> dict:
    sha = head_sha(repo) if git_ok(repo, "rev-parse", "HEAD") else ""
    meta = commit_meta(repo, sha) if sha else {}
    return {
        "status": status,
        "mirror": str(repo),
        "mirror_name": repo.name,
        "source_repo": config.ARCHIVE_REPO if repo.name == config.MIRROR_DIR else config.FALLBACK_MIRROR,
        "head_sha": sha,
        "head_short": meta.get("short", ""),
        "committed_at": meta.get("committed_at", ""),
        "commit_subject": meta.get("subject", ""),
        "note": note,
        "error": error,
    }


def refresh(root: Path | None = None, *, offline: bool = False,
            force: bool = False, prefer_proxy: bool = True) -> dict:
    """刷新镜像并返回可固化的刷新结果描述。"""
    root = root or config.PROJECT_ROOT
    mirror = root / config.MIRROR_DIR
    fallback = root / config.FALLBACK_MIRROR

    # 既有镜像：尝试 fetch；不联网或失败则原样沿用
    if mirror.is_dir() and git_ok(mirror, "rev-parse", "HEAD"):
        if offline:
            return _describe(mirror, "unchanged", "offline=True：沿用本地镜像 HEAD")
        remote = _remote_head(mirror)
        if remote is None:
            return _describe(mirror, "fallback",
                             "远端不可达；沿用本地镜像 HEAD（见 committed_at）",
                             "ls-remote origin HEAD failed")
        local = head_sha(mirror)
        if remote == local:
            return _describe(mirror, "unchanged", "本地 HEAD 已等于远端 HEAD")
        ok, out = _run(["git", "-C", str(mirror), "fetch", "origin", "--quiet"])
        if not ok:
            return _describe(mirror, "fallback",
                             "fetch 失败；沿用本地镜像 HEAD（见 committed_at）", out[:500])
        ok, out = _run(["git", "-C", str(mirror), "reset", "--hard", "--quiet", remote])
        if not ok:
            return _describe(mirror, "fallback",
                             "reset 失败；沿用本地镜像 HEAD（见 committed_at）", out[:500])
        return _describe(mirror, "refreshed", f"HEAD {local[:8]} → {remote[:8]}")

    # 无镜像：尝试 clone（代理优先，直连兜底）
    if not offline and not force:
        urls = ([config.ARCHIVE_URL_PROXY, config.ARCHIVE_URL_DIRECT] if prefer_proxy
                else [config.ARCHIVE_URL_DIRECT, config.ARCHIVE_URL_PROXY])
        errors = []
        for url in urls:
            ok, out = _run(["git", "clone", "--quiet", url, str(mirror)])
            if ok and git_ok(mirror, "rev-parse", "HEAD"):
                return _describe(mirror, "refreshed", f"新建镜像 clone from {url}")
            errors.append(f"{url}: {out[:200]}")
            if mirror.exists():
                subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(mirror)],
                               capture_output=True)
        return _describe(fallback, "fallback",
                         "clone 失败；沿用既有快照（见 committed_at 日期）",
                         " | ".join(errors))

    # offline 且无镜像 → 只能用既有快照
    if fallback.is_dir() and git_ok(fallback, "rev-parse", "HEAD"):
        return _describe(fallback, "fallback",
                         "offline=True 且无归档镜像；沿用既有快照（见 committed_at 日期）")
    raise RuntimeError("无可用语料快照：归档 clone 失败且既有 LocalizeLimbusCompany 不存在")


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="刷新官方汉化归档镜像")
    ap.add_argument("--root", default="")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    root = Path(args.root).resolve() if args.root else config.PROJECT_ROOT
    print(json.dumps(refresh(root, offline=args.offline, force=args.force),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
