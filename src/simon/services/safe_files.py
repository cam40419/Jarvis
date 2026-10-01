"""Open a regular local file without following a raced filesystem redirect."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO


def _posix_descriptor(path: Path) -> int:
    # Directory handles pin every ancestor; O_NOFOLLOW on only the leaf is
    # insufficient when a parent can be replaced between validation and open.
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None or os.open not in os.supports_dir_fd:
        raise OSError("Safe local file access is unavailable on this platform")
    ancestor = os.open(path.anchor, os.O_RDONLY | directory | nofollow)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=ancestor)
            os.close(ancestor)
            ancestor = child
        return os.open(
            path.name, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0), dir_fd=ancestor,
        )
    finally:
        os.close(ancestor)


def _windows_descriptor(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class AttributeTag(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # OPEN_EXISTING + OPEN_REPARSE_POINT. Reject a leaf reparse point before
    # reading it; the handle's final path detects redirects in raced ancestors.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError("Local file could not be opened")
    try:
        info = AttributeTag()
        if not kernel.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info),
        ) or info.attributes & (0x400 | 0x10):
            raise OSError("Local file is a filesystem redirect or directory")
        result = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, result, len(result), 0)
        if not length or length >= len(result):
            raise OSError("Local file location could not be verified")
        opened = result.value
        if opened.startswith("\\\\?\\UNC\\"):
            opened = "\\\\" + opened[8:]
        elif opened.startswith("\\\\?\\"):
            opened = opened[4:]
        if os.path.normcase(opened) != os.path.normcase(str(path)):
            raise OSError("Local file location changed while opening")
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def open_regular_nofollow(path: Path) -> BinaryIO:
    """Return an owned binary stream; errors never expose unvalidated content."""
    absolute = Path(os.path.abspath(path))  # Lexical normalization, never resolve links.
    descriptor = (_windows_descriptor(absolute) if os.name == "nt"
                  else _posix_descriptor(absolute))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Local file is not a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
