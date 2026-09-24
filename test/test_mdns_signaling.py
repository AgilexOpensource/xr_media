import unittest

from xr_media.media.server import _defer_mdns_candidates


class MdnsSignalingTest(unittest.TestCase):
    def offer(self, host="camera.local", complete=True):
        lines = ["v=0", "o=- 1 1 IN IP4 127.0.0.1", "s=-", "t=0 0",
                 "a=group:BUNDLE custom-video custom-audio"]
        for kind, mid, payload, codec in (("video", "custom-video", 96, "VP8/90000"),
                                           ("audio", "custom-audio", 111, "opus/48000/2")):
            lines.extend([
                f"m={kind} 9 UDP/TLS/RTP/SAVPF {payload}", "c=IN IP4 0.0.0.0",
                f"a=mid:{mid}", "a=recvonly", f"a=rtpmap:{payload} {codec}",
                f"a=candidate:foundation 1 udp 2122260223 {host} 50000 typ host",
                "a=candidate:other 1 udp 2122260222 10.0.0.2 50002 typ host",
            ])
            if complete:
                lines.append("a=end-of-candidates")
        return "\r\n".join(lines) + "\r\n"

    def test_plain_ip_offer_is_unchanged(self):
        offer = self.offer(host="10.0.0.1")
        self.assertEqual(_defer_mdns_candidates(offer), (offer, None))

    def test_mdns_defers_mixed_candidates_preserving_mids(self):
        offer = self.offer()
        stripped, (candidates, complete) = _defer_mdns_candidates(offer)
        self.assertNotIn("a=candidate:", stripped)
        self.assertNotIn("a=end-of-candidates", stripped)
        self.assertEqual(stripped, "".join(line for line in offer.splitlines(keepends=True)
                         if not line.startswith(("a=candidate:", "a=end-of-candidates"))))
        self.assertEqual([(entry.sdpMid, entry.sdpMLineIndex) for entry in candidates],
                         [("custom-video", 0), ("custom-video", 0),
                          ("custom-audio", 1), ("custom-audio", 1)])
        self.assertEqual([entry.ip for entry in candidates],
                         ["camera.local", "10.0.0.2", "camera.local", "10.0.0.2"])
        self.assertTrue(complete)

    def test_incomplete_gathering_does_not_signal_end(self):
        _, (_, complete) = _defer_mdns_candidates(self.offer(complete=False))
        self.assertFalse(complete)
