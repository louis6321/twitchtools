from typing import TYPE_CHECKING

from disnake.ext import commands, tasks

from twitchtools import (AlertOrigin, ApplicationCustomContext, Callback,
                         KickCallback, PartialKickUser, PartialUser,
                         PartialYoutubeUser, YoutubeCallback,
                         has_manage_permissions)
from twitchtools.exceptions import (VideoNotFound, VideoNotStream,
                                    VideoStreamEnded)
from twitchtools.youtube_cache import get_youtube_stream_states

if TYPE_CHECKING:
    from twitchtools import TwitchCallBackBot


class Catchup(commands.Cog):
    def __init__(self, bot):
        self.bot: TwitchCallBackBot = bot
        super().__init__()
        self.twitch_backup_checks.start()
        self.youtube_backup_checks.start()
        self.kick_backup_checks.start()

    def cog_unload(self):
        self.twitch_backup_checks.cancel()
        self.youtube_backup_checks.cancel()
        self.kick_backup_checks.cancel()

    @tasks.loop(seconds=120)
    async def twitch_backup_checks(self):
        await self.twitch_catchup()
        self.bot.log.debug("Ran twitch catchup")

    # Youtube callbacks are extremely unreliable and need a higher frequency. Also stream ends are only triggered by catchup
    @tasks.loop(seconds=600)
    async def youtube_backup_checks(self):
        await self.youtube_catchup()
        self.bot.log.debug("Ran youtube catchup")

    @tasks.loop(seconds=120)
    async def kick_backup_checks(self):
        await self.kick_catchup()
        self.bot.log.debug("Ran Kick catchup")

    @commands.slash_command()
    async def catchup(self, ctx: ApplicationCustomContext):
        pass

    @catchup.sub_command(name="all", description="Owner Only: Run streamer catchup manually")
    @commands.is_owner()
    @commands.cooldown(1, 10, commands.BucketType.default)
    async def catchup_all(self, ctx: ApplicationCustomContext):
        await ctx.response.defer(ephemeral=True)
        await self.twitch_catchup()
        await self.youtube_catchup()
        await self.kick_catchup()
        self.bot.log.info("Finished manual catchup")
        await ctx.send(f"{self.bot.emotes.success} Finished catchup!", ephemeral=True)
    
    @catchup.sub_command(name="server", description="Run streamer catchup manually for streamers in this server")
    @has_manage_permissions()
    @commands.cooldown(1, 10, commands.BucketType.guild)
    async def catchup_server(self, ctx: ApplicationCustomContext):
        await ctx.response.defer(ephemeral=True)
        twitch_filtered_callbacks = {s: c for s, c in (await self.bot.db.get_all_callbacks()).items() if str(ctx.guild.id) in c.alert_roles.keys()}
        await self.twitch_catchup(twitch_filtered_callbacks)
        
        youtube_filtered_callbacks = {s: c for s, c in (await self.bot.db.get_all_yt_callbacks()).items() if str(ctx.guild.id) in c.alert_roles.keys()}
        await self.youtube_catchup(youtube_filtered_callbacks)

        kick_filtered_callbacks = {
            streamer_id: callback
            for streamer_id, callback in (
                await self.bot.db.get_all_kick_callbacks()
            ).items()
            if str(ctx.guild.id) in callback.alert_roles
        }
        await self.kick_catchup(kick_filtered_callbacks)
        self.bot.log.info(f"Finished manual server catchup for {ctx.guild.name}")
        await ctx.send(f"{self.bot.emotes.success} Finished server catchup!", ephemeral=True)

    async def twitch_catchup(self, callbacks: dict[str, Callback] = None):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        callbacks = callbacks or await self.bot.db.get_all_callbacks()
        if callbacks == {}:
            return

        # Fetch all streamers, returning the currently live ones
        streams = await self.bot.tapi.get_streams(user_ids=list(callbacks.keys()), origin=AlertOrigin.catchup)
        # We only need the ID from them, as a string due to dict keys only being strings
        online_stream_uids = [str(stream.user.id) for stream in streams]

        # Iterate through all callbacks and update all streamers
        for streamer_id, callback_info in callbacks.items():
            if streamer_id in online_stream_uids:
                stream = [s for s in streams if s.user.id ==
                          int(streamer_id)][0]
                # Update display name if needed
                if callback_info.display_name != stream.user.display_name:
                    callback_info.display_name = stream.user.display_name
                    await self.bot.db.write_callback(stream.user, callback_info)
                self.bot.queue.put_nowait(stream)
            else:
                self.bot.queue.put_nowait(PartialUser(
                    streamer_id, callback_info.display_name.lower(), callback_info.display_name, origin=AlertOrigin.catchup))

    async def youtube_catchup(self, callbacks: dict[PartialYoutubeUser, YoutubeCallback] = None):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        callbacks = callbacks or await self.bot.db.get_all_yt_callbacks()
        if callbacks == {}:
            return

        # Get all caches, saves multiple DB calls for same data
        caches = {c: await self.bot.db.get_yt_channel_cache(c) for c in callbacks.keys()}

        # Fetch recent videos for every channel, including channels that already
        # have a live stream. YouTube permits multiple concurrent broadcasts.
        recent_vids = await self.bot.yapi.get_recent_video_ids(
            list(callbacks.keys())
        )
        self.bot.log.debug(f"Recent Video IDs for Channels: {recent_vids}")
        live_streams = await self.bot.yapi.are_videos_live(recent_vids)
        self.bot.log.debug(f"Live Youtube Streams: {live_streams}")

        cached_streams = {
            channel: get_youtube_stream_states(cache)
            for channel, cache in caches.items()
        }
        live_videos_cached = [
            video_id
            for streams in cached_streams.values()
            for video_id in streams.keys()
        ]
        ended_videos = await self.bot.yapi.have_videos_ended(live_videos_cached)

        # Update every cached stream and enqueue every newly discovered stream.
        for channel, callback_info in callbacks.items():
            channel_cached_streams = cached_streams[channel]

            for video_id in list(channel_cached_streams.keys()):
                if video_id in ended_videos:
                    self.bot.queue.put_nowait(
                        PartialYoutubeUser(
                            channel.id,
                            channel.display_name,
                            origin=AlertOrigin.catchup,
                            video_id=video_id,
                        )
                    )
                    continue

                try:
                    video = await self.bot.yapi.get_stream(
                        video_id, origin=AlertOrigin.catchup
                    )
                except (VideoNotFound, VideoNotStream, VideoStreamEnded):
                    self.bot.queue.put_nowait(
                        PartialYoutubeUser(
                            channel.id,
                            channel.display_name,
                            origin=AlertOrigin.catchup,
                            video_id=video_id,
                        )
                    )
                    continue
                self.bot.queue.put_nowait(video)

            new_video_ids = [
                video_id
                for video_id in live_streams.get(channel, [])
                if video_id not in channel_cached_streams
            ]
            for video_id in new_video_ids:
                try:
                    video = await self.bot.yapi.get_stream(
                        video_id, origin=AlertOrigin.catchup
                    )
                except (VideoNotFound, VideoNotStream, VideoStreamEnded):
                    continue

                if callback_info.display_name != video.user.display_name:
                    callback_info.display_name = video.user.display_name
                    await self.bot.db.write_yt_callback(
                        video.user, callback_info
                    )
                self.bot.queue.put_nowait(video)

    async def kick_catchup(self, callbacks: dict[str, KickCallback] = None):
        await self.bot.wait_until_ready()
        await self.bot.wait_until_db_ready()
        if callbacks is None:
            callbacks = await self.bot.db.get_all_kick_callbacks()
        if not callbacks:
            return

        streams = await self.bot.kapi.get_streams(
            list(callbacks.keys()), origin=AlertOrigin.catchup
        )
        streams_by_user_id = {str(stream.user.id): stream for stream in streams}

        for user_id, callback in callbacks.items():
            stream = streams_by_user_id.get(str(user_id))
            if stream is not None:
                if (
                    callback.display_name != stream.user.display_name
                    or callback.get("slug") != stream.user.slug
                ):
                    callback.display_name = stream.user.display_name
                    callback.slug = stream.user.slug
                    await self.bot.db.write_kick_callback(stream.user, callback)
                self.bot.queue.put_nowait(stream)
            else:
                self.bot.queue.put_nowait(
                    PartialKickUser(
                        user_id,
                        callback.slug,
                        callback.display_name,
                        origin=AlertOrigin.catchup,
                    )
                )


def setup(bot):
    bot.add_cog(Catchup(bot))
