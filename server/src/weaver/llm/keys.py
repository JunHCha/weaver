"""Resolve the API key from ``llm.api_key_file`` (preferred) or ``api_key_env``.

launchd jobs do not inherit the login shell's environment, so a private file
is the supported way to give the resident runtime a key. The file must be a
regular file (not a symlink) owned by the current user with no group/other
permission bits; only leading/trailing whitespace is stripped. Errors carry a
stable reason code, never the path or contents.
"""

import os
import stat
from collections.abc import Mapping

from weaver.config import LlmConfig

_MAX_KEY_BYTES = 4096


class KeyUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _read_key_file(path: os.PathLike[str] | str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise KeyUnavailable("api_key_file_missing") from None
    except OSError:
        # ELOOP (symlink) and permission problems are both unsafe/unusable.
        raise KeyUnavailable("api_key_file_insecure") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise KeyUnavailable("api_key_file_insecure")
        data = os.read(fd, _MAX_KEY_BYTES + 1)
    except OSError:
        raise KeyUnavailable("api_key_file_unreadable") from None
    finally:
        os.close(fd)
    if len(data) > _MAX_KEY_BYTES:
        raise KeyUnavailable("api_key_file_invalid")
    try:
        key = data.decode("utf-8").strip()
    except UnicodeError:
        raise KeyUnavailable("api_key_file_invalid") from None
    if not key:
        raise KeyUnavailable("api_key_missing")
    return key


def resolve_api_key(config: LlmConfig, environ: Mapping[str, str]) -> str:
    """Return the key or raise KeyUnavailable(reason). File wins over env."""
    if config.api_key_file is not None:
        return _read_key_file(config.api_key_file)
    if config.api_key_env is None:
        raise KeyUnavailable("api_key_env_unset")
    key = environ.get(config.api_key_env, "")
    if not key:
        raise KeyUnavailable("api_key_missing")
    return key
