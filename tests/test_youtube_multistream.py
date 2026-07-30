import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.catchup import Catchup
from cogs.state_manager import StreamStateManager
from twitchtools import AlertOrigin, PartialYoutubeUser, YoutubeVideoType
from twitchtools.api_youtube import http_youtube
from twitchtools.youtube_cache import (get_youtube_stream_states,
                                       sync_youtube_channel_cache)


class FakeLog:
    debug = staticmethod(lambda *args, **kwargs: None)
    info = staticmethod(lambda *args, **kwargs: None)


class YoutubeCacheTests(unittest.TestCase):
    def test_legacy_live_cache_is_migrated_to_video_state(self):
        cache = {
            "channel_id": "channel-1",
            "video_id": "video-1",
            "is_live": True,
            "live_channels": [10],
            "live_alerts": [{"channel": 20, "message": 30}],
            "triggered_guilds": ["40"],
        }

        streams = get_youtube_stream_states(cache)
        sync_youtube_channel_cache(cache, streams)

        self.assertEqual(["video-1"], list(cache["streams"]))
        self.assertEqual([10], cache["streams"]["video-1"]["live_channels"])
        self.assertTrue(cache["is_live"])
        self.assertNotIn("video_id", cache)
        self.assertNotIn("live_alerts", cache)


class YoutubeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_are_videos_live_returns_every_live_video_per_channel(self):
        channel = PartialYoutubeUser("channel-1", "Channel")
        response = SimpleNamespace(
            json=AsyncMock(
                return_value={
                    "items": [
                        {"id": "video-1"},
                        {"id": "video-2"},
                        {"id": "video-ended"},
                    ]
                }
            )
        )
        api = object.__new__(http_youtube)
        api.base = "https://example.test"
        api._request = AsyncMock(return_value=response)
        api.get_video_type = lambda item: YoutubeVideoType.stream
        api.has_stream_ended = lambda item: item["id"] == "video-ended"

        result = await api.are_videos_live(
            {channel: ["video-1", "video-2", "video-ended"]}
        )

        self.assertEqual(["video-1", "video-2"], result[channel])


class YoutubeCatchupTests(unittest.IsolatedAsyncioTestCase):
    async def test_catchup_discovers_another_stream_while_channel_is_live(self):
        channel = PartialYoutubeUser("channel-1", "Channel")
        callback = SimpleNamespace(display_name="Channel")
        cache = {
            "is_live": True,
            "streams": {
                "video-1": {
                    "video_id": "video-1",
                    "live_alerts": [],
                    "live_channels": [],
                }
            },
        }

        db = SimpleNamespace(
            get_yt_channel_cache=AsyncMock(return_value=cache),
            write_yt_callback=AsyncMock(),
        )

        async def get_stream(video_id, origin):
            return SimpleNamespace(
                id=video_id,
                user=SimpleNamespace(
                    id="channel-1", display_name="Channel"
                ),
                origin=origin,
            )

        yapi = SimpleNamespace(
            get_recent_video_ids=AsyncMock(
                return_value={channel: ["video-1", "video-2"]}
            ),
            are_videos_live=AsyncMock(
                return_value={channel: ["video-1", "video-2"]}
            ),
            have_videos_ended=AsyncMock(return_value=[]),
            get_stream=get_stream,
        )
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            wait_until_db_ready=AsyncMock(),
            db=db,
            yapi=yapi,
            queue=asyncio.Queue(),
            log=FakeLog(),
        )
        catchup = object.__new__(Catchup)
        catchup.bot = bot

        await catchup.youtube_catchup({channel: callback})

        queued_ids = [
            bot.queue.get_nowait().id,
            bot.queue.get_nowait().id,
        ]
        self.assertEqual(["video-1", "video-2"], queued_ids)


class YoutubeStateManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_video_gets_its_own_alert_state(self):
        channel = PartialYoutubeUser("channel-1", "Channel")
        cache = {}
        callback = SimpleNamespace(display_name="Channel")

        class FakeDb:
            async def get_yt_channel_cache(self, unused_channel):
                return cache

            async def get_yt_callback(self, unused_channel):
                return callback

            async def write_yt_callback(self, unused_channel, unused_callback):
                return None

            async def write_yt_channel_cache(self, unused_channel, data):
                return None

        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            wait_until_db_ready=AsyncMock(),
            db=FakeDb(),
            yapi=SimpleNamespace(
                get_user=AsyncMock(
                    side_effect=lambda user: SimpleNamespace(
                        id=user.id,
                        display_name=user.display_name,
                        avatar_url="avatar",
                    )
                )
            ),
            log=FakeLog(),
        )
        manager = StreamStateManager(bot)
        sent_video_ids = []

        async def send_alert(video, embed, unused_callback, stream_cache):
            sent_video_ids.append(video.id)
            index = len(sent_video_ids)
            return [100 + index], [{"channel": 200, "message": index}], ["300"]

        manager.send_live_alerts_and_channels = send_alert
        manager.get_stream_embed = lambda video: object()

        def make_video(video_id):
            return SimpleNamespace(
                id=video_id,
                channel=channel,
                user=PartialYoutubeUser("channel-1", "Channel"),
                title=f"Title {video_id}",
                type=YoutubeVideoType.stream,
                origin=AlertOrigin.callback,
            )

        await manager.on_youtube_streamer_online(make_video("video-1"))
        await manager.on_youtube_streamer_online(make_video("video-2"))
        await manager.on_youtube_streamer_online(make_video("video-1"))

        self.assertEqual(["video-1", "video-2"], sent_video_ids)
        self.assertEqual({"video-1", "video-2"}, set(cache["streams"]))
        self.assertTrue(cache["is_live"])

    async def test_ending_one_video_keeps_the_other_video_live(self):
        channel = PartialYoutubeUser(
            "channel-1",
            "Channel",
            origin=AlertOrigin.catchup,
            video_id="video-1",
        )
        cache = {
            "is_live": True,
            "streams": {
                "video-1": {
                    "video_id": "video-1",
                    "live_channels": [100],
                    "live_alerts": [],
                },
                "video-2": {
                    "video_id": "video-2",
                    "live_channels": [100],
                    "live_alerts": [],
                },
            },
        }
        callback = SimpleNamespace(display_name="Channel")

        class FakeDb:
            async def get_yt_channel_cache(self, unused_channel):
                return cache

            async def get_yt_callback(self, unused_channel):
                return callback

            async def write_yt_channel_cache(self, unused_channel, data):
                return None

        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            wait_until_db_ready=AsyncMock(),
            db=FakeDb(),
            log=FakeLog(),
        )
        manager = StreamStateManager(bot)
        offline_calls = []

        async def set_channels(unused_callback, stream_state, active_ids):
            offline_calls.append((stream_state["video_id"], active_ids))

        manager.set_channels_offline = set_channels
        manager.set_youtube_alerts_offline = AsyncMock()

        await manager.on_youtube_streamer_offline(channel)

        self.assertEqual({"video-2"}, set(cache["streams"]))
        self.assertTrue(cache["is_live"])
        self.assertEqual([("video-1", {100})], offline_calls)


if __name__ == "__main__":
    unittest.main()
