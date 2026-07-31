from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING, Iterable, Optional, Union

from aiohttp import ClientResponse, ClientSession

from .enums import AlertOrigin
from .exceptions import BadAuthorization, BadRequest, SubscriptionError
from .stream import KickStream
from .subscription import KickSubscription
from .user import KickUser, PartialKickUser

if TYPE_CHECKING:
    from main import TwitchCallBackBot


class http_kick:
    """Client for Kick's official public API."""

    def __init__(
        self,
        bot,
        kick_client_id: str = None,
        kick_client_secret: str = None,
        **kwargs,
    ):
        self.bot: TwitchCallBackBot = bot
        self.base = "https://api.kick.com/public/v1"
        self.oauth2_base = "https://id.kick.com/oauth"
        self.client_id = kick_client_id
        self.client_secret = kick_client_secret
        self.access_token: Optional[str] = None
        self.token_expires_at = 0.0
        self.public_key: Optional[str] = None
        self._token_lock = asyncio.Lock()
        self.session: Optional[ClientSession] = None
        self.bot.add_listener(self._make_session, "on_connect")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def _make_session(self):
        if self.session is None or self.session.closed:
            self.session = ClientSession()
        if self.configured:
            await self._fetch_access_token()

    async def close_session(self):
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def _fetch_access_token(self):
        if not self.configured:
            raise BadAuthorization("Kick client ID and secret are not configured")
        async with self._token_lock:
            if self.access_token and monotonic() < self.token_expires_at - 30:
                return
            if self.session is None or self.session.closed:
                self.session = ClientSession()
            response = await self.session.post(
                f"{self.oauth2_base}/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            data = await response.json(content_type=None)
            if response.status != 200 or not data.get("access_token"):
                raise BadAuthorization(
                    f"Kick token request failed: {data.get('message') or data.get('error') or response.status}"
                )
            self.access_token = data["access_token"]
            self.token_expires_at = monotonic() + int(data.get("expires_in", 3600))

    async def _request(self, path: str, method: str = "get", **kwargs) -> ClientResponse:
        await self._fetch_access_token()
        response = await self.session.request(
            method, f"{self.base}{path}", headers=self.headers, **kwargs
        )
        if response.status == 401:
            self.access_token = None
            await self._fetch_access_token()
            response = await self.session.request(
                method, f"{self.base}{path}", headers=self.headers, **kwargs
            )
        return response

    @staticmethod
    def chunks(items: Iterable, size: int):
        items = list(items)
        for index in range(0, len(items), size):
            yield items[index:index + size]

    async def _json(self, response: ClientResponse, error: type[Exception] = BadRequest):
        data = await response.json(content_type=None)
        if response.status < 200 or response.status >= 300:
            message = data.get("message") or data.get("error") or f"HTTP {response.status}"
            raise error(message)
        return data

    async def get_user(
        self,
        user: PartialKickUser = None,
        user_id: int = None,
        slug: str = None,
    ) -> Optional[KickUser]:
        if user is not None:
            user_id = user.id
        if user_id is None and slug is None:
            raise BadRequest("A Kick user ID or channel slug is required")

        if slug is not None:
            slug = slug.strip().rstrip("/").rsplit("/", 1)[-1]
            slug = slug.lower().removeprefix("@")
        params = [("broadcaster_user_id", int(user_id))] if user_id is not None else [("slug", slug)]
        channel_response = await self._request("/channels", params=params)
        channel_data = (await self._json(channel_response)).get("data", [])
        if not channel_data:
            return None
        channel = channel_data[0]

        user_response = await self._request(
            "/users", params=[("id", int(channel["broadcaster_user_id"]))]
        )
        users = (await self._json(user_response)).get("data", [])
        user_data = users[0] if users else {}
        return KickUser(
            user_id=channel["broadcaster_user_id"],
            slug=channel["slug"],
            name=user_data.get("name") or channel["slug"],
            profile_picture=user_data.get("profile_picture"),
            channel_description=channel.get("channel_description"),
            banner_picture=channel.get("banner_picture"),
        )

    async def get_streams(
        self,
        user_ids: list[Union[int, str]],
        origin: AlertOrigin = AlertOrigin.unavailable,
    ) -> list[KickStream]:
        if not user_ids:
            raise BadRequest("At least one Kick user ID is required")
        streams = []
        for chunk in self.chunks(user_ids, 100):
            response = await self._request(
                "/users/livestreams",
                params=[("user_id", int(user_id)) for user_id in chunk],
            )
            data = await self._json(response)
            streams.extend(
                KickStream(**stream, origin=origin)
                for stream in data.get("data", [])
            )
        return streams

    async def get_stream(
        self,
        user: PartialKickUser = None,
        user_id: int = None,
        origin: AlertOrigin = AlertOrigin.unavailable,
    ) -> Optional[KickStream]:
        if user is not None:
            user_id = user.id
        if user_id is None:
            raise BadRequest("A Kick user ID is required")
        streams = await self.get_streams([user_id], origin=origin)
        return streams[0] if streams else None

    async def create_subscription(
        self, broadcaster: PartialKickUser
    ) -> KickSubscription:
        response = await self._request(
            "/events/subscriptions",
            method="post",
            json={
                "broadcaster_user_id": broadcaster.id,
                "events": [
                    {"name": "livestream.status.updated", "version": 1}
                ],
                "method": "webhook",
            },
        )
        data = await self._json(response, SubscriptionError)
        subscriptions = data.get("data", [])
        if not subscriptions:
            raise SubscriptionError("Kick did not return a subscription")
        subscription = subscriptions[0]
        if subscription.get("error") or not subscription.get("subscription_id"):
            raise SubscriptionError(
                subscription.get("error") or "Kick did not create the subscription"
            )
        return KickSubscription(
            subscription["subscription_id"],
            broadcaster.id,
            event=subscription.get("name", "livestream.status.updated"),
            version=subscription.get("version", 1),
        )

    async def delete_subscription(self, subscription: Union[KickSubscription, str]):
        subscription_id = subscription.id if isinstance(subscription, KickSubscription) else subscription
        response = await self._request(
            "/events/subscriptions",
            method="delete",
            params=[("id", subscription_id)],
        )
        if response.status != 204:
            await self._json(response, SubscriptionError)
        return response

    async def get_public_key(self, force: bool = False) -> str:
        if self.public_key and not force:
            return self.public_key
        response = await self._request("/public-key")
        data = await self._json(response)
        self.public_key = data.get("data", {}).get("public_key")
        if not self.public_key:
            raise BadAuthorization("Kick did not return its webhook public key")
        return self.public_key
