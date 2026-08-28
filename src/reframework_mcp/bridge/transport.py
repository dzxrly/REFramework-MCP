"""Transport implementations for the in-process REFramework bridge."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import struct
from collections.abc import Awaitable, Callable
from ctypes import wintypes
from typing import Any, Protocol, cast

from reframework_mcp.bridge.protocol import MAX_FRAME_BYTES
from reframework_mcp.errors import ErrorCode, ReframeworkMCPError


def _load_kernel32() -> Any:
    """Load kernel32 without exposing Windows-only ctypes names to other platforms."""
    win_dll = vars(ctypes).get("WinDLL")
    if not callable(win_dll):
        raise ReframeworkMCPError(
            ErrorCode.BRIDGE_DISCONNECTED,
            "The REFramework bridge named pipe is only available on Windows.",
        )
    return win_dll("kernel32", use_last_error=True)


def _get_last_error() -> int:
    """Return the calling thread's Win32 error, or zero off Windows."""
    get_last_error = vars(ctypes).get("get_last_error")
    if not callable(get_last_error):
        return 0
    return int(get_last_error())


class BridgeTransport(Protocol):
    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


class InMemoryTransport:
    """Deterministic transport for tests and offline development."""

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> None:
        self.handler = handler

    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        result = self.handler(payload)
        if asyncio.iscoroutine(result):
            return cast(dict[str, Any], await asyncio.wait_for(result, timeout))
        return cast(dict[str, Any], result)


class NamedPipeTransport:
    """One request per Win32 named-pipe connection using length-prefixed JSON."""

    def __init__(self, pipe_name: str, connect_timeout: float = 2.0) -> None:
        self.pipe_name = pipe_name
        self.connect_timeout = connect_timeout

    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if os.name != "nt":
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_DISCONNECTED,
                "The REFramework bridge named pipe is only available on Windows.",
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._request_sync, payload),
                timeout=timeout,
            )
        except TimeoutError as error:
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_TIMEOUT,
                f"Bridge request exceeded {timeout:.1f} seconds",
                retryable=True,
            ) from error

    def _request_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        kernel32 = _load_kernel32()
        wait_named_pipe = kernel32.WaitNamedPipeW
        wait_named_pipe.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        wait_named_pipe.restype = wintypes.BOOL

        timeout_ms = max(1, int(self.connect_timeout * 1000))
        if not wait_named_pipe(self.pipe_name, timeout_ms):
            error_code = _get_last_error()
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_DISCONNECTED,
                f"Bridge pipe is not available: {self.pipe_name}",
                details={"win32_error": error_code},
                retryable=True,
            )

        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            self.pipe_name,
            0xC0000000,
            0,
            None,
            3,
            0,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            error_code = _get_last_error()
            raise ReframeworkMCPError(
                ErrorCode.BRIDGE_DISCONNECTED,
                "Failed to open the bridge pipe",
                details={"win32_error": error_code},
                retryable=True,
            )

        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > MAX_FRAME_BYTES:
                raise ReframeworkMCPError(
                    ErrorCode.INVALID_REQUEST,
                    "Bridge request is larger than the frame limit",
                    details={"size": len(encoded), "limit": MAX_FRAME_BYTES},
                )
            self._write_all(kernel32, handle, struct.pack("<I", len(encoded)) + encoded)
            header = self._read_exact(kernel32, handle, 4)
            response_size = struct.unpack("<I", header)[0]
            if response_size > MAX_FRAME_BYTES:
                raise ReframeworkMCPError(
                    ErrorCode.BRIDGE_PROTOCOL_ERROR,
                    "Bridge response is larger than the frame limit",
                    details={"size": response_size, "limit": MAX_FRAME_BYTES},
                )
            body = self._read_exact(kernel32, handle, response_size)
            decoded = json.loads(body.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ReframeworkMCPError(
                    ErrorCode.BRIDGE_PROTOCOL_ERROR,
                    "Bridge returned a non-object JSON response",
                )
            return decoded
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _write_all(kernel32: Any, handle: int, data: bytes) -> None:
        write_file = kernel32.WriteFile
        write_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        write_file.restype = wintypes.BOOL
        offset = 0
        while offset < len(data):
            written = wintypes.DWORD()
            buffer = ctypes.create_string_buffer(data[offset:])
            ok = write_file(
                handle,
                buffer,
                len(data) - offset,
                ctypes.byref(written),
                None,
            )
            if not ok or written.value == 0:
                raise ReframeworkMCPError(
                    ErrorCode.BRIDGE_PROTOCOL_ERROR,
                    "Failed to write a bridge frame",
                    details={"win32_error": _get_last_error()},
                    retryable=True,
                )
            offset += written.value

    @staticmethod
    def _read_exact(kernel32: Any, handle: int, size: int) -> bytes:
        read_file = kernel32.ReadFile
        read_file.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        read_file.restype = wintypes.BOOL
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            buffer = ctypes.create_string_buffer(remaining)
            read = wintypes.DWORD()
            ok = read_file(handle, buffer, remaining, ctypes.byref(read), None)
            if not ok or read.value == 0:
                raise ReframeworkMCPError(
                    ErrorCode.BRIDGE_PROTOCOL_ERROR,
                    "Bridge closed before the full frame was read",
                    details={"win32_error": _get_last_error()},
                    retryable=True,
                )
            chunks.append(buffer.raw[: read.value])
            remaining -= read.value
        return b"".join(chunks)
