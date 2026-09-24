import asyncio
from collections import Counter
import unittest
import time
from unittest.mock import patch
from uuid import uuid4

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiortc import (
    AudioStreamTrack,
    RTCBundlePolicy,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
import numpy as np

from xr_media import AudioFrame, MediaServer, MediaServerConfig, VideoFrame


class MediaMatrixTest(unittest.IsolatedAsyncioTestCase):
    async def test_mid_routing_survives_track_id_mismatch_and_restart(self):
        for counts in ((2, 1, 3, 2), (1, 1, 0, 2)):
            with self.subTest(counts=counts):
                await self.check_matrix(*counts, mismatched_track_ids=True)

    async def test_mdns_direction_and_card_count_matrix(self):
        for counts in ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0),
                       (0, 0, 0, 1), (2, 2, 2, 2), (1, 2, 3, 1)):
            with self.subTest(counts=counts):
                state = {"ready": False, "addresses": {}, "calls": 0}

                async def resolve(protocol, hostname, timeout=1.0):
                    self.assertTrue(state["ready"], "mDNS queried before answer was applied")
                    state["calls"] += 1
                    return state["addresses"][hostname]

                with patch("aioice.mdns.MDnsProtocol.resolve", new=resolve):
                    await self.check_matrix(*counts, mdns_state=state)
                self.assertGreater(state["calls"], 0)

    async def test_direction_and_card_count_matrix(self):
        for counts in ((1, 0, 0, 0), (0, 1, 0, 0), (2, 2, 2, 2), (1, 2, 3, 1)):
            with self.subTest(counts=counts):
                await self.check_matrix(*counts)

    async def check_matrix(self, video_outputs, audio_outputs, video_inputs, audio_inputs,
                           mdns_state=None, mismatched_track_ids=False):
        server = MediaServer(MediaServerConfig())
        output_ids = []
        for index in range(video_outputs):
            stream_id = "video_out_" + str(index)
            output_ids.append(stream_id)
            server.register_video_stream(stream_id, stream_id)
        for index in range(audio_outputs):
            stream_id = "audio_out_" + str(index)
            output_ids.append(stream_id)
            server.register_audio_stream(stream_id, stream_id)
        input_counts = Counter()
        received = Counter()
        consumers = []

        async def on_input(stream_id, frame):
            input_counts[stream_id] += 1

        server.add_video_listener(on_input)
        server.add_audio_listener(on_input)
        client = RTCPeerConnection(RTCConfiguration(iceServers=[], bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
        for kind, count in (("video", video_outputs), ("audio", audio_outputs)):
            for index in range(count):
                client.addTransceiver(kind, direction="recvonly")
        inputs = []
        for kind, count, factory in (("video", video_inputs, VideoStreamTrack),
                                     ("audio", audio_inputs, AudioStreamTrack)):
            for index in range(count):
                stream_id = kind + "_in_" + str(index)
                server.register_browser_input(stream_id, kind, stream_id)
                track = factory()
                transceiver = client.addTransceiver(track, direction="sendonly")
                inputs.append({"id": stream_id, "media_type": kind,
                               "track_id": track.id, "transceiver": transceiver})

        async def consume(track):
            while True:
                await track.recv()
                received[track.id] += 1

        @client.on("track")
        def on_track(track):
            consumers.append(asyncio.create_task(consume(track)))

        async def produce():
            while True:
                for index in range(video_outputs):
                    image = np.full((49, 65, 3), 30 + index, dtype=np.uint8)
                    server.push_video_frame(VideoFrame(image, 65, 49, "bgr24", time.monotonic_ns()),
                                      "video_out_" + str(index))
                for index in range(audio_outputs):
                    server.push_audio_frame(AudioFrame(np.zeros(320, dtype=np.int16), 16000, 1, time.monotonic_ns()),
                                      "audio_out_" + str(index))
                await asyncio.sleep(0.02)

        async def wait_until(condition):
            async def wait():
                while not condition():
                    await asyncio.sleep(0.01)
            try:
                await asyncio.wait_for(wait(), 8)
            except asyncio.TimeoutError:
                self.fail(str({
                    "counts": (video_outputs, audio_outputs, video_inputs, audio_inputs),
                    "client": (client.connectionState, client.iceConnectionState),
                    "peers": [(peer.connectionState, peer.iceConnectionState)
                              for peer in server._peers],
                    "transceivers": [
                        [
                            (
                                item.mid, item.kind, item.currentDirection,
                                item.sender.transport.state,
                                item.sender.track.id if item.sender.track else None,
                            )
                            for item in peer.getTransceivers()
                        ]
                        for peer in server._peers
                    ],
                    "received": dict(received), "inputs": dict(input_counts),
                    "server": vars(server.get_stats()),
                    "producer_error": repr(producer.exception()) if producer.done() else None,
                    "consumer_errors": [repr(task.exception()) for task in consumers if task.done()],
                }))

        app = web.Application()
        app.router.add_get("/signal", server._websocket)
        http = TestClient(TestServer(app))
        await http.start_server()
        producer = asyncio.create_task(produce())
        try:
            async with http.ws_connect('/signal') as socket:
                for entry in inputs:
                    await socket.send_json({"type": "input.control", "stream": entry["id"],
                                            "action": "start", "status": "live"})

                async def negotiate():
                    await client.setLocalDescription(await client.createOffer())
                    remote_sdp = client.localDescription.sdp
                    if mdns_state is not None:
                        mdns_state["ready"] = False
                        calls_before = mdns_state["calls"]
                        lines = []
                        names = {}
                        for line in remote_sdp.splitlines():
                            if line.startswith("a=candidate:"):
                                fields = line.split()
                                address = fields[4]
                                name = names.setdefault(address, str(uuid4()) + ".local")
                                mdns_state["addresses"][name] = address
                                fields[4] = name
                                line = " ".join(fields)
                            lines.append(line)
                        remote_sdp = "\r\n".join(lines) + "\r\n"
                    await socket.send_json({
                        "type": "offer", "sdp": remote_sdp,
                        "defer_mdns": mdns_state is not None,
                        "streams": output_ids,
                        "inputs": [{
                            "id": entry["id"], "media_type": entry["media_type"],
                            "track_id": ("browser-" if mismatched_track_ids else "") + entry["track_id"],
                            **({"mid": entry["transceiver"].mid} if mismatched_track_ids else {}),
                        } for entry in inputs],
                    })
                    answer = await asyncio.wait_for(socket.receive_json(), 8)
                    self.assertEqual([entry["id"] for entry in answer["streams"]], output_ids)
                    self.assertEqual(len({entry["mid"] for entry in answer["streams"]}), len(output_ids))
                    await client.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
                    if mdns_state is not None:
                        self.assertEqual(mdns_state["calls"], calls_before)
                        await socket.send_json({"type": "answer.applied", "offer_id": -1})
                        await asyncio.sleep(0.03)
                        self.assertEqual(mdns_state["calls"], calls_before)
                        mdns_state["ready"] = True
                        await socket.send_json({"type": "answer.applied", "offer_id": answer["ice_deferred"]})
                    mapping = {entry["mid"]: entry["id"] for entry in answer["streams"]}
                    return {mapping[transceiver.mid]: transceiver.receiver.track.id
                            for transceiver in client.getTransceivers() if transceiver.mid in mapping}

                mapping = await negotiate()
                await wait_until(lambda: all(received[track_id] >= 3 for track_id in mapping.values())
                                 and all(input_counts[entry["id"]] >= 3 for entry in inputs))
                if inputs:
                    entry = inputs[0]
                    old = entry["transceiver"]
                    await socket.send_json({"type": "input.control", "stream": entry["id"],
                                            "action": "stop", "status": "idle"})
                    old.sender.replaceTrack(None)
                    old.direction = "inactive"
                    replacement = VideoStreamTrack() if entry["media_type"] == "video" else AudioStreamTrack()
                    entry["track_id"] = replacement.id
                    entry["transceiver"] = client.addTransceiver(replacement, direction="sendonly")
                    before_inputs, before_outputs = input_counts.copy(), received.copy()
                    self.assertEqual(await negotiate(), mapping)
                    await socket.send_json({"type": "input.control", "stream": entry["id"],
                                            "action": "start", "status": "live"})
                    await wait_until(lambda: all(received[track_id] >= before_outputs[track_id] + 3
                                                for track_id in mapping.values())
                                     and all(input_counts[item["id"]] >= before_inputs[item["id"]] + 3
                                             for item in inputs))
                self.assertEqual(client.connectionState, "connected")
        finally:
            producer.cancel()
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(producer, *consumers, return_exceptions=True)
            await client.close()
            await http.close()
            await server.stop()
