import unittest
from types import SimpleNamespace

from cogs.state_manager import StreamStateManager
from twitchtools import YoutubeVideo


class SlackNotificationTests(unittest.TestCase):
    def test_twitch_live_message_format(self):
        stream = SimpleNamespace(
            user=SimpleNamespace(
                display_name="EWC_stcArena_EN",
                username="ewc_stcarena_en",
            ),
            title="OWCS MSC at EWC 26 - Day 1 - Group Stage - LIVE",
        )

        message = StreamStateManager.get_slack_live_message(stream)

        self.assertEqual(
            ":li::ve: *EWC_stcArena_EN* is live on Twitch! "
            ":twitch: `https://twitch.tv/ewc_stcarena_en`\n"
            "OWCS MSC at EWC 26 - Day 1 - Group Stage - LIVE",
            message,
        )

    def test_youtube_live_message_format(self):
        video = object.__new__(YoutubeVideo)
        video.id = "8vDJyAPSIfA"
        video.user = SimpleNamespace(display_name="Esports World Cup")
        video.title = "MSC at EWC 26 - Day 6 - Playoffs"

        message = StreamStateManager.get_slack_live_message(video)

        self.assertEqual(
            ":li::ve: *Esports World Cup* is live on YouTube! "
            ":youtube: `https://youtu.be/8vDJyAPSIfA`\n"
            "MSC at EWC 26 - Day 6 - Playoffs",
            message,
        )


if __name__ == "__main__":
    unittest.main()
