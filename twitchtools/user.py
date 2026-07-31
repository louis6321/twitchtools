from datetime import datetime
from typing import Optional

from dateutil import parser

from .asset import Avatar, OfflineImage
from .enums import AlertOrigin, BroadcasterType, UserType


class PartialUser:
    def __init__(self, user_id, user_login, display_name, origin: Optional[AlertOrigin] = None):
        self.user_id: int = int(user_id)
        self.id: int = int(user_id)
        self.login: str = user_login
        self.name: str = user_login
        self.username: str = user_login
        self.display_name: str = display_name
        self.origin: Optional[AlertOrigin] = origin

    def __str__(self) -> str:
        return self.login
        
    def __repr__(self) -> str:
        return f'<User id={self.id} name={self.name!r}>'

    def __eq__(self, other):
        return isinstance(other, User) and self.id == other.id


class PartialYoutubeUser:
    def __init__(
        self,
        user_id: str,
        display_name: str,
        origin: Optional[AlertOrigin] = AlertOrigin.unavailable,
        video_id: Optional[str] = None,
    ):
        self.user_id: str = user_id
        self.id: str = user_id
        self.display_name: str = display_name
        self.origin: AlertOrigin = origin
        self.video_id: Optional[str] = video_id

    def __str__(self) -> str:
        return self.display_name

    def __repr__(self) -> str:
        return f'<User id={self.id} name={self.display_name!r}>'

    def __eq__(self, other):
        return isinstance(other, User) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class User(PartialUser):
    def __init__(self, id: int, login: str, display_name: str, type: UserType, broadcaster_type: BroadcasterType,
                description: str, profile_image_url: str, offline_image_url: str, view_count: int, created_at: str):
        super().__init__(id, login, display_name)
        self.user_type: UserType = UserType(type)
        self.type: UserType = UserType(type)
        self.broadcaster_type: BroadcasterType = BroadcasterType(broadcaster_type)
        self.user_description: Optional[str] = None if description == "" else description
        self.description = None if description == "" else description
        self.avatar: Optional[Avatar] = None if profile_image_url == "" else Avatar(profile_image_url)
        self.offline_image: Optional[OfflineImage] = None if offline_image_url == "" else OfflineImage(offline_image_url)
        self.view_count: int = int(view_count)
        self.created_at: datetime = parser.parse(created_at)


class YoutubeUser(PartialYoutubeUser):
    def __init__(self, id: str, snippet: dict, **kwargs):
        super().__init__(id, snippet["title"])
        self.description: str = snippet["description"]
        self.avatar_url: str = snippet["thumbnails"]["high"]["url"]


class PartialKickUser:
    def __init__(
        self,
        user_id: int,
        slug: str,
        display_name: str = None,
        profile_picture: str = None,
        origin: Optional[AlertOrigin] = AlertOrigin.unavailable,
        ended_at: str = None,
    ):
        self.user_id: int = int(user_id)
        self.id: int = self.user_id
        self.slug: str = slug
        self.login: str = slug
        self.name: str = slug
        self.username: str = slug
        self.display_name: str = display_name or slug
        self.profile_picture: Optional[str] = profile_picture
        self.avatar_url: Optional[str] = profile_picture
        self.avatar: Optional[str] = profile_picture
        self.origin: AlertOrigin = origin
        self.ended_at: Optional[datetime] = (
            parser.parse(ended_at) if ended_at else None
        )

    def __str__(self) -> str:
        return self.slug

    def __repr__(self) -> str:
        return f'<KickUser id={self.id} slug={self.slug!r}>'

    def __eq__(self, other):
        return isinstance(other, PartialKickUser) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


class KickUser(PartialKickUser):
    def __init__(
        self,
        user_id: int,
        slug: str,
        name: str = None,
        profile_picture: str = None,
        channel_description: str = None,
        banner_picture: str = None,
        **kwargs,
    ):
        super().__init__(
            user_id,
            slug,
            display_name=name or slug,
            profile_picture=profile_picture,
            **kwargs,
        )
        self.description: Optional[str] = channel_description or None
        self.banner_picture: Optional[str] = banner_picture or None
