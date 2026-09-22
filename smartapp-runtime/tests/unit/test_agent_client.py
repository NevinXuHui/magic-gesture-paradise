import unittest

from examples.agent_client import _matches_subscription


class AgentClientTests(unittest.TestCase):
    def test_subscription_filters_event_data_type_and_app(self):
        event = {
            "event": "app_data",
            "appId": "rock_paper_scissors",
            "dataType": "game_result",
            "data": {"outcome": "win"},
        }
        self.assertTrue(_matches_subscription(
            event, "app_data", "game_result", "rock_paper_scissors"
        ))
        self.assertFalse(_matches_subscription(
            event, "app_data", "game_result", "cloud_show_display"
        ))
        self.assertFalse(_matches_subscription(
            event, "app_data", "word", "rock_paper_scissors"
        ))

    def test_empty_subscription_filters_accept_every_event(self):
        self.assertTrue(_matches_subscription({"event": "command_result"}, None, None, None))


if __name__ == "__main__":
    unittest.main()
