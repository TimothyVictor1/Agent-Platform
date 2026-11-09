"""Strict storage-key validation on top of the shared base contract."""

import re

from agentplatform.artifacts.base import validate_key
from agentplatform.errors import InvalidRequestError


def validate_storage_key(key: str) -> str:
    validate_key(key)
    # fullmatch also rejects a trailing newline (the base regex uses dollar).
    if re.fullmatch(r"[A-Za-z0-9._/-]+", key) is None:
        raise InvalidRequestError("invalid storage key characters")
    return key
