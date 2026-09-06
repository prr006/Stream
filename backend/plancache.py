"""
plancache — disk cache of PlaybackDecision results.

A plan is a pure function of (file version, client class, language), so it is
cached once computed and reused for every matching Play click — including
across server restarts. See docs/ARCHITECTURE.md §6.2.

Key: (file_id, file_version, client_class, preferred_lang).
  * file_version  — Drive md5Checksum/modifiedTime ("" until the probe has it).
  * client_class  — coarse UA bucket from the measured capability report
                    (e.g. "chrome-126"); the server-defaults plan is cached
                    under "server-default".

TTL: 24 h. Entries are plain JSON; a corrupt/missing entry is a cache miss,
never an error.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

PLAN_TTL_S = 24 * 3600

_key_re = re.compile(r"[^A-Za-z0-9_.-]")


def client_class(caps: dict | None) -> str:
    name = str((caps or {}).get("name") or "server-default")
    # "chrome-126" → "chrome-126" (major version is the meaningful unit);
    # anything exotic is truncated so the cache dir stays tidy.
    return _key_re.sub("_", name)[:40] or "server-default"


def key(file_id: str, file_version: str, caps: dict | None, lang: str) -> str:
    parts = (file_id, file_version or "unversioned", client_class(caps),
             lang or "und")
    return "_".join(_key_re.sub("_", str(p))[:80] for p in parts) + ".json"


def _path(cache_dir: Path, k: str) -> Path:
    return Path(cache_dir) / "plans" / k


def load(cache_dir: Path, k: str) -> dict | None:
    p = _path(cache_dir, k)
    try:
        if time.time() - p.stat().st_mtime > PLAN_TTL_S:
            return None
        data = json.loads(p.read_text())
        if not isinstance(data, dict) or "mode" not in data:
            return None
        return data
    except (OSError, ValueError):
        return None


def store(cache_dir: Path, k: str, plan: dict) -> None:
    p = _path(cache_dir, k)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan))
        tmp.replace(p)  # atomic publish
    except OSError:
        pass  # cache is best-effort; the decision itself is always correct
