"""Fixed helper: python3 -I -B /opt/app/tool.py write|read.

Only /opt/app is inserted into sys.path; task content is never imported.
"""
import base64
import json
import os
import secrets
import stat
import sys
from contextlib import contextmanager

sys.path.insert(0, "/opt/app")
from shared.path_policy import FILE_LIMIT, INPUT_LIMIT, PolicyError, validate_path, validate_write


@contextmanager
def parent_fd(path, create=False):
    fd = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PolicyError("UNSAFE_TARGET", "Target must be a regular file with one link")


def write(payload):
    path, content = validate_write(payload)
    with parent_fd(path, create=True) as (directory, name):
        try:
            regular(os.stat(name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            pass
        temp = ".mca-" + secrets.token_hex(16)
        fd = None
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            view = memoryview(content)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("short write")
                view = view[count:]
            os.close(fd)
            fd = None
            os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
    return {"ok": True, "path": path, "bytes_written": len(content)}


def read(payload):
    if not isinstance(payload, dict) or set(payload) != {"path"}:
        raise PolicyError("INVALID_ARGUMENT", "Expected exactly path")
    path = validate_path(payload["path"])
    with parent_fd(path) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(fd)
            regular(info)
            chunks = bytearray()
            while len(chunks) <= FILE_LIMIT:
                block = os.read(fd, FILE_LIMIT + 1 - len(chunks))
                if not block:
                    break
                chunks.extend(block)
        finally:
            os.close(fd)
    preview = bytes(chunks[:FILE_LIMIT])
    return {"ok": True, "path": path, "data_base64": base64.b64encode(preview).decode("ascii"),
            "size_bytes": info.st_size, "bytes_returned": len(preview),
            "truncated": len(chunks) > FILE_LIMIT}


def main():
    try:
        raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        if len(raw) > INPUT_LIMIT:
            raise PolicyError("INPUT_TOO_LARGE", "Helper input exceeds limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise PolicyError("INVALID_JSON", "Invalid UTF-8 JSON") from None
        operation = sys.argv[1] if len(sys.argv) == 2 else ""
        if operation not in ("write", "read"):
            raise PolicyError("INVALID_OPERATION", "Expected write or read")
        result = {"write": write, "read": read}[operation](payload)
        code = 0
    except PolicyError as exc:
        result = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        code = 2
    except OSError:
        result = {"ok": False, "error": {"code": "UNSAFE_OR_UNAVAILABLE", "message": "Safe filesystem operation refused"}}
        code = 3
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
