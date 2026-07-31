from datetime import timedelta
from time import time
from math import floor
from typing import TYPE_CHECKING, Union

import disnake
import aiofiles
from aiohttp import client_exceptions
from yaml import load, YAMLError
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader
from dateutil import parser, tz
from disnake.ext import commands
from disnake.utils import utcnow

from twitchtools import (AlertOrigin, Callback, KickCallback, KickStream,
                         KickUser, PartialKickUser, PartialUser,
                         PartialYoutubeUser, Stream, TitleEvent, User,
                         YoutubeCallback, YoutubeUser, YoutubeVideo,
                         YoutubeVideoType, human_timedelta)
from twitchtools.enums import (ChannelCache, KickChannelCache,
                               YoutubeChannelCache)
from twitchtools.youtube_cache import (get_youtube_stream_states,
                                       sync_youtube_channel_cache)

if TYPE_CHECKING:
    from main import TwitchCallBackBot

TWITCH_PURPLE = 9520895  # Hex #9146FF
YOUTUBE_RED = 16711680  # Hex FF0000
KICK_GREEN = 5504024  # Hex #53FC18
LEFT_TO_RIGHT_MARK = "\u200e"
LTR_ISOLATE = "\u2066"
FIRST_STRONG_ISOLATE = "\u2068"
POP_DIRECTIONAL_ISOLATE = "\u2069"


class StreamStateManager(commands.Cog):
    def __init__(self, bot):
        self.bot: TwitchCallBackBot = bot
        super().__init__()
        self.ignore_cooldowns: bool = False  # Used for debugging/development
        self.footer_msg: str = "POGGIES"

    async def on_title_change(self, event: TitleEvent):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        title_callback = await self.bot.db.get_title_callback(event.broadcaster)
        title_cache = await self.bot.db.get_title_cache(event.broadcaster)
        old_title = title_cache.title  # Get cached information for streamer, or none
        old_game = title_cache.game

        updated = []  # Quick way to make the dynamic embed title
        if event.title != old_title:
            updated.append("title")
        if event.game != old_game:
            updated.append("game")

        if updated == []:  # If for some reason neither the title or game updated, just ignore
            return

        title_cache.title = event.title
        title_cache.game = event.game

        await self.bot.db.write_title_cache(event.broadcaster, title_cache)

        if stream := await self.bot.tapi.get_stream(event.broadcaster.username, origin=AlertOrigin.callback):
            return await self.title_change_update_alerts(event, stream, old_game)

        if not title_callback:
            return

        user = await self.bot.tapi.get_user(user_id=event.broadcaster.id)
        # Create embed for discord
        embed = disnake.Embed(
            url=f"https://twitch.tv/{event.broadcaster.username}", colour=TWITCH_PURPLE, timestamp=utcnow())
        if event.title != old_title:
            embed.add_field(name="Old Title", value=self.isolate_bidi_text(
                old_title), inline=True)
            embed.add_field(name="New Title", value=self.isolate_bidi_text(
                event.title), inline=True)
        if event.game != old_game:
            embed.add_field(name="Old Game", value=self.isolate_bidi_text(
                old_game), inline=True)
            embed.add_field(name="New Game", value=self.isolate_bidi_text(
                event.game), inline=True)
        embed.set_author(name=self.isolate_ltr_text(
            f"{self.isolate_bidi_text(event.broadcaster.display_name)} "
            f"updated their {' and '.join(updated)}!"),
                         url=f"https://twitch.tv/{event.broadcaster.username}", icon_url=user.avatar)
        embed.set_footer(text=self.footer_msg)

        self.bot.log.info(
            f"[Twitch] {event.broadcaster.username} => TITLE UPDATE")

        # Send embed to each defined channel
        for alert_info in title_callback.alert_roles.values():
            if c := self.bot.get_channel(alert_info.notif_channel_id):
                if alert_info.role_id is None:
                    role_mention = ""
                elif alert_info.role_id == "everyone":
                    role_mention = "@everyone"
                else:
                    role_mention = f"<@&{alert_info.role_id}>"
                try:
                    await c.send(f"{role_mention}", embed=embed)
                except disnake.Forbidden:
                    pass
                except disnake.HTTPException:
                    pass

    async def on_streamer_offline(self, streamer: Union[User, PartialUser]):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        channel_cache = await self.bot.db.get_channel_cache(streamer)
        callback = await self.bot.db.get_callback(streamer)

        if not callback:
            return
        if not self.is_live(channel_cache):
            return

        self.bot.log.info(
            f"[Twitch]{self.is_catchup(streamer)} {streamer.display_name} => OFFLINE")

        await self.set_channels_offline(callback, channel_cache)
        await self.set_twitch_alerts_offline(streamer, callback, channel_cache)

        channel_cache.pop("live_channels", None)
        if channel_cache.get("live_alerts", []) != []:
            channel_cache.reusable_alerts = channel_cache.live_alerts
        channel_cache.pop("live_alerts", None)
        channel_cache.pop("stream_id", None)
        channel_cache.is_live = False
        channel_cache.pop("games", None)
        channel_cache.pop("last_update", None)
        channel_cache.pop("viewer_milestone", None)
        channel_cache.pop("triggered_guilds", None)

        # Update cache
        await self.bot.db.write_channel_cache(streamer, channel_cache)

    async def on_youtube_streamer_offline(self, channel: Union[YoutubeUser, PartialYoutubeUser]):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        # Due to lack of an event, only catchup will trigger this
        channel_cache = await self.bot.db.get_yt_channel_cache(channel)
        callback = await self.bot.db.get_yt_callback(channel)
        if not callback:
            return
        if not self.is_live(channel_cache):
            return

        streams = get_youtube_stream_states(channel_cache)
        requested_video_id = getattr(channel, "video_id", None)
        if requested_video_id is not None:
            video_ids = (
                [requested_video_id] if requested_video_id in streams else []
            )
        else:
            # Backwards compatibility for an offline event without a video ID.
            video_ids = list(streams.keys())

        remaining_streams = {
            video_id: stream_state
            for video_id, stream_state in streams.items()
            if video_id not in video_ids
        }
        active_channel_ids = {
            channel_id
            for stream_state in remaining_streams.values()
            for channel_id in stream_state.get("live_channels", [])
        }

        for video_id in video_ids:
            stream_state = streams[video_id]
            self.bot.log.info(
                f"[Youtube]{self.is_catchup(channel)} "
                f"{channel.display_name} ({video_id}) => OFFLINE"
            )
            await self.set_channels_offline(
                callback, stream_state, active_channel_ids
            )
            await self.set_youtube_alerts_offline(
                channel, callback, stream_state
            )
            streams.pop(video_id, None)

        sync_youtube_channel_cache(channel_cache, streams)
        await self.bot.db.write_yt_channel_cache(channel, channel_cache)

    async def on_kick_streamer_offline(
        self, channel: Union[KickUser, PartialKickUser]
    ):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        channel_cache = await self.bot.db.get_kick_channel_cache(channel)
        callback = await self.bot.db.get_kick_callback(channel)
        if not callback or not self.is_live(channel_cache):
            return

        self.bot.log.info(
            f"[Kick]{self.is_catchup(channel)} "
            f"{channel.display_name} => OFFLINE"
        )
        await self.set_channels_offline(callback, channel_cache)
        await self.set_kick_alerts_offline(channel, callback, channel_cache)

        channel_cache.pop("live_channels", None)
        if channel_cache.get("live_alerts", []) != []:
            channel_cache.reusable_alerts = channel_cache.live_alerts
        channel_cache.pop("live_alerts", None)
        channel_cache.pop("stream_id", None)
        channel_cache.is_live = False
        channel_cache.pop("games", None)
        channel_cache.pop("last_update", None)
        channel_cache.pop("triggered_guilds", None)
        channel_cache.pop("title", None)
        channel_cache.pop("game", None)
        await self.bot.db.write_kick_channel_cache(channel, channel_cache)

    async def on_streamer_online(self, stream: Stream):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()

        channel_cache = await self.bot.db.get_channel_cache(stream.user)
        callback = await self.bot.db.get_callback(stream.user)
        if not callback:
            return
        on_cooldown = self.on_cooldown(channel_cache.get("alert_cooldown", 0))

        # Do not re-run this function is the streamer is already live
        if self.is_live(channel_cache):
            if stream.origin == AlertOrigin.callback:
                self.bot.log.info(
                    f"[Twitch] Callback received for "
                    f"{stream.user.display_name} while live, checking "
                    "pending title-matched alerts")
            elif stream.origin == AlertOrigin.catchup:
                await self.viewer_count_milestones(stream, callback, channel_cache)
            # Remove this return so that title phrase matching can run
            # return

        else:
            if on_cooldown:  # There is a 10 minute cooldown between alerts, but live channels will still be created
                self.bot.log.info(
                    f"[Twitch] Notification cooldown active for {stream.user.display_name}, restoring old channels/messages")

            self.bot.log.info(
                f"[Twitch]{self.is_catchup(stream)} {stream.user.display_name} => ONLINE")

        # If no guilds left to recheck, just return
        if [k for k in callback.alert_roles.keys() if k not in channel_cache.get("triggered_guilds", [])] == []:
            return

        # Update cached display name
        if callback.display_name != stream.user.display_name:
            callback.display_name = stream.user.display_name
            await self.bot.db.write_callback(stream.user, callback)

        # Create embed message
        stream.user = await self.bot.tapi.get_user(user=stream.user)
        embed = self.get_stream_embed(stream)

        live_channels, live_alerts, triggered_guilds = await self.send_live_alerts_and_channels(stream, embed, callback, channel_cache)

        if not channel_cache.get("is_live", False):
            # Finally, combine all data into channel cache, and update the file
            channel_cache = {
                "alert_cooldown": int(time()),
                "user_login": stream.user.username,
                "stream_id": stream.stream_id,
                "is_live": True,
                "live_channels": live_channels,
                "live_alerts": live_alerts,
                "last_update": int(time()),
                "games": {stream.game_name: 0},
                "triggered_guilds": triggered_guilds
            }
        else:
            channel_cache["triggered_guilds"] = list(
                set(triggered_guilds + channel_cache["triggered_guilds"]))
            channel_cache["live_channels"] = list(
                set(live_channels + channel_cache["live_channels"]))
            msgs = [a["message"] for a in channel_cache["live_alerts"]]
            for alert in live_alerts:
                if alert["message"] not in msgs:
                    channel_cache["live_alerts"].append(alert)

        # await write_channel_cache(channel_cache)
        await self.bot.db.write_channel_cache(stream.user, channel_cache)

    async def on_youtube_streamer_online(self, video: YoutubeVideo):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()

        # Ignore these for now
        if video.type in [YoutubeVideoType.scheduled_premiere, YoutubeVideoType.scheduled_stream]:
            return

        channel_cache = await self.bot.db.get_yt_channel_cache(video.channel)
        callback = await self.bot.db.get_yt_callback(video.channel)
        streams = get_youtube_stream_states(channel_cache)
        stream_cache = streams.get(video.id)

        # Repeated notifications are ignored per video, not per channel. A
        # different video on the same channel is a separate live alert.
        if stream_cache is not None:
            if video.origin == AlertOrigin.catchup:
                await self.update_youtube_title(video, stream_cache)
            elif video.origin == AlertOrigin.callback:
                self.bot.log.info(
                    f"[Youtube] Callback received for "
                    f"{video.user.display_name} video {video.id} while live, "
                    "ignoring"
                )
            sync_youtube_channel_cache(channel_cache, streams)
            await self.bot.db.write_yt_channel_cache(
                video.channel, channel_cache
            )
            return

        self.bot.log.info(
            f"[Youtube{' Premiere' if video.type == YoutubeVideoType.premiere else ' Stream'}]{self.is_catchup(video)} {video.user.display_name} => ONLINE")

        # Update cached display name
        if callback.display_name != video.user.display_name:
            callback.display_name = video.user.display_name
            await self.bot.db.write_yt_callback(video.user, callback)

        # Create embed message
        video.user = await self.bot.yapi.get_user(video.user)
        embed = self.get_stream_embed(video)

        live_channels, live_alerts, triggered_guilds = await self.send_live_alerts_and_channels(
            video, embed, callback, {}
        )

        streams[video.id] = {
            "video_id": video.id,
            "title": video.title,
            "alert_cooldown": int(time()),
            "live_channels": live_channels,
            "live_alerts": live_alerts,
            "last_update": int(time()),
            "triggered_guilds": triggered_guilds,
        }
        channel_cache["channel_id"] = video.channel.id
        sync_youtube_channel_cache(channel_cache, streams)
        await self.bot.db.write_yt_channel_cache(video.user, channel_cache)

    async def on_kick_streamer_online(self, stream: KickStream):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()

        channel_cache = await self.bot.db.get_kick_channel_cache(stream.user)
        callback = await self.bot.db.get_kick_callback(stream.user)
        if not callback:
            return

        if (
            self.is_live(channel_cache)
            and channel_cache.get("stream_id") != stream.stream_id
        ):
            # An offline transition was missed and a new broadcast has begun.
            await self.on_kick_streamer_offline(
                PartialKickUser(
                    stream.user.id,
                    stream.user.slug,
                    stream.user.display_name,
                    stream.user.profile_picture,
                    origin=stream.origin,
                )
            )
            channel_cache = await self.bot.db.get_kick_channel_cache(stream.user)

        was_live = self.is_live(channel_cache)
        on_cooldown = self.on_cooldown(channel_cache.get("alert_cooldown", 0))

        if was_live:
            metadata_changed = (
                channel_cache.get("title") != stream.title
                or channel_cache.get("game") != stream.game_name
            )
            if metadata_changed:
                old_game = channel_cache.get("game")
                if old_game and channel_cache.get("games") is not None:
                    channel_cache.games[old_game] = (
                        channel_cache.games.get(old_game, 0)
                        + int(time())
                        - channel_cache.get("last_update", int(time()))
                    )
                    channel_cache.games.setdefault(stream.game_name, 0)
                    channel_cache.last_update = int(time())
                channel_cache.title = stream.title
                channel_cache.game = stream.game_name
                await self.update_alert_messages(
                    channel_cache, self.get_stream_embed(stream)
                )
            if stream.origin == AlertOrigin.callback:
                self.bot.log.info(
                    f"[Kick] Callback received for {stream.user.display_name} "
                    "while live, checking pending title-matched alerts"
                )
        else:
            if on_cooldown:
                self.bot.log.info(
                    f"[Kick] Notification cooldown active for "
                    f"{stream.user.display_name}, restoring old channels/messages"
                )
            self.bot.log.info(
                f"[Kick]{self.is_catchup(stream)} "
                f"{stream.user.display_name} => ONLINE"
            )

        pending_guilds = [
            guild_id
            for guild_id in callback.alert_roles
            if guild_id not in channel_cache.get("triggered_guilds", [])
        ]
        if not pending_guilds:
            if was_live:
                await self.bot.db.write_kick_channel_cache(
                    stream.user, channel_cache
                )
            return

        if (
            callback.display_name != stream.user.display_name
            or callback.get("slug") != stream.user.slug
        ):
            callback.display_name = stream.user.display_name
            callback.slug = stream.user.slug
            await self.bot.db.write_kick_callback(stream.user, callback)

        embed = self.get_stream_embed(stream)
        live_channels, live_alerts, triggered_guilds = (
            await self.send_live_alerts_and_channels(
                stream, embed, callback, channel_cache
            )
        )

        if not was_live:
            channel_cache = {
                "alert_cooldown": int(time()),
                "slug": stream.user.slug,
                "stream_id": stream.stream_id,
                "is_live": True,
                "live_channels": live_channels,
                "live_alerts": live_alerts,
                "last_update": int(time()),
                "games": {stream.game_name: 0},
                "title": stream.title,
                "game": stream.game_name,
                "triggered_guilds": triggered_guilds,
            }
        else:
            channel_cache["triggered_guilds"] = list(
                set(triggered_guilds + channel_cache.get("triggered_guilds", []))
            )
            channel_cache["live_channels"] = list(
                set(live_channels + channel_cache.get("live_channels", []))
            )
            message_ids = [
                alert["message"] for alert in channel_cache.get("live_alerts", [])
            ]
            channel_cache.setdefault("live_alerts", [])
            channel_cache["live_alerts"].extend(
                alert for alert in live_alerts if alert["message"] not in message_ids
            )

        await self.bot.db.write_kick_channel_cache(stream.user, channel_cache)

    def on_cooldown(self, alert_cooldown: int) -> bool:
        if int(time()) - alert_cooldown < 1800 and not self.ignore_cooldowns:
            return True
        return False

    def is_live(self, channel_cache: Union[ChannelCache, YoutubeChannelCache, KickChannelCache]) -> bool:
        # To reduce the likelyhood of strange errors, clarify this.
        if channel_cache.get("is_live", False):
            return True

    @staticmethod
    def get_platform_name(item) -> str:
        if isinstance(item, YoutubeVideo):
            return "YouTube"
        if isinstance(item, KickStream):
            return "Kick"
        return "Twitch"

    @staticmethod
    def get_stream_url(item) -> str:
        if isinstance(item, YoutubeVideo):
            return f"https://youtube.com/watch?v={item.id}"
        if isinstance(item, KickStream):
            return f"https://kick.com/{item.user.slug}"
        return f"https://twitch.tv/{item.user.username}"

    @classmethod
    def get_platform_log_tag(cls, item) -> str:
        return f"[{cls.get_platform_name(item)}]"

    async def send_live_alerts_and_channels(self, item: Union[Stream, YoutubeVideo, KickStream], embed: disnake.Embed, callback: Union[Callback, YoutubeCallback, KickCallback], channel_cache: Union[ChannelCache, YoutubeChannelCache, KickChannelCache]) -> tuple[list, list, list]:
        SelfOverride, DefaultRole, OverrideRole = self.get_overwrites()
        # Cooldown reuse only applies when a stream first goes online. If the
        # stream is already live, untriggered guilds may now match a title
        # phrase and need a fresh alert.
        on_cooldown = (
            not channel_cache.get("is_live", False)
            and self.on_cooldown(channel_cache.get("alert_cooldown", 0))
        )

        live_channels = []
        live_alerts = []
        triggered_guilds = []
        reuse_done = False
        for guild_id, alert_info in {k: v for k, v in callback.alert_roles.items() if k not in channel_cache.get("triggered_guilds", [])}.items():
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                continue

            if alert_info.get("title_match_phrase", None):
                if alert_info.title_match_phrase not in item.title.lower():
                    self.bot.log.info(
                        f"{self.get_platform_log_tag(item)} {item.user.display_name} title phrase for {guild.name} didn't match, skipping alert")
                    continue

            if isinstance(item, YoutubeVideo):
                if item.type == YoutubeVideoType.premiere and not alert_info.get("enable_premieres", False):
                    continue
            link = self.get_stream_url(item)
            message = self.get_discord_live_message(item)

            # Format role mention
            if alert_info.role_id == "everyone":
                role_mention = f" {guild.default_role}"
            elif alert_info.role_id == None:
                role_mention = ""
            else:
                role = guild.get_role(alert_info.role_id)
                role_mention = f" {role.mention}"

            if not on_cooldown:  # Send live alert if not on alert cooldown, and append channel id and message id to channel cache
                alert_channel_id = alert_info.get("notif_channel_id", None)
                alert_channel = self.bot.get_channel(alert_channel_id)
                if alert_channel is not None:
                    try:
                        live_alert = await alert_channel.send(alert_info.get("custom_message", message)+role_mention, embed=embed)
                        live_alerts.append(
                            {"channel": live_alert.channel.id, "message": live_alert.id})
                    except disnake.Forbidden:
                        pass
                    except disnake.HTTPException:
                        pass
            elif not reuse_done:
                self.bot.log.debug(
                    f"Running alert reuse for {item.user.display_name}")
                if channel_cache.get("reusable_alerts", None) is not None:
                    for alert in channel_cache.reusable_alerts:
                        alert_channel_id = alert.get("channel", None)
                        alert_channel = self.bot.get_channel(alert_channel_id)
                        if alert_channel is not None:
                            if callback.alert_roles[str(alert_channel.guild.id)].get("title_match_phrase", None):
                                if callback.alert_roles[str(alert_channel.guild.id)].title_match_phrase not in item.title.lower():
                                    self.bot.log.info(
                                        f"{self.get_platform_log_tag(item)} Didn't match title phrase for {guild.name}, skipping alert")
                                    continue
                            try:
                                alert_message = await alert_channel.fetch_message(alert.get("message"))
                            except disnake.NotFound:
                                pass
                            else:
                                try:
                                    live_alert = await alert_message.edit(content=alert_info.get("custom_message", message)+role_mention, embed=embed)
                                    live_alerts.append(
                                        {"channel": live_alert.channel.id, "message": live_alert.id})
                                except disnake.Forbidden:
                                    pass
                                except disnake.HTTPException:
                                    pass
                # Since reuse is all done in the first iteration, don't waste time doing it again
                reuse_done = True

            match alert_info.mode:
                case 0:  # Temporary live channel mode

                    # Create channel overrides
                    NewChannelOverrides = {self.bot.user: SelfOverride}
                    if alert_info.role_id != "everyone":
                        NewChannelOverrides[guild.default_role] = DefaultRole
                    if alert_info.role_id is not None and alert_info.role_id != "everyone":
                        NewChannelOverrides[role] = OverrideRole

                    # Create temporary channel and add channel id to channel cache
                    try:
                        channel = await guild.create_text_channel(f"🔴{item.user.display_name.lower()}", overwrites=NewChannelOverrides, position=0)
                        if channel:
                            user_escaped = item.user.display_name.replace(
                                '_', '\\_')
                            await channel.send(self.isolate_ltr_text(
                                f"{self.isolate_bidi_text(user_escaped)} "
                                f"is live! {link}"
                            ))
                            live_channels.append(channel.id)
                    except disnake.Forbidden:
                        self.bot.log.warning(
                            f"Error creating text channels for {item.user.display_name} in guild {guild.name}")

                # Notification is already sent, nothing needed to be done
                case 1:
                    pass

                # Permanent channel. Do the same as above, but modify the existing channel, instead of making a new one
                case 2:
                    channel = self.bot.get_channel(alert_info["channel_id"])
                    if channel is not None:
                        try:
                            await channel.edit(name="🔴now-live")
                            live_channels.append(channel.id)
                        except disnake.Forbidden:
                            self.bot.log.warning(
                                f"Error updating channels for {item.user.display_name} in guild {channel.guild.name}")
                    else:
                        self.bot.log.warning(
                            f"Persistent channel not found for {item.user.display_name}")
            triggered_guilds.append(guild_id)

        if item.type != YoutubeVideoType.premiere:
            try:
                async with aiofiles.open("config/callbacks.yml") as c:
                    callback_yaml = load(await c.read(), Loader=Loader)

                if webhooks_info := callback_yaml.get("callbacks", {}).get(item.user.id):
                    for webhook in webhooks_info["webhooks"]:
                        if webhooks_info.get("title_match_phrase", None):
                            if webhooks_info["title_match_phrase"] not in item.title.lower():
                                self.bot.log.info(
                                    f"{self.get_platform_log_tag(item)} Title phrase for slack webhook {item.user.display_name} didn't match, skipping alert")
                                continue
                        if webhook not in channel_cache.get("triggered_guilds", []):
                                if webhook.startswith("https://hooks.slack.com"):
                                    message = self.get_slack_live_message(item)
                                    payload = {
                                        # Top-level text preserves Slack's
                                        # clickable inline-code URL handling.
                                        "text": message,
                                        "unfurl_links": False,
                                        "unfurl_media": False,
                                    }
                                    try:
                                        r = await self.bot.aSession.post(
                                            webhook,
                                            json=payload,
                                            headers={
                                                "Content-type":
                                                "application/json"
                                            },
                                        )
                                        rb = (await r.read()).decode()
                                        if r.status == 200 and rb == 'ok':
                                            self.bot.log.info(f"{self.get_platform_log_tag(item)} Sent slack online webhook for {item.user.display_name}")
                                            triggered_guilds.append(webhook)
                                        else:
                                            self.bot.log.error(f"{self.get_platform_log_tag(item)} Error sending slack online webhook for {item.user.display_name}: {rb}")
                                    except client_exceptions.ClientError as e:
                                        self.bot.log.error(f"{self.get_platform_log_tag(item)} Error sending slack online webhook for {item.user.display_name}: {str(e)}")
                                elif webhook.startswith("https://discord.com/api/webhooks"):
                                    url = self.get_stream_url(item)
                                    message = (
                                        f"{self.get_discord_live_message(item)}\n"
                                        f"{self.isolate_bidi_text(item.title)}\n"
                                        f"{self.isolate_ltr_text(url)}"
                                    )
                                    hook = disnake.Webhook.from_url(webhook, session=self.bot.aSession)
                                    try:
                                        await hook.send(message)
                                        triggered_guilds.append(webhook)
                                    except (disnake.errors.NotFound, disnake.errors.Forbidden, disnake.errors.HTTPException) as e:
                                        self.bot.log.error(f"{self.get_platform_log_tag(item)} Error sending discord online webhook for {item.user.display_name}: {str(e)}")
                                    else:
                                        self.bot.log.info(f"{self.get_platform_log_tag(item)} Sent discord online webhook for {item.user.display_name}")

            except FileNotFoundError:
                pass
            except YAMLError:
                self.bot.log.error(
                    "Error parsing config/callbacks.yml, ignoring.")

        return live_channels, live_alerts, triggered_guilds

    @staticmethod
    def isolate_bidi_text(text: str) -> str:
        return (
            f"{FIRST_STRONG_ISOLATE}{text}"
            f"{POP_DIRECTIONAL_ISOLATE}"
        )

    @staticmethod
    def isolate_ltr_text(text: str) -> str:
        return f"{LTR_ISOLATE}{text}{POP_DIRECTIONAL_ISOLATE}"

    @staticmethod
    def get_discord_live_message(item: Union[Stream, YoutubeVideo, KickStream]) -> str:
        display_name = item.user.display_name.replace('_', '\\_')
        display_name = StreamStateManager.isolate_bidi_text(display_name)
        platform = StreamStateManager.get_platform_name(item)
        return StreamStateManager.isolate_ltr_text(
            f"{display_name} is live on {platform}!"
        )

    @staticmethod
    def get_slack_live_message(item: Union[Stream, YoutubeVideo, KickStream]) -> str:
        if isinstance(item, YoutubeVideo):
            platform = "YouTube"
            platform_emoji = ":youtube:"
            url = f"https://youtu.be/{item.id}"
        elif isinstance(item, KickStream):
            platform = "Kick"
            platform_emoji = ":large_green_circle:"
            url = f"https://kick.com/{item.user.slug}"
        else:
            platform = "Twitch"
            platform_emoji = ":twitch:"
            url = f"https://twitch.tv/{item.user.username}"

        display_name = StreamStateManager.isolate_bidi_text(
            item.user.display_name
        )
        title = StreamStateManager.isolate_bidi_text(item.title)
        return (
            f"{LEFT_TO_RIGHT_MARK}:li::ve: *{display_name}* "
            f"is live on {platform}! {platform_emoji} `{url}`"
            + f"\n{title}"
        )

    async def update_youtube_title(self, video: YoutubeVideo, stream_cache: dict):
        # Titles and alert messages are tracked per video because a channel may
        # have several simultaneous broadcasts.
        if stream_cache.get("title") != video.title:
            stream_cache["title"] = video.title
            await self.bot.ratelimit_request(video.user)
            video.user = await self.bot.yapi.get_user(video.user)
            embed = self.get_stream_embed(video)
            await self.update_alert_messages(stream_cache, embed)

    async def update_alert_messages(self, channel_cache: Union[ChannelCache, YoutubeChannelCache, KickChannelCache], embed: disnake.Embed):
        for m in channel_cache.get("live_alerts", []):
            if channel := self.bot.get_channel(m.get("channel", None)):
                try:
                    message = await channel.fetch_message(m.get("message", None))
                    await message.edit(embed=embed)
                except disnake.NotFound:
                    pass
                except disnake.Forbidden:
                    pass

    def get_overwrites(self) -> tuple[disnake.PermissionOverwrite, disnake.PermissionOverwrite, disnake.PermissionOverwrite]:
        # Permission overrides
        # Make sure the bot has permission to access the channel
        SelfOverride = disnake.PermissionOverwrite()
        SelfOverride.view_channel = True
        SelfOverride.send_messages = True
        DefaultRole = disnake.PermissionOverwrite()  # Deny @everyone access
        DefaultRole.view_channel = False
        DefaultRole.send_messages = False
        # Give users with the role access to view only
        OverrideRole = disnake.PermissionOverwrite()
        OverrideRole.view_channel = True
        return SelfOverride, DefaultRole, OverrideRole

    def is_catchup(self, user: Union[User, YoutubeUser, KickUser, KickStream]) -> str:
        return ' [Catchup]' if user.origin == AlertOrigin.catchup else ''

    async def set_channels_offline(
        self,
        callback: Union[Callback, YoutubeCallback, KickCallback],
        channel_cache: Union[ChannelCache, YoutubeChannelCache, KickChannelCache],
        active_channel_ids: set[int] = None,
    ):
        """Iterate through all live channels, if applicable, either deleting them or renaming them to stream-offline depending on mode"""
        active_channel_ids = active_channel_ids or set()
        for channel_id in channel_cache.get("live_channels", []):
            if channel_id in active_channel_ids:
                continue
            if channel := self.bot.get_channel(channel_id):
                try:
                    alert_info = callback.alert_roles.get(str(channel.guild.id))
                    if alert_info is None:
                        continue
                    match alert_info.mode:
                        case 0:
                            await channel.delete()
                        case 2:
                            await channel.edit(name="stream-offline")
                except disnake.Forbidden:
                    continue
                except disnake.HTTPException:
                    continue

    async def set_twitch_alerts_offline(self, streamer: User, callback: Callback, channel_cache: ChannelCache):
        """Just like channels, iterate through the sent live alerts, and make them past tense."""
        # Find a vod to make the embed link to
        vod = None
        if stream_id := channel_cache.get("stream_id", None):
            vod = await self.bot.tapi.get_video_from_stream_id(streamer, stream_id)

        for alert_ids in channel_cache.get("live_alerts", []):
            if channel := self.bot.get_channel(alert_ids["channel"]):
                try:  # Try to get the live alerts message, skipping if not found
                    message = await channel.fetch_message(alert_ids["message"])
                except disnake.NotFound:
                    continue
                # Get the live alert embed, skipping it was removed, or something else happened
                if len(message.embeds) < 1:
                    continue
                embed = message.embeds[0]
                # Replace the applicable strings with past tense phrasing
                embed.set_author(name=self.isolate_ltr_text(
                    f"{self.isolate_bidi_text(streamer.display_name)} "
                    "is now offline"),
                                 url=embed.author.url, icon_url=embed.author.icon_url)
                embed.url = vod.url if vod else embed.url
                # embed.set_author(name=embed.author.name.replace("is now live on Twitch!", "was live on Twitch!"), url=embed.author.url)
                split_description = embed.description.split('Streaming ', 1)
                if len(split_description) > 1:
                    extracted_game = split_description[1].split('\n')[0]
                    if vod:  # Embed timestamp is the stream start time. Could also use vod.created_at, but meh
                        end_time = embed.timestamp + \
                            timedelta(seconds=vod.duration)
                    else:
                        end_time = utcnow()

                    # List the last 5 games played in the embed
                    if games := channel_cache.get("games", None):
                        if len(games) == 1:
                            past_games = f"Was streaming {extracted_game} for {human_timedelta(end_time, source=embed.timestamp, accuracy=2)}"
                        else:
                            sliced_games = {key: games[key]
                                            for key in list(games.keys())[:5]}
                            sliced_games[list(
                                sliced_games.keys())[-1]] += int(time()) - channel_cache.last_update
                            past_games_list = []
                            for game_name, length in sliced_games.items():
                                if length == 0:
                                    continue
                                past_games_list.append(
                                    f"{game_name} for ~{human_timedelta(embed.timestamp+timedelta(seconds=length), source=embed.timestamp, accuracy=2)}")
                            extra = " (5 most recent)" if len(
                                games) > 5 else ""
                            past_games = f"Was streaming{extra}:" + \
                                "\n" + ',\n'.join(past_games_list)
                    else:
                        # Fallback
                        self.bot.log.warning(
                            "Using fallback game extraction")
                        past_games = f"Was streaming {extracted_game} for ~{human_timedelta(end_time, source=embed.timestamp, accuracy=2)}"
                    if callback.alert_roles.get(str(channel.guild.id), {}).get("show_cest_time", False):
                        cest_tz = tz.gettz("CET")
                        start_time_cest = embed.timestamp.astimezone(
                            cest_tz).strftime("%H:%M")
                        end_time_cest = end_time.astimezone(
                            cest_tz).strftime("%H:%M %Z")
                        detailed_length = f"\n{start_time_cest} - {end_time_cest}"
                    else:
                        detailed_length = ""
                    embed.description = f"{past_games}{detailed_length}"
                    try:
                        await message.edit(content=self.isolate_ltr_text(
                            f"{self.isolate_bidi_text(streamer.display_name)} "
                            "is now offline"
                        ), embed=embed)
                    except disnake.Forbidden:
                        continue

        # try:
        #     async with aiofiles.open("config/callbacks.yml") as c:
        #         callback_yaml = load(await c.read(), Loader=Loader)

        #     if webhooks_info := callback_yaml.get("callbacks", {}).get(streamer.id):

        #         for webhook in webhooks_info["webhooks"]:
        #             if webhook in channel_cache["triggered_guilds"]:
        #                 if webhook.startswith("https://hooks.slack.com"):
        #                     message = f"{streamer.display_name} is no longer live on Twitch"
        #                     if vod:
        #                         message += "\n"
        #                         message += f"{vod.url}"
        #                     try:
        #                         r = await self.bot.aSession.post(webhook, json={"text": message}, headers={"Content-type": "application/json"})
        #                         rb = (await r.read()).decode()
        #                         if r.status == 200 and rb == 'ok':
        #                             self.bot.log.info(f"[Twitch] Sent slack online webhook for {streamer.display_name}")
        #                         else:
        #                             self.bot.log.error(f"[Twitch] Error sending slack online webhook for {streamer.display_name}: {rb}")
        #                     except client_exceptions.ClientError as e:
        #                         self.bot.log.error(f"[Twitch] Error sending slack online webhook for {streamer.display_name}: {str(e)}")
        #                 elif webhook.startswith("https://discord.com/api/webhooks"):
        #                     user_escaped = streamer.display_name.replace('_', '\_')
        #                     message = f"{user_escaped} is no longer live on Twitch"
        #                     if vod:
        #                         message += "\n"
        #                         message += f"{vod.url}"
        #                     hook = disnake.Webhook.from_url(webhook, session=self.bot.aSession)
        #                     try:
        #                         await hook.send(message)
        #                     except (disnake.errors.NotFound, disnake.errors.Forbidden, disnake.errors.HTTPException) as e:
        #                         self.bot.log.error(f"[Twitch] Error sending discord offline webhook for {streamer.display_name}: {str(e)}")
        #                     else:
        #                         self.bot.log.info(f"[Twitch] Sent discord offline webhook for {streamer.display_name}")

        # except FileNotFoundError:
        #     pass
        # except YAMLError:
        #     self.bot.log.error("Error parsing config/callbacks.yml, ignoring.")

    async def set_youtube_alerts_offline(self, channel: YoutubeUser, callback: YoutubeCallback, channel_cache: YoutubeChannelCache):
        """Just like channels, iterate through the sent live alerts, and make them past tense."""
        live_alerts = channel_cache.get("live_alerts", [])
        if not live_alerts:
            return
        video_end_time = await self.bot.yapi.has_video_ended(
            channel_cache.get("video_id")
        )
        end_time = parser.parse(video_end_time) if video_end_time else utcnow()
        for alert_ids in live_alerts:
            if c := self.bot.get_channel(alert_ids["channel"]):
                try:  # Try to get the live alerts message, skipping if not found
                    message = await c.fetch_message(alert_ids["message"])
                except disnake.NotFound:
                    continue
                # Get the live alert embed, skipping it was removed, or something else happened
                if len(message.embeds) < 1:
                    continue
                embed = message.embeds[0]
                # Replace the applicable strings with past tense phrasing
                embed.set_author(name=self.isolate_ltr_text(
                    f"{self.isolate_bidi_text(channel.display_name)} "
                    "is now offline"),
                                 url=embed.author.url, icon_url=embed.author.icon_url)
                if callback["alert_roles"].get(str(c.guild.id), {}).get("show_cest_time", False):
                    cest_tz = tz.gettz("CET")
                    start_time_cest = embed.timestamp.astimezone(
                        cest_tz).strftime("%H:%M")
                    end_time_cest = end_time.astimezone(
                        cest_tz).strftime("%H:%M %Z")
                    detailed_length = f"\n{start_time_cest} - {end_time_cest}"
                else:
                    detailed_length = ""
                embed.description = f"Was streaming for {'~' if not video_end_time else ''}{human_timedelta(end_time, source=embed.timestamp, accuracy=2)}{detailed_length}"
                try:
                    await message.edit(content=self.isolate_ltr_text(
                        f"{self.isolate_bidi_text(channel.display_name)} "
                        "is now offline"
                    ), embed=embed)
                except disnake.Forbidden:  # In case something weird happens
                    continue

        # try:
        #     async with aiofiles.open("config/callbacks.yml") as c:
        #         callback_yaml = load(await c.read(), Loader=Loader)

        #     if webhooks_info := callback_yaml.get("callbacks", {}).get(channel.id):
        #         for webhook in webhooks_info["webhooks"]:
        #             if webhook in channel_cache["triggered_guilds"]:
        #                 if webhook.startswith("https://hooks.slack.com"):
        #                     message = f"{channel.display_name} is no longer live on Youtube\nhttps://youtube.com/watch?v={channel_cache.video_id}"
        #                     try:
        #                         r = await self.bot.aSession.post(webhook, json={"text": message}, headers={"Content-type": "application/json"})
        #                         rb = (await r.read()).decode()
        #                         if r.status == 200 and rb == 'ok':
        #                             self.bot.log.info(f"[Youtube] Sent slack online webhook for {channel.display_name}")
        #                         else:
        #                             self.bot.log.error(f"[Youtube] Error sending slack online webhook for {channel.display_name}: {rb}")
        #                     except client_exceptions.ClientError as e:
        #                         self.bot.log.error(f"[Youtube] Error sending slack online webhook for {channel.display_name}: {str(e)}")
        #                 elif webhook.startswith("https://discord.com/api/webhooks"):
        #                     user_escaped = channel.display_name.replace('_', '\_')
        #                     message = f"{user_escaped} is no longer live on Youtube\nhttps://youtube.com/watch?v={channel_cache.video_id}"
        #                     hook = disnake.Webhook.from_url(webhook, session=self.bot.aSession)
        #                     try:
        #                         await hook.send(message)
        #                     except (disnake.errors.NotFound, disnake.errors.Forbidden, disnake.errors.HTTPException) as e:
        #                         self.bot.log.error(f"[Youtube] Error sending discord offline webhook for {channel.display_name}: {str(e)}")
        #                     else:
        #                         self.bot.log.info(f"[Youtube] Sent discord offline webhook for {channel.display_name}")

        # except FileNotFoundError:
        #     pass
        # except YAMLError:
        #     self.bot.log.error("Error parsing config/callbacks.yml, ignoring.")

    async def set_kick_alerts_offline(
        self,
        channel: Union[KickUser, PartialKickUser],
        callback: KickCallback,
        channel_cache: KickChannelCache,
    ):
        end_time = channel.ended_at or utcnow()
        for alert_ids in channel_cache.get("live_alerts", []):
            discord_channel = self.bot.get_channel(alert_ids["channel"])
            if discord_channel is None:
                continue
            try:
                message = await discord_channel.fetch_message(alert_ids["message"])
            except (disnake.NotFound, disnake.Forbidden):
                continue
            if not message.embeds:
                continue

            embed = message.embeds[0]
            embed.set_author(
                name=self.isolate_ltr_text(
                    f"{self.isolate_bidi_text(channel.display_name)} "
                    "is now offline"
                ),
                url=embed.author.url,
                icon_url=embed.author.icon_url,
            )

            games = channel_cache.get("games", {})
            if games:
                current_game = channel_cache.get("game")
                if current_game in games:
                    games[current_game] += max(
                        0, int(end_time.timestamp()) - channel_cache.get("last_update", int(end_time.timestamp()))
                    )
                if len(games) == 1:
                    game_name = next(iter(games))
                    description = (
                        f"Was streaming {self.isolate_bidi_text(game_name)} for "
                        f"{human_timedelta(end_time, source=embed.timestamp, accuracy=2)}"
                    )
                else:
                    recent_games = list(games.items())[-5:]
                    game_lines = [
                        f"{self.isolate_bidi_text(game_name)} for "
                        f"~{human_timedelta(embed.timestamp + timedelta(seconds=seconds), source=embed.timestamp, accuracy=2)}"
                        for game_name, seconds in recent_games
                        if seconds > 0
                    ]
                    description = "Was streaming:\n" + ",\n".join(game_lines)
            else:
                description = (
                    "Was streaming for "
                    f"~{human_timedelta(end_time, source=embed.timestamp, accuracy=2)}"
                )

            if callback.alert_roles.get(str(discord_channel.guild.id), {}).get(
                "show_cest_time", False
            ):
                cest_tz = tz.gettz("CET")
                description += (
                    "\n"
                    f"{embed.timestamp.astimezone(cest_tz).strftime('%H:%M')} - "
                    f"{end_time.astimezone(cest_tz).strftime('%H:%M %Z')}"
                )
            embed.description = self.isolate_ltr_text(description)
            try:
                await message.edit(
                    content=self.isolate_ltr_text(
                        f"{self.isolate_bidi_text(channel.display_name)} "
                        "is now offline"
                    ),
                    embed=embed,
                )
            except disnake.Forbidden:
                continue

    def get_stream_embed(self, item: Union[Stream, YoutubeVideo, KickStream], **kwargs) -> disnake.Embed:
        if isinstance(item, Stream):
            embed = disnake.Embed(
                title=self.isolate_bidi_text(item.title),
                url=f"https://twitch.tv/{item.user.name}",
                description=self.isolate_ltr_text(
                    f"Streaming {self.isolate_bidi_text(item.game)}\n"
                    f"[Watch Stream](https://twitch.tv/{item.user.name})"
                ),
                colour=TWITCH_PURPLE, timestamp=item.started_at)
            embed.set_author(name=self.isolate_ltr_text(
                f"{self.isolate_bidi_text(item.user.display_name)} "
                "is now live on Twitch!"),
                             url=f"https://twitch.tv/{item.user.name}", icon_url=item.user.avatar)

        elif isinstance(item, TitleEvent):
            embed = disnake.Embed(
                title=self.isolate_bidi_text(item.title),
                url=f"https://twitch.tv/{item.broadcaster.username}",
                description=self.isolate_ltr_text(
                    f"Streaming {self.isolate_bidi_text(item.game)}\n"
                    f"[Watch Stream](https://twitch.tv/"
                    f"{item.broadcaster.name})"
                ),
                colour=TWITCH_PURPLE, timestamp=kwargs["stream"].started_at)
            embed.set_author(name=self.isolate_ltr_text(
                f"{self.isolate_bidi_text(item.broadcaster.display_name)} "
                "is now live on Twitch!"),
                             url=f"https://twitch.tv/{item.broadcaster.username}", icon_url=kwargs["stream"].user.avatar)

        elif isinstance(item, YoutubeVideo):
            embed = disnake.Embed(
                title=self.isolate_bidi_text(item.title),
                url=f"https://youtube.com/watch?v={item.id}",
                description=self.isolate_ltr_text(
                    f"[Watch Stream](https://youtube.com/watch?v={item.id})"
                ),
                colour=YOUTUBE_RED, timestamp=item.started_at)
            embed.set_author(name=self.isolate_ltr_text(
                f"{self.isolate_bidi_text(item.user.display_name)} "
                "is now live on YouTube!"),
                             url=f"https://youtube.com/watch?v={item.id}", icon_url=item.user.avatar_url)

        elif isinstance(item, KickStream):
            url = f"https://kick.com/{item.user.slug}"
            embed = disnake.Embed(
                title=self.isolate_bidi_text(item.title),
                url=url,
                description=self.isolate_ltr_text(
                    f"Streaming {self.isolate_bidi_text(item.game)}\n"
                    f"[Watch Stream]({url})"
                ),
                colour=KICK_GREEN,
                timestamp=item.started_at,
            )
            embed.set_author(
                name=self.isolate_ltr_text(
                    f"{self.isolate_bidi_text(item.user.display_name)} "
                    "is now live on Kick!"
                ),
                url=url,
                icon_url=item.user.avatar_url,
            )

        embed.set_footer(text=self.footer_msg)
        return embed

    async def title_change_update_alerts(self, event: TitleEvent, stream: Stream, old_game: str):
        await self.bot.ratelimit_request(event.broadcaster)
        # channel_cache = await get_channel_cache()
        channel_cache = await self.bot.db.get_channel_cache(stream.user)
        # EventSub is the source of truth for this update. Helix can briefly
        # return the previous title immediately after the event arrives.
        stream.title = event.title
        stream.stream_title = event.title
        stream.game = event.game
        stream.game_name = event.game
        stream.game_id = event.game_id
        stream.user = await self.bot.tapi.get_user(user=stream.user)
        embed = self.get_stream_embed(event, stream=stream)
        await self.update_alert_messages(channel_cache, embed)

        # Add new game to games list if applicable
        if channel_cache.get("games", None) and event.game != old_game:
            old_time = channel_cache.games.get(event.game_name, 0)
            if event.game_name in channel_cache.games.keys():
                channel_cache.games.pop(event.game_name, None)
            if channel_cache.games.get(old_game, None) != None:
                channel_cache.games[old_game] = (
                    int(time()) - channel_cache.last_update) + old_time
            channel_cache.games[event.game_name] = old_time
            channel_cache.last_update = int(time())
            await self.bot.db.write_channel_cache(stream.user, channel_cache)

        # Re-evaluate guilds that were skipped when the stream's original
        # title did not contain their configured match phrase.
        await self.on_streamer_online(stream)

    async def viewer_count_milestones(self, stream: Stream, callback: Callback, channel_cache: ChannelCache):
        # Check if view count is higher than minimum and if it exceeds a previous announcement + interval amount
        if stream.view_count >= self.bot.viewer_milestones_minimum and stream.view_count >= channel_cache.get("viewer_milestone", 0)+self.bot.viewer_milestones_interval:
            self.bot.log.info(
                f"{stream.user.username} => VIEW COUNT ({stream.view_count:,}) (Twitch)")
            # Do some funky maths to calculate the interval that it passed
            channel_cache.viewer_milestone = floor((stream.view_count-self.bot.viewer_milestones_minimum) /
                                                   self.bot.viewer_milestones_interval)*self.bot.viewer_milestones_interval+self.bot.viewer_milestones_minimum
            await self.bot.db.write_channel_cache(stream.user, channel_cache)

            # Create embed message
            stream.user = await self.bot.tapi.get_user(user=stream.user)
            view_embed = disnake.Embed(
                title=self.isolate_ltr_text(
                    f"{self.isolate_bidi_text(stream.user.display_name)} "
                    f"just passed "
                    f"{channel_cache.get('viewer_milestone', 0):,} viewers!"
                ),
                url=f"https://twitch.tv/{stream.user.name}",
                description=self.isolate_ltr_text(
                    f"Streaming {self.isolate_bidi_text(stream.game)} for "
                    f"{human_timedelta(stream.started_at, suffix=False, accuracy=2)}\n"
                    f"[Watch Stream](https://twitch.tv/{stream.user.name})"
                ),
                colour=TWITCH_PURPLE, timestamp=utcnow())
            view_embed.set_author(
                name=self.isolate_bidi_text(stream.title),
                url=f"https://twitch.tv/{stream.user.name}",
                icon_url=stream.user.avatar)
            # This got stuck when combined. Not sure why
            view_embed.set_footer(text=self.footer_msg)

            for guild_id, alert_info in callback.alert_roles.items():
                guild = self.bot.get_guild(int(guild_id))
                if guild is None:
                    continue

                if alert_info.get("title_match_phrase", None):
                    if alert_info.title_match_phrase not in stream.title.lower():
                        self.bot.log.info(
                            f"[Twitch] {stream.user.display_name} title phrase for {guild.name} didn't match, skipping alert")
                        continue

                # Format role mention
                if alert_info.role_id == "everyone":
                    role_mention = f" {guild.default_role}"
                elif alert_info.role_id == None:
                    role_mention = ""
                else:
                    role = guild.get_role(alert_info.role_id)
                    role_mention = f" {getattr(role, 'mention', '')}"

                if alert_channel := self.bot.get_channel(alert_info.get("notif_channel_id", None)):
                    try:
                        # live_alert = await alert_channel.send(f"{stream.user.display_name} is live on Twitch!{role_mention}", embed=embed)
                        user_escaped = stream.user.display_name.replace(
                            '_', '\\_')
                        await alert_channel.send(self.isolate_ltr_text(
                            f"{self.isolate_bidi_text(user_escaped)} "
                            f"just passed "
                            f"{channel_cache.get('viewer_milestone', 0):,} "
                            f"viewers!{role_mention}"
                        ), embed=view_embed)
                    except disnake.Forbidden:
                        pass
                    except disnake.HTTPException:
                        pass


def setup(bot):
    bot.add_cog(StreamStateManager(bot))
