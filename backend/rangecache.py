"""
RangeCache — chunked, coalesced, LRU cache of Google Drive file bytes.

Sits BEHIND the /stream/{file_id} proxy. The public HTTP API of the proxy is
unchanged; this layer only decides where the bytes come from (local disk vs
Drive). See docs/ARCHITECTURE.md §11.

Why it exists
-------------
FFmpeg/ffprobe and browsers issue their own small HTTP Range requests (tens
of KB to a few MB). Without coalescing, reading a 15 GB file means thousands
of Drive requests — every seek, every second viewer, every re-probe. The
RangeCache fetches from Drive in whole 8 MiB chunks and serves arbitrary
sub-ranges from local disk, so each Drive byte is fetched AT MOST ONCE per
file version, ever.

Design
------
* One sparse data file per (file_id, version): {root}/{safe_id}/{version}.cache
  plus a small index sidecar {version}.cache.idx (JSON: completed chunks).
  A chunk counts as CACHED only if the index lists it as complete — so a
  crash mid-write can never serve partial bytes.
* CHUNK_SIZE (default 8 MiB). A byte range [s, e] is covered by the whole
  chunks overlapping it; a missing chunk triggers exactly one upstream fetch.
* Single-flight: all chunk work for one file serializes on a per-file lock;
  concurrent waiters find the chunk on disk afterwards.
* LRU by whole cache file (the safe unit): when total apparent size exceeds
  max_bytes, least-recently-used files are deleted until below 85% of max.
* max_bytes == 0 → DISABLED: no files are created or read; callers must use
  the plain passthrough path (POC behavior, `--no-cache` parity).

The upstream fetcher is injected (async (start, end) -> bytes) so the layer
is testable against a fake Drive and keeps the bearer token inside the
gateway (app.py).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB — one Drive request per chunk

_EVICTION_TARGET = 0.85  # evict until below 85% of max_bytes

_safe_re = re.compile(r"[^A-Za-z0-9_.-]")


def safe_id(file_id: str) -> str:
    return _safe_re.sub("_", file_id)[:200] or "unknown"


class RangeFetchError(RuntimeError):
    pass


class RangeCache:
    def __init__(self, root: Path, max_bytes: int, chunk_size: int = CHUNK_SIZE):
        self.root = Path(root)
        self.max_bytes = int(max_bytes or 0)
        self.chunk_size = int(chunk_size)
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._lru: dict[str, float] = {}  # data_path -> monotonic touch
        self._active: set[str] = set()    # data file names held by in-flight reads
        self.stats = {"chunk_hits": 0, "chunk_misses": 0,
                      "upstream_chunks": 0, "bytes_served_cached": 0}
        if not self.disabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def disabled(self) -> bool:
        return self.max_bytes <= 0

    # ------------------------------------------------------------------
    def _paths(self, file_id: str, version: str) -> tuple[Path, Path]:
        v = re.sub(r"[^A-Za-z0-9_.-]", "_", str(version))[:64] or "unversioned"
        data = self.root / safe_id(file_id) / f"{v}.cache"
        return data, data.with_suffix(data.suffix + ".idx")

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._file_locks:
            self._file_locks[key] = asyncio.Lock()
        return self._file_locks[key]

    def _load_index(self, idx_path: Path) -> dict[int, int]:
        try:
            raw = json.loads(idx_path.read_text())
            return {int(k): int(v) for k, v in raw.get("chunks", {}).items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_index(self, idx_path: Path, chunks: dict[int, int]) -> None:
        tmp = idx_path.with_suffix(".idx.tmp")
        tmp.write_text(json.dumps({"chunks": {str(k): v for k, v in chunks.items()}}))
        os.replace(tmp, idx_path)  # atomic publish

    def _touch(self, key: str) -> None:
        self._lru[key] = time.monotonic()

    def _known_files(self) -> list[tuple[float, Path]]:
        out = []
        for key, ts in self._lru.items():
            try:
                p = Path(key)
                if p.exists():
                    out.append((ts, p))
            except OSError:
                pass
        return out

    def _total_size(self) -> int:
        return sum(p.stat().st_size for _, p in self._known_files())

    def evict(self) -> int:
        """
        Delete LRU whole cache files until under the budget. Files currently
        held by an in-flight get_range are skipped (they would be refetched
        mid-stream — the next eviction pass picks them up). Bytes freed.
        """
        freed = 0
        if self.disabled:
            return 0
        target = self.max_bytes * _EVICTION_TARGET
        while True:
            if self._total_size() <= target:
                break
            files = [f for f in sorted(self._known_files())
                     if f[1].name not in self._active]
            if not files:
                break  # only active files remain
            _, p = files[0]
            try:
                size = p.stat().st_size
                p.unlink()
                freed += size
            except OSError:
                pass
            self._lru.pop(str(p), None)
            for sibling in (p, p.with_suffix(p.suffix + ".idx")):
                if sibling.exists():
                    try:
                        sibling.unlink()
                    except OSError:
                        pass
            try:
                if p.parent.exists() and not any(p.parent.iterdir()):
                    p.parent.rmdir()
            except OSError:
                pass
        return freed

    # ------------------------------------------------------------------
    async def get_range(
        self,
        file_id: str,
        version: str,
        start: int,
        end: int,
        fetch: "async (int, int) -> bytes",
    ) -> bytes:
        """
        Return file bytes [start, end] (inclusive), fetching whole chunks via
        `fetch(chunk_start, chunk_end)` as needed. `fetch` returns the chunk
        bytes (the gateway clamps to the true file size).
        """
        if self.disabled:
            raise RangeFetchError("RangeCache is disabled (max_bytes <= 0)")
        start = max(0, int(start))
        end = max(start, int(end))

        data_path, idx_path = self._paths(file_id, version)
        key = str(data_path)
        data_path.parent.mkdir(parents=True, exist_ok=True)

        first = start // self.chunk_size
        last = end // self.chunk_size
        self._active.add(data_path.name)
        try:
            return await self._get_range_locked(key, data_path, idx_path,
                                                first, last, start, end, fetch)
        finally:
            self._active.discard(data_path.name)

    async def _get_range_locked(self, key, data_path, idx_path, first, last,
                                start, end, fetch) -> bytes:
        parts: list[bytes] = []
        async with self._lock(key):
            self._touch(key)
            chunks = self._load_index(idx_path)
            for ci in range(first, last + 1):
                cs = ci * self.chunk_size
                ce = cs + self.chunk_size - 1
                chunk = self._read_chunk(data_path, ci, chunks)
                if chunk is None:
                    self.stats["chunk_misses"] += 1
                    data = await fetch(cs, ce)
                    if data is None:
                        raise RangeFetchError(f"upstream fetch failed for chunk {ci}")
                    data = bytes(data)
                    fd = os.open(str(data_path), os.O_RDWR | os.O_CREAT, 0o644)
                    try:
                        os.pwrite(fd, data, cs)
                    finally:
                        os.close(fd)
                    chunks[ci] = len(data)
                    self._save_index(idx_path, chunks)  # durable only when complete
                    self.stats["upstream_chunks"] += 1
                    chunk = data
                else:
                    self.stats["chunk_hits"] += 1
                    self.stats["bytes_served_cached"] += len(chunk)
                lo = max(start, cs) - cs
                hi = min(end, ce) - cs
                parts.append(chunk[lo:hi + 1])
        if self._total_size() > self.max_bytes:
            asyncio.ensure_future(self._evict_bg())
        return b"".join(parts)

    async def _evict_bg(self) -> None:
        try:
            self.evict()
        except Exception:  # noqa: BLE001 — cache hygiene must never break a stream
            pass

    def _read_chunk(self, data_path: Path, ci: int,
                    chunks: dict[int, int]) -> bytes | None:
        if ci not in chunks:
            return None
        expect = chunks[ci]
        cs = ci * self.chunk_size
        try:
            with open(data_path, "rb") as f:
                f.seek(cs)
                data = f.read(expect)
        except OSError:
            return None
        if len(data) != expect:
            return None  # index says complete, disk disagrees → refetch
        return data

    # ------------------------------------------------------------------
    def stat(self) -> dict:
        return {
            "disabled": self.disabled,
            "max_bytes": self.max_bytes,
            "chunk_size": self.chunk_size,
            "files": len(self._lru),
            "total_bytes": self._total_size(),
            **self.stats,
        }
