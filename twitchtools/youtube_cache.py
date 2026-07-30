from collections.abc import MutableMapping


YOUTUBE_STREAM_STATE_KEYS = (
    "alert_cooldown",
    "live_channels",
    "live_alerts",
    "last_update",
    "triggered_guilds",
)


def get_youtube_stream_states(channel_cache: MutableMapping) -> MutableMapping:
    """Return per-video live state, migrating the old single-video cache in memory."""
    streams = channel_cache.get("streams")
    if isinstance(streams, MutableMapping):
        return streams

    streams = {}
    video_id = channel_cache.get("video_id")
    if channel_cache.get("is_live", False) and video_id:
        stream_state = {
            key: channel_cache[key]
            for key in YOUTUBE_STREAM_STATE_KEYS
            if key in channel_cache
        }
        stream_state["video_id"] = str(video_id)
        streams[str(video_id)] = stream_state

    channel_cache["streams"] = streams
    return streams


def sync_youtube_channel_cache(
    channel_cache: MutableMapping, streams: MutableMapping
) -> None:
    """Store aggregate channel state and remove obsolete single-stream fields."""
    channel_cache["streams"] = streams
    channel_cache["is_live"] = bool(streams)
    for key in ("video_id", *YOUTUBE_STREAM_STATE_KEYS):
        channel_cache.pop(key, None)
