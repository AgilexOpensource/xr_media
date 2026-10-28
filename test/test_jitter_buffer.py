import asyncio
import json

import pytest

from xr_media import MediaServer, MediaServerConfig


@pytest.mark.parametrize("value", [-1, 4001])
def test_rejects_out_of_range_target(value):
    with pytest.raises(ValueError, match="video_jitter_buffer_target_ms"):
        MediaServerConfig(video_jitter_buffer_target_ms=value)


@pytest.mark.parametrize("value", [None, 0, 4000])
def test_stream_list_exposes_target(value):
    server = MediaServer(MediaServerConfig(video_jitter_buffer_target_ms=value))
    response = asyncio.run(server._stream_list(None))
    assert json.loads(response.body)["client"] == {"video_jitter_buffer_target_ms": value}
