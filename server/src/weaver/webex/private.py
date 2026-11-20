"""Private atomic credential storage, serialized across CLI/poller processes."""

import fcntl
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class OAuthError(ValueError):
    """Fixed error codes only; never include server responses or credentials."""


def _check(fd: int, mode: int, *, directory: bool = False) -> None:
    info = os.fstat(fd)
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise OAuthError("unsafe_private_permissions")
    if not directory and info.st_nlink != 1:
        raise OAuthError("unsafe_private_links")


class PrivateFile:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def locked(self) -> Iterator[int]:
        """Walk without following links; pin parent fd for all file operations."""
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        lock = -1
        try:
            if not self.path.is_absolute():
                raise OAuthError("private_path_must_be_absolute")
            for part in self.path.parent.parts[1:]:
                if part in (".", ".."):
                    raise OAuthError("unsafe_private_path")
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
            _check(fd, 0o700, directory=True)
            lock = os.open(
                self.path.name + ".lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            _check(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield fd
        except OSError:
            raise OAuthError("private_file_unavailable") from None
        finally:
            if lock >= 0:
                os.close(lock)
            os.close(fd)

    def read(self, parent: int) -> bytes | None:
        try:
            fd = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return None
        try:
            _check(fd, 0o600)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise OAuthError("invalid_credentials")
            return data
        finally:
            os.close(fd)

    def write(self, parent: int, data: bytes) -> None:
        if len(data) > 65536:
            raise OAuthError("invalid_credentials")
        # Validate existing destination before replacing it, including symlinks.
        self.read(parent)
        temporary = ".webex-" + secrets.token_hex(16)
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            os.replace(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(fd)
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
