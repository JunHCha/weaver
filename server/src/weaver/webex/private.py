"""Compatibility names for the shared private credential file helper."""

from weaver.private import PrivateFile
from weaver.private import PrivateFileError as OAuthError

__all__ = ["OAuthError", "PrivateFile"]
