import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from munch import munchify

from cogs.yt_subscription_handler import YTSubscriptionHandler
from twitchtools import PartialYoutubeUser


class FakeLog:
    debug = staticmethod(lambda *args, **kwargs: None)
    info = staticmethod(lambda *args, **kwargs: None)
    exception = staticmethod(lambda *args, **kwargs: None)


class YoutubeSubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_subscription_id_is_created_and_persisted(self):
        channel = PartialYoutubeUser("channel-1", "Legacy Channel")
        channel_data = munchify(
            {
                "display_name": "Legacy Channel",
                "secret": "secret",
                "alert_roles": {},
                "expiry_time": 0,
            }
        )
        subscription = SimpleNamespace(id="new-subscription-id")
        db = SimpleNamespace(write_yt_callback=AsyncMock())
        yapi = SimpleNamespace(
            create_subscription=AsyncMock(return_value=subscription)
        )
        handler = object.__new__(YTSubscriptionHandler)
        handler.bot = SimpleNamespace(db=db, yapi=yapi, log=FakeLog())

        await handler.yt_subscribe(channel, channel_data)

        yapi.create_subscription.assert_awaited_once_with(
            channel, "secret", None
        )
        self.assertEqual(
            "new-subscription-id", channel_data.subscription_id
        )
        self.assertGreater(channel_data.expiry_time, 0)
        db.write_yt_callback.assert_awaited_once_with(channel, channel_data)

    async def test_one_failure_does_not_stop_later_channel_renewals(self):
        failed_channel = PartialYoutubeUser("channel-1", "Broken Channel")
        valid_channel = PartialYoutubeUser("channel-2", "Valid Channel")
        failed_data = munchify(
            {
                "display_name": "Broken Channel",
                "secret": "secret-1",
                "expiry_time": 0,
            }
        )
        valid_data = munchify(
            {
                "display_name": "Valid Channel",
                "secret": "secret-2",
                "expiry_time": 0,
            }
        )
        db = SimpleNamespace(
            get_all_yt_callbacks=AsyncMock(
                return_value={
                    failed_channel: failed_data,
                    valid_channel: valid_data,
                }
            ),
            write_yt_callback=AsyncMock(),
        )
        yapi = SimpleNamespace(
            create_subscription=AsyncMock(
                side_effect=[
                    RuntimeError("temporary failure"),
                    SimpleNamespace(id="valid-subscription-id"),
                ]
            )
        )
        bot = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            wait_until_db_ready=AsyncMock(),
            db=db,
            yapi=yapi,
            log=FakeLog(),
        )
        handler = object.__new__(YTSubscriptionHandler)
        handler.bot = bot

        with patch(
            "cogs.yt_subscription_handler.sleep", new=AsyncMock()
        ):
            await handler.yt_check_expiry.coro(handler)

        self.assertEqual(2, yapi.create_subscription.await_count)
        self.assertEqual(
            "valid-subscription-id", valid_data.subscription_id
        )
        db.write_yt_callback.assert_awaited_once_with(
            valid_channel, valid_data
        )


if __name__ == "__main__":
    unittest.main()
