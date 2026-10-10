from __future__ import annotations

import socket
from urllib.parse import urlparse

from ..domain import DatasetManifest, SourceType


def default_network_check(manifest: DatasetManifest, hf_endpoint: str = "https://huggingface.co",
                          timeout: float = 2.0) -> bool:
    """True if the dataset's source host is reachable (TCP connect). Local sources need no network."""
    if manifest.source_type in (SourceType.LOCAL_PATH, SourceType.USER_UPLOAD):
        return True
    uri = hf_endpoint if manifest.source_type == SourceType.HUGGINGFACE else manifest.source_uri
    u = urlparse(uri if "://" in uri else f"https://{uri}")
    if not u.hostname:
        return False
    port = u.port or (80 if u.scheme == "http" else 443)
    try:
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False
