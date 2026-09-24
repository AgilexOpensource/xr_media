from types import SimpleNamespace
import unittest

from xr_media.media.media import PeerMediaContext, match_browser_input


class MediaInputMappingTest(unittest.TestCase):
    def match(self, inputs, kind="video", mid="3", track_id="remote-track", index=8):
        track = SimpleNamespace(kind=kind, id=track_id)
        transceiver = SimpleNamespace(mid=mid, receiver=SimpleNamespace(track=track))
        peer = SimpleNamespace(getTransceivers=lambda: [transceiver])
        context = PeerMediaContext([])
        context.values["browser.inputs"] = inputs
        return match_browser_input(peer, track, context, index)

    def test_mid_wins_over_mismatched_track_id_and_exhausted_index(self):
        for kind in ("video", "audio"):
            entry = {"id": "custom-input", "media_type": kind,
                     "mid": "3", "track_id": "browser-track"}
            self.assertIs(self.match([entry], kind=kind), entry)

    def test_mid_wins_over_another_cards_matching_track_id(self):
        wrong = {"id": "wrong", "media_type": "video", "mid": "2", "track_id": "remote-track"}
        correct = {"id": "correct", "media_type": "video", "mid": "3", "track_id": "other"}
        self.assertIs(self.match([wrong, correct]), correct)

    def test_unknown_mid_does_not_route_to_another_card(self):
        entry = {"id": "other", "media_type": "video", "mid": "2", "track_id": "remote-track"}
        self.assertIsNone(self.match([entry], index=0))

    def test_media_kind_is_preserved(self):
        entry = {"id": "audio", "media_type": "audio", "mid": "3"}
        self.assertIsNone(self.match([entry], kind="video", index=0))

    def test_legacy_track_and_initial_order_mapping_remain_supported(self):
        entry = {"id": "legacy", "media_type": "video", "track_id": "remote-track"}
        self.assertIs(self.match([entry]), entry)
        self.assertIs(self.match([entry], track_id="different", index=0), entry)
        self.assertIsNone(self.match([entry], track_id="different", index=1))
