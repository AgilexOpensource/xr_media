"""Shared interface for independently implemented media modules."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiortc import MediaStreamTrack, RTCPeerConnection


@dataclass
class PeerMediaContext:
    requested_streams: List[str]
    values: Dict[str, Any] = field(default_factory=dict)


def match_browser_input(
    peer: RTCPeerConnection,
    track: MediaStreamTrack,
    context: PeerMediaContext,
    incoming_index: int,
) -> Optional[dict]:
    configured = [item for item in context.values.get("browser.inputs", [])
                  if item.get("media_type") == track.kind]
    mid = next((item.mid for item in peer.getTransceivers()
                if item.receiver.track is track), None)
    if mid is not None and any(item.get("mid") is not None for item in configured):
        return next((item for item in configured if item.get("mid") == mid), None)
    matched = next((item for item in configured if item.get("track_id") == track.id), None)
    if matched is not None:
        return matched
    return configured[incoming_index] if incoming_index < len(configured) else None


class MediaModule(ABC):
    """One media kind that can attach its tracks to a peer connection."""

    @property
    @abstractmethod
    def kind(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def attach(self, peer: RTCPeerConnection, context: PeerMediaContext) -> None:
        raise NotImplementedError

    async def detach(self, peer: RTCPeerConnection) -> None:
        del peer
