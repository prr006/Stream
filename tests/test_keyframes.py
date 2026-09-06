"""
Keyframe-index tests (docs/ARCHITECTURE.md §4, P0.3).

Generates real media with the bundled ffmpeg (30 fps, GOP 30 → a keyframe
every 1.0 s) and verifies the Range-read indexers:

  MP4 (moov at end)      → source "stss", moov_position "end"
  MP4 (+faststart)       → moov_position "start", same keyframe times
  MKV                    → source "cues", same keyframe times
  garbage bytes          → source "none", no exception

No network involved: the fetcher slices a local byte array, exactly the
contract the Drive gateway provides.

Run from the repo root:  python tests/test_keyframes.py
"""
import asyncio
import pathlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "backend"))

import boxparse  # noqa: E402
import keyframes  # noqa: E402
import media  # noqa: E402

failures = []


def check(name, ok, extra=""):
    tag = "PASS" if ok else "FAIL"
    if not ok:
        failures.append(name)
    print(f"[{tag}] {name}" + (f"  -- {extra}" if extra and not ok else ""))


def ffmpeg_bin() -> str:
    ff = media.ffmpeg_bin()
    if not ff:
        print("[SKIP] no ffmpeg available — keyframe tests skipped")
        sys.exit(0)
    return ff


def make_sample(ff: str, fmt: str, extra: list[str], out: Path) -> None:
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=duration=5:size=320x240:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
           "-c:v", "libx264", "-preset", "ultrafast",
           "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "64k"] + extra + [str(out)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"sample generation failed ({fmt}):\n{r.stderr.decode()[-500:]}")


def mem_fetcher(data: bytes):
    async def fetch(start: int, end: int) -> bytes:
        end = min(end, len(data) - 1)
        return data[start:end + 1]
    return fetch


def close_enough(a: float, b: float, tol: float = 0.08) -> bool:
    return abs(a - b) <= tol


def expect_keyframe_grid(times: list[float]) -> bool:
    """5 s @ 30 fps with GOP 30 → 5 keyframes ≈ 0, 1, 2, 3, 4 s."""
    if len(times) < 4 or len(times) > 6:
        return False
    if not close_enough(times[0], 0.0, 0.05):
        return False
    grid = list(range(len(times)))
    return all(close_enough(t, g) for t, g in zip(times, grid))


def main() -> int:
    ff = ffmpeg_bin()
    tmp = Path(tempfile.mkdtemp(prefix="keyframes-"))

    # --- generate real samples ------------------------------------------------
    mp4 = tmp / "sample.mp4"          # moov at end (ffmpeg default)
    mp4_fast = tmp / "sample_fast.mp4"  # moov at start
    mkv = tmp / "sample.mkv"
    make_sample(ff, "mp4", [], mp4)
    make_sample(ff, "mp4-fast", ["-movflags", "+faststart"], mp4_fast)
    make_sample(ff, "mkv", ["-f", "matroska"], mkv)

    # --- MP4, moov at end -------------------------------------------------------
    data = mp4.read_bytes()
    idx = asyncio.run(keyframes.build_keyframe_index(
        {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}, mem_fetcher(data), len(data)))
    check("mp4: source=stss", idx["source"] == "stss",
          f"source={idx['source']} err={idx['error']}")
    check("mp4: moov_position=end", idx["moov_position"] == "end",
          str(idx["moov_position"]))
    check("mp4: not fragmented / not encrypted",
          not idx["fragmented"] and not idx["encrypted"])
    check("mp4: keyframe grid ≈ 0,1,2,3,4 s",
          expect_keyframe_grid(idx["times"]), str(idx["times"]))

    # --- MP4, faststart (moov at start) -----------------------------------------
    data = mp4_fast.read_bytes()
    idx = asyncio.run(keyframes.build_keyframe_index(
        {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}, mem_fetcher(data), len(data)))
    check("mp4-fast: moov_position=start", idx["moov_position"] == "start",
          str(idx["moov_position"]))
    check("mp4-fast: keyframe grid matches",
          expect_keyframe_grid(idx["times"]), str(idx["times"]))

    # --- MKV cues -----------------------------------------------------------------
    data = mkv.read_bytes()
    idx = asyncio.run(keyframes.build_keyframe_index(
        {"format_name": "matroska,webm"}, mem_fetcher(data), len(data)))
    check("mkv: source=cues", idx["source"] == "cues",
          f"source={idx['source']} err={idx['error']}")
    check("mkv: keyframe grid ≈ 0,1,2,3,4 s",
          expect_keyframe_grid(idx["times"]), str(idx["times"]))

    # --- direct boxparse entry points (unit level) ---------------------------------
    async def direct():
        # MP4 moov parse from raw bytes
        d = mp4.read_bytes()
        r = await boxparse.analyze_mp4(mem_fetcher(d), len(d))
        # MKV cues parse
        d2 = mkv.read_bytes()
        t = await boxparse.parse_mkvcues(mem_fetcher(d2), len(d2))
        # garbage
        d3 = bytes(64 * 1024)
        r3 = await boxparse.analyze_mp4(mem_fetcher(d3), len(d3))
        t3 = await boxparse.parse_mkvcues(mem_fetcher(d3), len(d3))
        return r, t, r3, t3

    r, t, r3, t3 = asyncio.run(direct())
    check("boxparse: mp4 direct parse finds keyframes", r["count"] >= 4, str(r))
    check("boxparse: mkv direct parse finds cues", bool(t) and len(t) >= 4, str(t))
    check("boxparse: garbage → source none, no exception",
          r3["count"] == 0 and t3 is None)

    # --- container mismatch: mp4 parser on mkv bytes must fail gracefully ----------
    idx = asyncio.run(keyframes.build_keyframe_index(
        {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        mem_fetcher(mkv.read_bytes()), len(mkv.read_bytes())))
    check("container mismatch fails gracefully (source=none)",
          idx["source"] == "none" and idx["count"] == 0, str(idx))

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED")
        return 1
    print("All keyframe-index checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
