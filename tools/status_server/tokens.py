"""从 resources.assets 提取 CDN token,并提供在线校验。"""
from __future__ import annotations

import re
from urllib import request as urllib_request


L_TOKEN_RE = re.compile(
    rb"downloadcommon\.limbuscompanycdn\.org/(l\d{8}_[A-Za-z0-9_-]+)"
)
F_TOKEN_RE = re.compile(
    rb"downloadfmod\.limbuscompanycdn\.org/(f\d{8}_[A-Za-z0-9_-]+)"
)


def extract_tokens(data: bytes) -> tuple[str | None, str | None]:
    l_tokens = {m.group(1).decode("ascii") for m in L_TOKEN_RE.finditer(data)}
    f_tokens = {m.group(1).decode("ascii") for m in F_TOKEN_RE.finditer(data)}
    pick = lambda s: max(s, key=lambda t: t[1:9]) if s else None
    return pick(l_tokens), pick(f_tokens)


def verify_token(token: str) -> str | None:
    url = (
        f"https://downloadcommon.limbuscompanycdn.org/{token}"
        "/Assets/LocalizePatch/LocalizePatchInfo.hash"
    )
    req = urllib_request.Request(
        url,
        headers={
            "User-Agent": "UnityPlayer/6000.3.12f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)"
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            return response.read().decode("utf-8", "replace").strip() or None
    except Exception as exc:
        return f"<verify failed: {exc}>"
