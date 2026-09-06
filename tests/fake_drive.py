"""
A tiny fake 'Google Drive' upstream used by test_range_proxy.py.

It serves a deterministic 5 MiB byte pattern and honors Range requests the
same way Drive's alt=media endpoint does (200 full body, or 206 + Content-Range).
"""
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

SIZE = 5 * 1024 * 1024  # 5 MiB
DATA = bytes((i * 7) % 256 for i in range(SIZE))


def _parse_range(header: str, size: int) -> tuple[int, int]:
    """Parse 'bytes=start-end' / 'bytes=start-' / 'bytes=-suffix'."""
    _, _, rng = header.partition("=")
    start_s, _, end_s = rng.partition("-")
    if start_s == "":  # suffix range: last N bytes
        n = int(end_s)
        return size - n, size - 1
    start = int(start_s)
    end = int(end_s) if end_s else size - 1
    return start, min(end, size - 1)


async def get_file(request):
    if request.query_params.get("alt") != "media":
        return JSONResponse({
            "id": request.path_params["file_id"],
            "mimeType": "video/mp4",
            "size": str(SIZE),
        })

    range_header = request.headers.get("range")
    if not range_header:
        return Response(DATA, media_type="video/mp4")

    start, end = _parse_range(range_header, SIZE)
    return Response(
        DATA[start:end + 1],
        status_code=206,
        media_type="video/mp4",
        headers={
            "Content-Range": f"bytes {start}-{end}/{SIZE}",
            "Accept-Ranges": "bytes",
        },
    )


async def list_files(request):
    return JSONResponse({"files": [{
        "id": "vid123",
        "name": "demo-video.mp4",
        "mimeType": "video/mp4",
        "size": str(SIZE),
        "modifiedTime": "2026-09-01T00:00:00Z",
    }]})


app = Starlette(routes=[
    Route("/files", list_files),
    Route("/files/{file_id}", get_file),
])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=9010, log_level="warning")
