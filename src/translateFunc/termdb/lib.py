"""共享工具：BOM 保真 IO、flatten、git 只读访问、证据字段构造。

关键约定（对应 plan §2.1 的 BOM 坑）：
  * 源文件只读，绝不写回 —— 因此不会产生“内容没变但整文件 diff”的假变更；
  * 读取统一走 read_text_bom_aware()：有 BOM 用 utf-8-sig 解码，无 BOM 用 utf-8；
  * read_bytes() 保留原始字节用于 blob/sha256 校验与 BOM 保真核对。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .config import (CAP_GROUP, CJK, HANGUL, KANA, KATAKANA, LANGS, LATIN,
                     NAME_FIELDS, NAME_FIELD_CANDIDATES, NAME_KEYS,
                     PLACEHOLDER, STOP_CAP, ZH)  # re-export：兼容 from .lib import CJK 等


# --------------------------------------------------------------------------
# BOM 保真 / 确定性 IO
# --------------------------------------------------------------------------
class TextFile:
    """一次读取，保留 raw bytes / BOM 状态 / 解码后文本 / 多种哈希。"""

    __slots__ = ("path", "raw", "bom", "text", "sha256", "blob_sha1")

    def __init__(self, path: Path, raw: bytes):
        self.path = path
        self.raw = raw
        self.bom = raw.startswith(b"\xef\xbb\xbf")
        payload = raw[3:] if self.bom else raw
        # errors='strict'：解码失败即暴露，避免静默污染对齐结果
        self.text = payload.decode("utf-8")
        self.sha256 = hashlib.sha256(raw).hexdigest()
        h = hashlib.sha1()
        h.update(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw)
        self.blob_sha1 = h.hexdigest()

    @property
    def bytes(self) -> int:
        return len(self.raw)


def read_text_file(path: Path) -> TextFile:
    return TextFile(path, path.read_bytes())


def load_json_file(path: Path) -> tuple[Any, TextFile]:
    """返回 (json 对象, TextFile)。BOM 在 TextFile 中已剥离，不会抛 Unexpected BOM。"""
    tf = read_text_file(path)
    return json.loads(tf.text), tf


def load_json_lenient(text: str) -> Any:
    """给 git show 返回的字符串用：先去 BOM 再 loads（历史文件同样可能带 BOM）。"""
    return json.loads(text.lstrip("\ufeff"))


def write_json(path: Path, data: Any, *, bom: bool = False, sort_keys: bool = True) -> None:
    """确定性写出产物；bom=True 时写 utf-8-sig，否则 utf-8。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8"))


def write_text(path: Path, text: str, *, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))


def read_json(path: Path) -> Any:
    """读取我们自己写的产物（同样容忍 BOM）。"""
    return json.loads(path.read_bytes().decode("utf-8-sig"))


# --------------------------------------------------------------------------
# 结构工具
# --------------------------------------------------------------------------
def flatten(obj: Any, prefix: tuple = ()) -> dict[tuple, str]:
    """仅保留字符串叶子；list 索引用 #i —— 与 _diag_analysis 口径一致。"""
    out: dict[tuple, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, prefix + (str(k),)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, prefix + (f"#{i}",)))
    elif isinstance(obj, str):
        out[prefix] = obj
    return out


def path_key(path: tuple) -> str:
    return ".".join(path)


def flat_strings(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for v in obj.values():
            flat_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            flat_strings(v, out)
    elif isinstance(obj, str):
        out.append(obj)


def index_records(doc: Any, name_fields: tuple[str, ...]) -> dict[tuple, dict]:
    """(结构路径) -> 含 id 的记录。用于 KR/zh 同路径配对。"""
    idx: dict[tuple, dict] = {}

    def walk(o: Any, path: tuple = ()) -> None:
        if isinstance(o, dict):
            if o.get("id") is not None:
                idx[path] = o
            for k, v in o.items():
                walk(v, path + (k,))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + (i,))

    walk(doc)
    return idx


def index_named(doc: Any, name_fields: tuple[str, ...]) -> dict[tuple, tuple[str, str]]:
    """(path, field) -> (id, text)：仅取第一个存在的 name 类字段。"""
    idx: dict[tuple, tuple[str, str]] = {}

    def walk(o: Any, path: tuple = ()) -> None:
        if isinstance(o, dict):
            if o.get("id") is not None:
                f = next((k for k in name_fields if isinstance(o.get(k), str)), None)
                if f:
                    idx[(path, f)] = (str(o["id"]), o[f])
            for k, v in o.items():
                walk(v, path + (k,))
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, path + (i,))

    walk(doc)
    return idx


# --------------------------------------------------------------------------
# git 只读访问
# --------------------------------------------------------------------------
def git(repo: Path, *args: str, check: bool = False) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def git_ok(repo: Path, *args: str) -> bool:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return p.returncode == 0


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def ls_tree(repo: Path, sha: str = "HEAD", pathspec: str | None = None) -> dict[str, dict]:
    """path -> {mode,type,sha}。用 -z 避免路径转义问题。"""
    args = ["ls-tree", "-r", "-z", sha]
    if pathspec:
        args.append(pathspec)
    raw = subprocess.run(["git", "-C", str(repo), *args],
                         capture_output=True).stdout
    out: dict[str, dict] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        head, _, path = rec.partition(b"\t")
        mode, typ, obj = head.decode("ascii").split()
        out[path.decode("utf-8")] = {"mode": mode, "type": typ, "sha": obj}
    return out


def cat_file_batch(repo: Path, shas: list[str]) -> dict[str, bytes]:
    """一次 git cat-file --batch 取回多个 blob 的原始内容。

    必须用 committed blob 而非工作区字节做哈希：Windows 检出会把 LF 变成 CRLF，
    直接读工作区会把整棵仓库误判为 drift（正是 plan §2.1 的“假 diff”）。
    """
    import subprocess as _sp

    unique = list(dict.fromkeys(shas))
    if not unique:
        return {}
    p = _sp.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                  stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE)
    out: dict[str, bytes] = {}
    try:
        for sha in unique:
            p.stdin.write((sha + "\n").encode("ascii"))
            p.stdin.flush()
            header = p.stdout.readline().decode("ascii", "replace").strip()
            parts = header.split()
            if len(parts) < 3 or parts[1] != "blob":
                out[sha] = b""
                continue
            size = int(parts[2])
            data = p.stdout.read(size)
            p.stdout.read(1)  # 分隔符 \n
            out[sha] = data
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=30)
        except Exception:
            p.kill()
    return out


def normalize_newlines(data: bytes) -> bytes:
    """与 _archive_sync_lib._files_equal 同口径：忽略 CRLF/LF 差异。"""
    return data.replace(b"\r\n", b"\n")


def commit_meta(repo: Path, sha: str) -> dict:
    fmt = "%H%n%h%n%ct%n%ci%n%an%n%s"
    parts = git(repo, "show", "-s", f"--format={fmt}", sha).splitlines()
    while len(parts) < 6:
        parts.append("")
    return {
        "sha": parts[0], "short": parts[1],
        "committer_ts": int(parts[2]) if parts[2].strip().isdigit() else 0,
        "committed_at": parts[3], "author": parts[4], "subject": parts[5],
    }


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return git_ok(repo, "merge-base", "--is-ancestor", ancestor, descendant)


# --------------------------------------------------------------------------
# 证据字段
# --------------------------------------------------------------------------
def evidence_bundle(tag: str | None, raw_token: str | None, commit: str,
                    blobs: dict[str, str] | None = None) -> dict:
    ev = {
        "llc_tag": tag,
        "raw_token": raw_token,
        "commit_sha": commit,
    }
    if blobs:
        ev["blob_sha256"] = {lang: blobs[lang] for lang in LANGS if lang in blobs}
    return ev


def zh_rel_paths(repo: Path) -> list[str]:
    """镜像内 LLC_zh-CN 下所有 json 相对路径（posix，稳定排序）。"""
    root = repo / ZH
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.json"))
