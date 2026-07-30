from asyncio import sleep
from datetime import datetime
from typing import TYPE_CHECKING

from disnake.ext import commands, tasks

from twitchtools import PartialYoutubeUser, YoutubeCallback

if TYPE_CHECKING:
    from twitchtools import TwitchCallBackBot

LEASE_SECONDS = 828000


class YTSubscriptionHandler(commands.Cog, name="Youtube Subscription Handler"):
    def __init__(self, bot):
        self.bot: TwitchCallBackBot = bot
        super().__init__()
        self.yt_check_expiry.start()

    def cog_unload(self):
        self.yt_check_expiry.cancel()

    @tasks.loop(hours=24)
    async def yt_check_expiry(self):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        self.bot.log.debug("[Youtube] Resubscribing youtube callbacks")
        for channel, channel_data in (await self.bot.db.get_all_yt_callbacks()).items():
            expiry_time = channel_data.get("expiry_time", 0)
            if expiry_time < datetime.utcnow().timestamp():
                self.bot.log.info(
                    f"[Youtube] Resubscribing YT channel {channel.display_name}")
                try:
                    await self.yt_subscribe(channel, channel_data)
                except Exception:
                    # A malformed callback or transient request failure must
                    # not stop renewals for every channel after it.
                    self.bot.log.exception(
                        f"[Youtube] Failed to resubscribe YT channel "
                        f"{channel.display_name}"
                    )
                await sleep(1)

    async def yt_subscribe(self, channel: PartialYoutubeUser, channel_data: YoutubeCallback):
        secret = channel_data.get("secret")
        if not secret:
            # Persist first because the confirmation request reads the secret
            # from the database while create_subscription is still waiting.
            secret = self.bot.random_string_generator(21)
            channel_data["secret"] = secret
            await self.bot.db.write_yt_callback(channel, channel_data)

        subscription = await self.bot.yapi.create_subscription(
            channel,
            secret,
            channel_data.get("subscription_id"),
        )
        # Minus a day plus 100 seconds, ensures that the subscription never expires
        timestamp = datetime.utcnow().timestamp() + (LEASE_SECONDS - 86500)
        channel_data["subscription_id"] = subscription.id
        channel_data["expiry_time"] = int(timestamp)
        await self.bot.db.write_yt_callback(channel, channel_data)


def setup(bot):
    bot.add_cog(YTSubscriptionHandler(bot))
