"""FastAPI app binding the implementation modules to the HTTP routes.

Routes mirror the SandboxHandle verbs in SPEC.md §9.1:
    GET  /health
    GET  /screen
    GET  /screenshot
    POST /click | /move | /drag | /scroll | /type | /key
    POST /command {cmd,timeout_s}
    POST /files/write (multipart: path + file)
    GET  /files/read?path=
    GET  /files/list?path=

Every endpoint requires ``Authorization: Bearer <CONTROL_TOKEN>``.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response
from fastapi.responses import Response as FileRespResponse
from starlette.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, model_validator

from . import execs as exec_module
from . import files_lib
from . import input as input_lib
from . import screen
from .auth import require_bearer

app = FastAPI(
    title="agentplatform-sandbox-control",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Serialise pointer/keyboard mutations so concurrent calls cannot interleave
# a click with a drag or mangle an in-progress mousedown/mouseup pair.
INPUT_LOCK = asyncio.Lock()

_AUTH = Depends(require_bearer)


def _error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XY(Strict):
    x: int
    y: int

    @model_validator(mode="after")
    def _in_screen(self) -> "XY":
        input_lib.check_coords(self.x, self.y)
        return self


class ClickIn(Strict):
    x: int
    y: int
    button: Literal["left", "middle", "right"] = "left"
    count: int = 1

    @model_validator(mode="after")
    def _validate(self) -> "ClickIn":
        input_lib.check_coords(self.x, self.y)
        if not 1 <= self.count <= 3:
            raise ValueError("count must be 1, 2 or 3")
        return self


class DragIn(Strict):
    x1: int
    y1: int
    x2: int
    y2: int

    @model_validator(mode="after")
    def _validate(self) -> "DragIn":
        input_lib.check_coords(self.x1, self.y1)
        input_lib.check_coords(self.x2, self.y2)
        return self


class ScrollIn(Strict):
    x: int
    y: int
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = 3

    @model_validator(mode="after")
    def _validate(self) -> "ScrollIn":
        input_lib.check_coords(self.x, self.y)
        if not 1 <= self.amount <= 50:
            raise ValueError("amount must be in 1..50")
        return self


class TypeIn(Strict):
    text: str
    interval_ms: int = 10

    @model_validator(mode="after")
    def _validate(self) -> "TypeIn":
        from . import config

        if not self.text:
            raise ValueError("text must not be empty")
        if len(self.text) > config.MAX_TYPE_CHARS:
            raise ValueError(f"text exceeds {config.MAX_TYPE_CHARS} characters")
        if not config.MIN_INTERVAL_MS <= self.interval_ms <= config.MAX_INTERVAL_MS:
            raise ValueError(
                f"interval_ms must be in {config.MIN_INTERVAL_MS}..{config.MAX_INTERVAL_MS}"
            )
        if len(self.text) * self.interval_ms > config.MAX_TYPE_TOTAL_MS:
            raise ValueError("text * interval_ms would take too long; split the call")
        return self


class KeyIn(Strict):
    combo: str


class CommandIn(Strict):
    cmd: str
    timeout_s: int | None = None


async def _run_locked(fn, *args: object) -> None:
    async with INPUT_LOCK:
        await asyncio.to_thread(fn, *args)


def _map_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, OverflowError):
        return _error(413, str(exc))
    if isinstance(exc, FileNotFoundError):
        return _error(404, f"file not found: {exc}")
    if isinstance(exc, IsADirectoryError):
        return _error(422, f"path is a directory: {exc}")
    if isinstance(exc, NotADirectoryError):
        return _error(404, f"path is not a directory: {exc}")
    if isinstance(exc, ValueError):
        return _error(422, str(exc))
    return _error(500, f"internal error: {exc}")


# --- status -------------------------------------------------------------------
@app.get("/health")
async def health(_auth: None = _AUTH) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/screen")
async def get_screen(_auth: None = _AUTH) -> JSONResponse:
    return JSONResponse(screen.asdict())


@app.get("/screenshot")
async def get_screenshot(_auth: None = _AUTH) -> FileRespResponse:
    try:
        png = await asyncio.to_thread(screen.grab_png)
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return Response(content=png, media_type="image/png")


# --- input ------------------------------------------------------------------
@app.post("/click")
async def click(body: ClickIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(
            input_lib.click, body.x, body.y, body.button, body.count
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/move")
async def move(body: XY, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.move, body.x, body.y)
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/drag")
async def drag(body: DragIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(
            input_lib.drag, body.x1, body.y1, body.x2, body.y2
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/scroll")
async def scroll(body: ScrollIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(
            input_lib.scroll, body.x, body.y, body.direction, body.amount
        )
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/type")
async def type_text(body: TypeIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.type_text, body.text, body.interval_ms)
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/key")
async def key(body: KeyIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.key, body.combo)
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


# --- command -----------------------------------------------------------------
@app.post("/command")
async def command(body: CommandIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        outcome = await exec_module.run_command(body.cmd, body.timeout_s)
    except ValueError as exc:
        raise _map_errors(exc) from exc
    return JSONResponse(outcome.asdict())


# --- files --------------------------------------------------------------------
@app.post("/files/write")
async def files_write(
    _auth: None = _AUTH,
    path: str = Form(...),
    file: UploadFile = File(...),
) -> JSONResponse:
    try:
        files_lib.validate_absolute(path)
    except ValueError as exc:
        raise _error(422, str(exc)) from exc

    async def chunks() -> AsyncIterator[bytes]:
        while True:
            block = await file.read(1 << 20)
            if not block:
                break
            yield block

    try:
        total = await files_lib.write_stream(path, chunks())
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"path": path, "bytes": total})


@app.get("/files/read")
async def files_read(
    _auth: None = _AUTH, path: Annotated[str, Query(...)] = ""
) -> Response:
    try:
        data = await asyncio.to_thread(files_lib.read_bytes, path)
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    basename = path.rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{basename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/files/list")
async def files_list(
    _auth: None = _AUTH, path: Annotated[str, Query(...)] = ""
) -> JSONResponse:
    try:
        entries = await asyncio.to_thread(files_lib.list_dir, path)
    except Exception as exc:  # noqa: BLE001
        raise _map_errors(exc) from exc
    return JSONResponse({"path": path, "entries": entries})
