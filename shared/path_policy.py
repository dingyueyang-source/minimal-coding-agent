"""Pure string validation shared by the backend and isolated helper."""
import re

FILE_LIMIT = 64 * 1024
PATH_LIMIT = 255
INPUT_LIMIT = 512 * 1024


class PolicyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def utf8(value: object, field: str) -> bytes:
    if not isinstance(value, str):
        raise PolicyError("INVALID_ARGUMENT", f"{field} must be a string")
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise PolicyError("INVALID_UNICODE", f"{field} contains invalid Unicode") from None


def validate_path(path: object) -> str:
    data = utf8(path, "path")
    if not path or path.startswith("/") or "\\" in path or re.match(r"^[A-Za-z]:", path):
        raise PolicyError("PATH_REJECTED", "Use a nonempty relative POSIX path")
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in path):
        raise PolicyError("PATH_REJECTED", "Control characters are forbidden")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PolicyError("PATH_REJECTED", "Empty, dot and parent components are forbidden")
    if len(data) > PATH_LIMIT or any(len(p.encode("utf-8")) > PATH_LIMIT for p in parts):
        raise PolicyError("PATH_REJECTED", "Path exceeds the UTF-8 byte limit")
    return path


def validate_write(payload: object) -> tuple[str, bytes]:
    if not isinstance(payload, dict) or set(payload) != {"path", "content"}:
        raise PolicyError("INVALID_ARGUMENT", "Expected exactly path and content")
    path = validate_path(payload["path"])
    content = utf8(payload["content"], "content")
    if len(content) > FILE_LIMIT:
        raise PolicyError("FILE_TOO_LARGE", "Content exceeds 64 KiB")
    return path, content


def validate_request(request: object) -> str:
    data = utf8(request, "request")
    if not request.strip() or len(data) > 8192:
        raise PolicyError("INVALID_REQUEST", "Request must be nonblank and at most 8 KiB")
    return request
