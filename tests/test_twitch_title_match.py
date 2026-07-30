import unittest
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from munch import munchify

from cogs.state_manager import StreamStateManager
from twitchtools import AlertOrigin, Live, PartialUser


class FakeLog:
    debug = staticmethod(lambda *args, **kwargs: None)
    info = staticmethod(lambda *args, **kwargs: None)
    warning = staticmethod(lambda *args, **kwargs: None)


class TwitchTitleMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_event_rechecks_pending_live_alerts(self):
        broadcaster = PartialUser("1", "streamer", "Streamer")
        event = SimpleNamespace(
            broadcaster=broadcaster,
            title="Now playing target game",
            game="Target Game",
            game_name="Target Game",
            game_id="20",
        )
        stream = SimpleNamespace(
            user=PartialUser("1", "streamer", "Streamer"),
            title="Old title",
            stream_title="Old title",
            game="Old Game",
            game_name="Old Game",
            game_id="10",
            origin=AlertOrigin.callback,
        )
        title_cache = munchify({"title": "Old title", "game": "Old Game"})
        channel_cache = munchify({"is_live": True, "live_alerts": []})
        db = SimpleNamespace(
            get_title_callback=AsyncMock(return_value=None),
            get_title_cache=AsyncMock(return_value=title_cache),
            write_title_cache=AsyncMock(),
            get_channel_cache=AsyncMock(return_value=channel_cache),
        )
        full_user = SimpleNamespace(
            id=1,
            username="streamer",
            name="streamer",
            display_name="Streamer",
            avatar="avatar",
        )
        tapi = SimpleNamespace(
            get_stream=AsyncMock(return_value=stream),
            get_user=AsyncMock(return_value=full_user),
        )
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            wait_until_db_ready=AsyncMock(),
            ratelimit_request=AsyncMock(),
            db=db,
            tapi=tapi,
            log=FakeLog(),
        )
        manager = StreamStateManager(bot)
        manager.get_stream_embed = lambda item, **kwargs: object()
        manager.update_alert_messages = AsyncMock()
        manager.on_streamer_online = AsyncMock()

        await manager.on_title_change(event)

        self.assertEqual(event.title, stream.title)
        self.assertEqual(event.game, stream.game)
        manager.on_streamer_online.assert_awaited_once_with(stream)

    async def test_catchup_sends_newly_matching_alert_during_cooldown(self):
        partial_user = PartialUser("1", "streamer", "Streamer")
        stream = SimpleNamespace(
            id=100,
            stream_id=100,
            user=partial_user,
            title="Now playing target game",
            game="Target Game",
            game_name="Target Game",
            type=Live.live,
            view_count=1,
            origin=AlertOrigin.catchup,
        )
        channel_cache = munchify(
            {
                "alert_cooldown": int(time()),
                "is_live": True,
                "live_channels": [],
                "live_alerts": [],
                "games": {"Target Game": 0},
                "last_update": int(time()),
                "triggered_guilds": [],
            }
        )
        alert_info = munchify(
            {
                "title_match_phrase": "target",
                "role_id": None,
                "notif_channel_id": 10,
                "mode": 1,
            }
        )
        callback = munchify(
            {
                "display_name": "Streamer",
                "alert_roles": {"1": alert_info},
            }
        )

        class FakeDb:
            async def get_channel_cache(self, unused_user):
                return channel_cache

            async def get_callback(self, unused_user):
                return callback

            async def write_callback(self, unused_user, unused_callback):
                return None

            async def write_channel_cache(self, unused_user, unused_cache):
                return None

        full_user = SimpleNamespace(
            id=1,
            username="streamer",
            name="streamer",
            display_name="Streamer",
            avatar="avatar",
        )
        alert_message = SimpleNamespace(
            id=50, channel=SimpleNamespace(id=10)
        )
        alert_channel = SimpleNamespace(send=AsyncMock(return_value=alert_message))
        guild = SimpleNamespace(
            id=1,
            name="Guild",
            default_role=SimpleNamespace(),
            get_role=lambda unused_role_id: None,
        )
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            wait_until_db_ready=AsyncMock(),
            db=FakeDb(),
            tapi=SimpleNamespace(get_user=AsyncMock(return_value=full_user)),
            viewer_milestones_minimum=100,
            viewer_milestones_interval=100,
            get_guild=lambda guild_id: guild if guild_id == 1 else None,
            get_channel=lambda channel_id: (
                alert_channel if channel_id == 10 else None
            ),
            log=FakeLog(),
            user=SimpleNamespace(),
        )
        manager = StreamStateManager(bot)
        manager.get_stream_embed = lambda item, **kwargs: object()

        await manager.on_streamer_online(stream)

        alert_channel.send.assert_awaited_once()
        self.assertEqual(["1"], channel_cache.triggered_guilds)
        self.assertEqual(
            [{"channel": 10, "message": 50}], channel_cache.live_alerts
        )


if __name__ == "__main__":
    unittest.main()
