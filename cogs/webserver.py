from __future__ import annotations

import hashlib
import hmac
import base64
import binascii
from typing import TYPE_CHECKING

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from twitchtools import PartialKickUser, PartialUser, PartialYoutubeUser
from twitchtools.enums import AlertOrigin

if TYPE_CHECKING:
    from main import TwitchCallBackBot


class RecieverWebServer:
    def __init__(self, bot, port: int = 18271):
        self.bot: TwitchCallBackBot = bot
        self.port = port
        self.web_server = web.Application()
        self.web_server.add_routes(
            [web.route('*', '/{callback_type}/{channel_id}', self._reciever)])
        self.web_server.add_routes([web.route('*', '/', self._info)])

        # TO BE USED ONLY FOR DEBUGGING PURPOSES
        self.allow_unverified_requests: bool = False

    async def start(self):
        runner = web.AppRunner(self.web_server)
        await runner.setup()
        await web.TCPSite(runner, host="0.0.0.0", port=self.port).start()
        self.bot.log.info(f"[Webserver] Running on localhost:{self.port}")
        return self.web_server

    async def _info(self, request: web.Request):
        return web.Response(status=200, text=f"Twitch Tools webserver running here")

    async def _reciever(self, request: web.Request):
        await self.bot.wait_until_ready()
        channel_id = request.match_info["channel_id"]
        callback_type = request.match_info["callback_type"]
        self.bot.log.info(f"[Webserver] {request.method} {callback_type} for {channel_id}")
        if request.method == 'POST':
            return await self.post_request(request, callback_type, channel_id)
        elif request.method == "GET":
            return await self.get_request(request, callback_type, channel_id)
        return web.Response(status=404)

    async def verify_request(self, request: web.Request, secret: str):
        if self.allow_unverified_requests:
            return True
        # notif_cache = await get_notif_cache()
        await self.bot.wait_until_db_ready()
        notif_cache = await self.bot.db.get_notif_cache()

        try:
            message_id = request.headers["Twitch-Eventsub-Message-Id"]
            timestamp = request.headers["Twitch-Eventsub-Message-Timestamp"]
            signature = request.headers['Twitch-Eventsub-Message-Signature']
        except KeyError as e:
            self.bot.log.info(f"[Twitch] Request Denied. Missing Key {e}")
            return False
        if message_id in notif_cache:
            return None

        hmac_message = message_id.encode("utf-8") + timestamp.encode("utf-8") + await request.read()
        h = hmac.new(secret.encode("utf-8"), hmac_message, hashlib.sha256)
        expected_signature = f"sha256={h.hexdigest()}"
        self.bot.log.debug(f"Timestamp: {timestamp}")
        self.bot.log.debug(
            f"Expected: {expected_signature}. Receieved: {signature}")
        if signature != expected_signature:
            return False
        notif_cache.append(message_id)
        await self.bot.db.write_notif_cache(notif_cache)
        return True

    async def verify_kick_request(self, request: web.Request):
        if self.allow_unverified_requests:
            return True
        await self.bot.wait_until_db_ready()
        notification_cache = await self.bot.db.get_notif_cache()
        try:
            message_id = request.headers["Kick-Event-Message-Id"]
            timestamp = request.headers["Kick-Event-Message-Timestamp"]
            signature = request.headers["Kick-Event-Signature"]
        except KeyError as error:
            self.bot.log.info(f"[Kick] Request denied. Missing key {error}")
            return False

        if message_id in notification_cache:
            return None

        body = await request.read()
        signed_message = (
            message_id.encode("utf-8")
            + b"."
            + timestamp.encode("utf-8")
            + b"."
            + body
        )
        try:
            decoded_signature = base64.b64decode(signature, validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            self.bot.log.info(f"[Kick] Invalid webhook signature: {error}")
            return False

        for force_key_refresh in (False, True):
            try:
                public_key_pem = await self.bot.kapi.get_public_key(
                    force=force_key_refresh
                )
                public_key = serialization.load_pem_public_key(
                    public_key_pem.encode("utf-8")
                )
                public_key.verify(
                    decoded_signature,
                    signed_message,
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
                break
            except InvalidSignature:
                if force_key_refresh:
                    self.bot.log.info("[Kick] Invalid webhook signature")
                    return False
            except (ValueError, TypeError) as error:
                self.bot.log.error(f"[Kick] Invalid public key: {error}")
                return False
            except Exception as error:
                self.bot.log.error(f"[Kick] Could not verify webhook: {error}")
                return False

        notification_cache.append(message_id)
        await self.bot.db.write_notif_cache(notification_cache)
        return True

    async def get_request(self, request: web.Request, callback_type: str, channel_id: str):
        if callback_type == "youtube":
            try:
                mode = request.query['hub.mode']
                challenge = request.query['hub.challenge']
            except KeyError:
                return web.Response(status=404)
            if mode == "unsubscribe":
                self.bot.log.info(
                    f"Youtube subscription removal confirmed for {channel_id}")
                return web.Response(text=challenge)
            callback = await self.bot.db.get_yt_callback_by_id(channel_id)
            if callback is None:
                self.bot.log.info(f"[Youtube] Request for {channel_id} not found")
                return web.Response(status=404)

            try:
                #mode = request.query['hub.mode']
                #challenge = request.query['hub.challenge']
                verification = request.query['hub.verify_token']
                secret = verification.split(":")[0]
                verify_token = verification.split(":")[-1]
            except KeyError as e:
                self.bot.log.warning(
                    f"[Youtube] Subscription failed: Not all arguments provided {e}")
                return web.Response(status=404)

            if mode == "subscribe" and secret == callback["secret"]:
                self.bot.dispatch(
                    "youtube_subscription_confirmation", verify_token)
                self.bot.log.info(
                    f"[Youtube] Subscription confirmed for {callback['display_name']}")
                return web.Response(text=challenge)
        return web.Response(status=404)

    async def post_request(self, request: web.Request, callback_type: str, channel_id: str):
        if channel_id == "_callbacktest":
            return web.Response(status=204)
        if callback_type == "youtube":
            callback = await self.bot.db.get_yt_callback_by_id(channel_id)
            if callback is None:
                self.bot.log.info(
                    f"[Youtube] Request for {channel_id} not found")
                return web.Response(status=404)
            data = (await request.read()).decode('utf-8')
            self.bot.log.info(
                f"[Youtube] Notification for {callback['display_name']}")
            return await self.youtube_notification(PartialYoutubeUser(channel_id, callback["display_name"]), data)

        if callback_type == "kick":
            return await self.kick_notification(request)

        else:
            callback = await self.bot.db.get_callback_by_id(channel_id)
            if callback is None:
                callback = await self.bot.db.get_title_callback_by_id(channel_id)
                if callback is None:
                    self.bot.log.info(
                        f"[Twitch] Request for {channel_id} not found")
                    return web.Response(status=400)
            channel = PartialUser(
                channel_id, callback["display_name"].lower(), callback["display_name"])

            verified = await self.verify_request(request, callback["secret"])
            if verified == False:
                self.bot.log.info("[Twitch] Unverified request, ignoring")
                return web.Response(status=400)
            elif verified == None:
                self.bot.log.info("[Twitch] Request duplicate, ignoring")
                return web.Response(status=202)
            try:
                mode = request.headers["Twitch-Eventsub-Message-Type"]
            except KeyError:
                self.bot.log.info("[Twitch] Missing required parameters")
                return web.Response(status=400)
            data = await request.json()

            if mode == "webhook_callback_verification":  # Initial Verification of Subscription
                if callback_type == "titlecallback":
                    self.bot.dispatch("subscription_confirmation",
                                      data["subscription"]["id"])
                    self.bot.log.info(
                        f"[Twitch] Title Change Subscription confirmed for {channel.display_name}")
                else:
                    self.bot.dispatch("subscription_confirmation",
                                      data["subscription"]["id"])
                    self.bot.log.info(
                        f"[Twitch] Subscription confirmed for {channel.display_name}")
                challenge = data['challenge']
                return web.Response(status=202, text=challenge)
            elif mode == "authorization_revoked":
                if callback_type == "titlecallback":
                    self.bot.log.critical(
                        f"Title Change Authorization Revoked for {channel.display_name}!")
                else:
                    self.bot.log.critical(
                        f"Authorization Revoked for {channel.display_name}!")
                return web.Response(status=202)
            elif mode == "notification":
                if callback_type == "titlecallback":
                    self.bot.log.info(
                        f"[Twitch] Title change notification for {channel.display_name}")
                    return await self.title_notification(channel, data)
                else:
                    self.bot.log.info(
                        f"[Twitch] Twitch notification for {channel.display_name}")
                    return await self.notification(channel, data)
            else:
                self.bot.log.info("[Twitch] Unknown mode")
        return web.Response(status=404)

    async def title_notification(self, channel: PartialUser, data: dict):
        event = self.bot.tapi.get_event(data)
        self.bot.queue.put_nowait(event)

        return web.Response(status=202)

    async def notification(self, channel: PartialUser, data: dict):
        streamer = await self.bot.tapi.get_user(user_id=channel.id)
        streamer.origin = AlertOrigin.callback
        stream = await self.bot.tapi.get_stream(streamer, origin=AlertOrigin.callback)

        live = stream is not None
        if self.allow_unverified_requests:
            live = True if data["subscription"]["type"] == "stream.online" else False

        if live:
            self.bot.queue.put_nowait(stream)
        else:
            self.bot.queue.put_nowait(streamer)

        return web.Response(status=202)

    async def youtube_notification(self, channel: PartialYoutubeUser, data: str):
        self.bot.test = (channel, data)
        video = await self.bot.yapi.parse_video_xml(channel, data)

        if video:
            self.bot.queue.put_nowait(video)
        # The webserver can never trigger an offline event, only catchup can.
        # else:
        #     self.bot.queue.put_nowait(channel)

        return web.Response(status=202)

    async def kick_notification(self, request: web.Request):
        verified = await self.verify_kick_request(request)
        if verified is False:
            return web.Response(status=401)
        if verified is None:
            self.bot.log.info("[Kick] Duplicate request ignored")
            return web.Response(status=202)

        event_type = request.headers.get("Kick-Event-Type")
        if event_type != "livestream.status.updated":
            self.bot.log.info(f"[Kick] Ignoring unsupported event {event_type!r}")
            return web.Response(status=204)

        try:
            data = await request.json()
            broadcaster = data["broadcaster"]
            broadcaster_id = int(broadcaster["user_id"])
        except (KeyError, TypeError, ValueError):
            self.bot.log.info("[Kick] Notification was missing broadcaster data")
            return web.Response(status=400)

        callback = await self.bot.db.get_kick_callback_by_id(broadcaster_id)
        if callback is None:
            self.bot.log.info(f"[Kick] Request for {broadcaster_id} not found")
            return web.Response(status=204)

        if data.get("is_live"):
            stream = await self.bot.kapi.get_stream(
                user_id=broadcaster_id, origin=AlertOrigin.callback
            )
            if stream is not None:
                self.bot.queue.put_nowait(stream)
            else:
                self.bot.log.warning(
                    f"[Kick] {callback['display_name']} reported live before "
                    "the livestream API reflected it; catch-up will retry"
                )
        else:
            self.bot.queue.put_nowait(
                PartialKickUser(
                    broadcaster_id,
                    broadcaster.get("channel_slug") or callback["slug"],
                    broadcaster.get("username") or callback["display_name"],
                    broadcaster.get("profile_picture"),
                    origin=AlertOrigin.callback,
                    ended_at=data.get("ended_at"),
                )
            )

        self.bot.log.info(
            f"[Kick] {callback['display_name']} => "
            f"{'ONLINE' if data.get('is_live') else 'OFFLINE'}"
        )
        return web.Response(status=202)
