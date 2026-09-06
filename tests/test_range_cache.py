"""
RangeCache tests (docs/ARCHITECTURE.md §11, P0.2).

Part 1 — unit: the cache layer against an in-memory upstream (byte pattern):
  correctness, chunk coalescing, single-flight, LRU eviction, disabled mode.
Part 2 — integration: the real FastAPI app + fake Drive upstream with the
  cache ENABLED: identical bytes to passthrough, upstream fetches coalesced
  into 8 MiB chunks, second access served from disk (zero content fetches).

Run from the repo root:  python tests/test_range_cache.py
"""
import asyncio
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

import fake_drive  # noqa: E402
import rangecache  # noqa: E402

failures = []


def check(name, ok, extra=""):
    tag = "PASS" if ok else "FAIL"
    if not ok:
        failures.append(name)
    print(f"[{tag}] {name}" + (f"  -- {extra}" if extra and not ok else ""))


# --------------------------------------------------------------------------
# Part 1 — unit
# --------------------------------------------------------------------------

PATTERN = bytes((i * 13 + 5) % 256 for i in range(25 * 1024 * 1024))  # 25 MiB
CHUNK = 1024 * 1024  # 1 MiB chunks → 25 of them


def unit_tests():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rangecache-"))

    # --- correctness + coalescing ------------------------------------------
    async def scenario1():
        calls = {"n": 0}

        async def fetch(start, end):
            calls["n"] += 1
            return PATTERN[start:end + 1]

        cache = rangecache.RangeCache(tmp / "u1", max_bytes=200 * 1024 * 1024,
                                      chunk_size=CHUNK)
        # a range spanning chunks 2..5 (2.5 MiB .. 5.5 MiB)
        body = await cache.get_range("f1", "v1", 2 * CHUNK + 100,
                                     5 * CHUNK + 200, fetch)
        expected = PATTERN[2 * CHUNK + 100:5 * CHUNK + 201]
        assert body == expected, "byte mismatch"
        first_calls = calls["n"]
        assert first_calls == 4, f"expected 4 chunk fetches, got {first_calls}"
        # repeat: fully cached
        body2 = await cache.get_range("f1", "v1", 2 * CHUNK + 100,
                                      5 * CHUNK + 200, fetch)
        assert body2 == body
        assert calls["n"] == first_calls, "repeat must not refetch"
        # sub-range inside already-cached chunks
        body3 = await cache.get_range("f1", "v1", 3 * CHUNK, 3 * CHUNK + 9, fetch)
        assert body3 == PATTERN[3 * CHUNK:3 * CHUNK + 10]
        assert calls["n"] == first_calls
        # a NEW file key must not see the old cache
        body4 = await cache.get_range("f1", "v2", 0, CHUNK - 1, fetch)
        assert body4 == PATTERN[:CHUNK]
        assert calls["n"] == first_calls + 1
        return cache, calls

    asyncio.run(scenario1())  # asserts inside
    check("unit: byte correctness + chunk coalescing + version isolation", True)

    # --- single-flight -------------------------------------------------------
    async def scenario2():
        calls = {"n": 0}

        async def fetch(start, end):
            calls["n"] += 1
            await asyncio.sleep(0.05)  # make the race window real
            return PATTERN[start:end + 1]

        cache = rangecache.RangeCache(tmp / "u2", max_bytes=200 * 1024 * 1024,
                                      chunk_size=CHUNK)
        results = await asyncio.gather(*[
            cache.get_range("f1", "v1", 0, CHUNK - 1, fetch) for _ in range(8)
        ])
        assert all(r == PATTERN[:CHUNK] for r in results)
        return calls["n"]

    n = asyncio.run(scenario2())
    check("unit: 8 concurrent cold readers → exactly 1 upstream fetch",
          n == 1, f"got {n} fetches")

    # --- crash safety: a torn chunk (index says complete, disk short) --------
    async def scenario3():
        calls = {"n": 0}

        async def fetch(start, end):
            calls["n"] += 1
            return PATTERN[start:end + 1]

        cache = rangecache.RangeCache(tmp / "u3", max_bytes=200 * 1024 * 1024,
                                      chunk_size=CHUNK)
        await cache.get_range("f1", "v1", 0, CHUNK - 1, fetch)
        # simulate a torn write: truncate the data file, keep the index
        data_path = cache._paths("f1", "v1")[0]
        with open(data_path, "rb+") as f:
            f.truncate(CHUNK // 2)
        body = await cache.get_range("f1", "v1", 0, CHUNK - 1, fetch)
        assert body == PATTERN[:CHUNK]
        return calls["n"]

    n = asyncio.run(scenario3())
    check("unit: torn chunk is refetched, never served short", n == 2,
          f"got {n} fetches")

    # --- disabled mode --------------------------------------------------------
    async def scenario4():
        cache = rangecache.RangeCache(tmp / "u4", max_bytes=0)
        try:
            await cache.get_range("f1", "v1", 0, 99, lambda s, e: b"")
            return False
        except rangecache.RangeFetchError:
            return True

    check("unit: disabled cache raises (caller uses passthrough)",
          asyncio.run(scenario4()))
    u4 = tmp / "u4"
    check("unit: disabled cache writes no files",
          not u4.exists() or not any(u4.iterdir()))

    # --- LRU eviction ---------------------------------------------------------
    async def scenario5():
        calls = {"n": 0}

        async def fetch(start, end):
            calls["n"] += 1
            return PATTERN[start:end + 1]

        # Budget: 8 MiB (8 chunks). File f1 gets its first 4 chunks, then
        # file f2 gets ITS first 4 chunks → total hits the ceiling and the
        # 85% watermark evicts the LRU file (f1); f2 (recently active) stays.
        # Re-touching f1 refetches exactly the one requested chunk.
        cache = rangecache.RangeCache(tmp / "u5", max_bytes=8 * CHUNK,
                                      chunk_size=CHUNK)
        for i in range(4):
            await cache.get_range("f1", "v1", i * CHUNK, i * CHUNK + 9, fetch)
        for i in range(4):
            await cache.get_range("f2", "v1", i * CHUNK, i * CHUNK + 9, fetch)
        cache.evict()  # force synchronously (the bg task may have raced)
        p1, p2 = (cache._paths("f1", "v1")[0], cache._paths("f2", "v1")[0])
        # existence BEFORE the refetch below (which recreates f1)
        f1_gone, f2_kept = (not p1.exists()), p2.exists()
        before = calls["n"]
        body = await cache.get_range("f1", "v1", 0, CHUNK - 1, fetch)
        return (f1_gone, f2_kept, cache._total_size(),
                body == PATTERN[:CHUNK], calls["n"] - before)

    f1_gone, f2_kept, total, refetch_ok, refetches = asyncio.run(scenario5())
    check("unit: LRU evicts oldest file, keeps recent, under budget",
          f1_gone and f2_kept and total <= 8 * CHUNK,
          f"f1_gone={f1_gone} f2_kept={f2_kept} total={total} budget={8 * CHUNK}")
    check("unit: evicted file is refetched transparently",
          refetch_ok and refetches == 1, f"refetches={refetches}")


# --------------------------------------------------------------------------
# Part 2 — integration through the real app
# --------------------------------------------------------------------------

def integration_tests():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="rangecache-it-"))
    _token = tmp / "token.json"
    _token.write_text(__import__("json").dumps({
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "expires_at": time.time() + 3600,
    }))
    os.environ["DRIVE_API_BASE"] = "http://127.0.0.1:9011"
    os.environ["TOKEN_FILE"] = str(_token)
    os.environ["CACHE_DIR"] = str(tmp / "cache")
    os.environ["RANGE_CACHE_MAX_BYTES"] = str(64 * 1024 * 1024)

    import uvicorn
    import app as poc
    from fastapi.testclient import TestClient

    FAKE_PORT = 9011
    config = uvicorn.Config(fake_drive.app, host="127.0.0.1", port=FAKE_PORT,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", FAKE_PORT), timeout=0.25):
                break
        except OSError:
            time.sleep(0.05)

    client = TestClient(poc.app)
    data, size = fake_drive.DATA, fake_drive.SIZE

    # 1. First range read: correct + exactly ONE upstream content fetch
    #    (5 MiB file < 8 MiB chunk → one fetch coalesces everything)
    fake_drive.MEDIA_FETCHES = 0
    r = client.get("/stream/vid123", headers={"Range": "bytes=1000-1999"})
    check("int: first range GET 206 + bytes",
          r.status_code == 206 and r.content == data[1000:2000],
          f"{r.status_code} {len(r.content)}")
    check("int: one upstream content fetch (chunk coalesced)",
          fake_drive.MEDIA_FETCHES == 1, f"got {fake_drive.MEDIA_FETCHES}")
    check("int: content-range header correct",
          r.headers.get("content-range") == f"bytes 1000-1999/{size}",
          r.headers.get("content-range", "<missing>"))

    # 2. Repeated/different ranges: served from disk, NO new content fetches
    r = client.get("/stream/vid123", headers={"Range": "bytes=0-500"})
    check("int: second range GET 206 + bytes (cached)",
          r.status_code == 206 and r.content == data[:501])
    r = client.get("/stream/vid123", headers={"Range": "bytes=4194304-"})
    check("int: open-ended range served from cache",
          r.status_code == 206 and r.content == data[4_194_304:])
    r = client.get("/stream/vid123", headers={"Range": "bytes=-500"})
    check("int: suffix range served from cache",
          r.status_code == 206 and r.content == data[-500:])
    check("int: no new upstream content fetches after warm-up",
          fake_drive.MEDIA_FETCHES == 1, f"got {fake_drive.MEDIA_FETCHES}")

    # 3. 416 semantics
    r = client.get("/stream/vid123", headers={"Range": f"bytes={size}-"})
    check("int: unsatisfiable range → 416", r.status_code == 416)

    # 4. debug endpoint reports the cache
    r = client.get("/api/debug")
    dc = r.json()["range_cache"]
    check("int: /api/debug range_cache stats",
          r.status_code == 200 and dc["files"] >= 1 and dc["total_bytes"] > 0,
          str(dc))

    server.should_exit = True


# --------------------------------------------------------------------------

if __name__ == "__main__":
    unit_tests()
    integration_tests()
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("All RangeCache checks passed.")
