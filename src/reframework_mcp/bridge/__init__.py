"""Bridge protocol and transports."""

from .client import BridgeClient
from .transport import BridgeTransport, InMemoryTransport, NamedPipeTransport

__all__ = [
    "BridgeClient",
    "BridgeTransport",
    "InMemoryTransport",
    "NamedPipeTransport",
]
