import importlib.util
import unittest
from pathlib import Path


_client_path = Path(__file__).resolve().parents[2] / "examples" / "agent_client.py"
_client_spec = importlib.util.spec_from_file_location("agent_client_example", _client_path)
_client_module = importlib.util.module_from_spec(_client_spec)
_client_spec.loader.exec_module(_client_module)
_matches_subscription = _client_module._matches_subscription


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
