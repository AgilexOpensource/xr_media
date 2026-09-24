import asyncio
import unittest
from unittest.mock import MagicMock

from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError
from aiortc.rtp import RtcpByePacket

from xr_media import MediaServer, MediaServerConfig


class InputRestartTest(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 5)

    async def test_new_transceiver_recovers_after_remote_bye(self):
        for kind, track_factory in (("video", VideoStreamTrack), ("audio", AudioStreamTrack)):
            with self.subTest(kind=kind):
                server = MediaServer(MediaServerConfig())
                stream_id = "custom_" + kind
                server.register_browser_input(stream_id, kind, stream_id)
                server._browser_inputs[stream_id]["status"] = "live"
                received = []

                async def on_frame(received_id, frame):
                    received.append(received_id)

                getattr(server, "add_" + kind + "_listener")(on_frame)
                client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                track = track_factory()
                transceiver = client.addTransceiver(track, direction="sendonly")
                peer, context = await server._new_peer([], [
                    {"id": stream_id, "media_type": kind, "track_id": track.id}
                ])
                signal = MagicMock(closed=True)
                server._peer_signals[peer] = signal
                server._input_owners[stream_id] = signal

                async def negotiate():
                    await client.setLocalDescription(await client.createOffer())
                    await peer.setRemoteDescription(client.localDescription)
                    await peer.setLocalDescription(await peer.createAnswer())
                    await client.setRemoteDescription(peer.localDescription)

                try:
                    await negotiate()
                    await self.wait_until(lambda: len(received) >= 3)
                    receiver = peer.getReceivers()[0]
                    report = await receiver.getStats()
                    source = next(stat.ssrc for stat in report.values()
                                  if stat.type == "inbound-rtp")
                    await receiver._handle_rtcp_packet(RtcpByePacket(sources=[source]))
                    await self.wait_until(lambda: receiver.track.readyState == "ended")
                    with self.assertRaises(MediaStreamError):
                        await receiver.track.recv()
                    transceiver.sender.replaceTrack(None)
                    transceiver.direction = "inactive"
                    track.stop()
                    count_before = len(received)
                    replacement = track_factory()
                    client.addTransceiver(replacement, direction="sendonly")
                    context.values["browser.inputs"] = [
                        {"id": stream_id, "media_type": kind, "track_id": replacement.id}
                    ]
                    await negotiate()
                    await self.wait_until(lambda: len(received) >= count_before + 3)
                    self.assertEqual(receiver.track.readyState, "ended")
                    self.assertEqual(peer.getReceivers()[-1].track.readyState, "live")
                    self.assertEqual(set(received), {stream_id})
                finally:
                    await client.close()
                    await server.stop()
