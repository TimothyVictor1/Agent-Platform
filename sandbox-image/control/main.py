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
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import execs as exec_module
from . import files_lib, screen
from . import input as input_lib
from .auth import require_bearer
from .config import (
    MAX_CLICK_COUNT,
    MAX_FILE_BYTES,
    MAX_INTERVAL_MS,
    MAX_SCROLL_AMOUNT,
    MAX_TYPE_CHARS,
    MAX_TYPE_TOTAL_MS,
    MIN_INTERVAL_MS,
    MIN_SCROLL_AMOUNT,
)

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

# Request-body cap: the 50 MB file limit plus headroom for the multipart
# form overhead (boundaries, headers, form fields).
MAX_REQUEST_BODY_BYTES = MAX_FILE_BYTES + 65_536


def _error(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


class _BodyTooLarge(HTTPException):
    """The streaming request body exceeded MAX_REQUEST_BODY_BYTES.

    An HTTPException on purpose: FastAPI wraps any other exception raised
    while it parses a form body into a generic 400, but re-raises
    HTTPException so the app's exception handler answers 413.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body exceeds the size limit")


async def _send_413(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": b'{"detail":"request body exceeds the size limit"}',
        }
    )


class BodyLimitMiddleware:
    """Pure ASGI middleware capping the size of request bodies.

    Two layers of defence for the multipart upload endpoint: an early
    Content-Length check (rejects before a byte is read) and a byte counter
    on the receive channel for chunked/unknown-length bodies. Both respond
    413 instead of letting the upload stream into the container.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    await _send_413(send)
                    return
            except ValueError:
                pass  # malformed header: let the app deal with it

        received = 0
        response_started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > MAX_REQUEST_BODY_BYTES:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:
                # The response head is already gone; let the server tear
                # the connection down rather than fake a complete reply.
                raise
            await _send_413(send)


# Registered as user middleware so it sits INSIDE ServerErrorMiddleware:
# _BodyTooLarge raised while the endpoint streams the body is caught here
# (response not started) and answered with 413 instead of a 500.
app.add_middleware(BodyLimitMiddleware)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XY(Strict):
    x: int
    y: int

    @model_validator(mode="after")
    def _in_screen(self) -> XY:
        input_lib.check_coords(self.x, self.y)
        return self


class ClickIn(Strict):
    x: int
    y: int
    button: Literal["left", "middle", "right"] = "left"
    count: int = 1

    @model_validator(mode="after")
    def _validate(self) -> ClickIn:
        input_lib.check_coords(self.x, self.y)
        if not 1 <= self.count <= MAX_CLICK_COUNT:
            raise ValueError(f"count must be 1..{MAX_CLICK_COUNT}")
        return self


class DragIn(Strict):
    x1: int
    y1: int
    x2: int
    y2: int

    @model_validator(mode="after")
    def _validate(self) -> DragIn:
        input_lib.check_coords(self.x1, self.y1)
        input_lib.check_coords(self.x2, self.y2)
        return self


class ScrollIn(Strict):
    x: int
    y: int
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = 3

    @model_validator(mode="after")
    def _validate(self) -> ScrollIn:
        input_lib.check_coords(self.x, self.y)
        if not MIN_SCROLL_AMOUNT <= self.amount <= MAX_SCROLL_AMOUNT:
            raise ValueError(f"amount must be in {MIN_SCROLL_AMOUNT}..{MAX_SCROLL_AMOUNT}")
        return self


class TypeIn(Strict):
    text: str
    interval_ms: int = 10

    @model_validator(mode="after")
    def _validate(self) -> TypeIn:
        if not self.text:
            raise ValueError("text must not be empty")
        if len(self.text) > MAX_TYPE_CHARS:
            raise ValueError(f"text exceeds {MAX_TYPE_CHARS} characters")
        if not MIN_INTERVAL_MS <= self.interval_ms <= MAX_INTERVAL_MS:
            raise ValueError(f"interval_ms must be in {MIN_INTERVAL_MS}..{MAX_INTERVAL_MS}")
        if len(self.text) * self.interval_ms > MAX_TYPE_TOTAL_MS:
            raise ValueError("text * interval_ms would take too long; split the call")
        return self


class KeyIn(Strict):
    combo: str


class CommandIn(Strict):
    cmd: str
    timeout_s: int | None = None


async def _run_locked(fn: Callable[..., object], *args: object) -> None:
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
async def get_screenshot(_auth: None = _AUTH) -> Response:
    try:
        png = await asyncio.to_thread(screen.grab_png)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return Response(content=png, media_type="image/png")


# --- input ------------------------------------------------------------------
@app.post("/click")
async def click(body: ClickIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.click, body.x, body.y, body.button, body.count)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/move")
async def move(body: XY, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.move, body.x, body.y)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/drag")
async def drag(body: DragIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.drag, body.x1, body.y1, body.x2, body.y2)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/scroll")
async def scroll(body: ScrollIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.scroll, body.x, body.y, body.direction, body.amount)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/type")
async def type_text(body: TypeIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.type_text, body.text, body.interval_ms)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"status": "ok"})


@app.post("/key")
async def key(body: KeyIn, _auth: None = _AUTH) -> JSONResponse:
    try:
        await _run_locked(input_lib.key, body.combo)
    except Exception as exc:
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
    file: UploadFile = File(...),  # noqa: B008 - FastAPI's multipart pattern
) -> JSONResponse:
    try:
        files_lib.check_contained(path, must_exist=False)
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
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"path": path, "bytes": total})


@app.get("/files/read")
async def files_read(_auth: None = _AUTH, path: Annotated[str, Query(...)] = "") -> Response:
    try:
        data = await asyncio.to_thread(files_lib.read_bytes, path)
    except Exception as exc:
        raise _map_errors(exc) from exc
    basename = path.rstrip("/").rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            # RFC 5987 encoding: safe for any basename, including
            # non-ASCII and characters special to header parsing.
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(basename)}",
            "Cache-Control": "no-store",
        },
    )


@app.get("/files/list")
async def files_list(_auth: None = _AUTH, path: Annotated[str, Query(...)] = "") -> JSONResponse:
    try:
        entries = await asyncio.to_thread(files_lib.list_dir, path)
    except Exception as exc:
        raise _map_errors(exc) from exc
    return JSONResponse({"path": path, "entries": entries})
