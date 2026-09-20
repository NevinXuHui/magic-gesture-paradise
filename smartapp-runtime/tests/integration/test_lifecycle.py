import ast
import unittest
from pathlib import Path


class LifecycleBoundaryTests(unittest.TestCase):
    def test_application_layer_does_not_import_concrete_adapters_or_infrastructure(self):
        application = Path(__file__).parents[2] / "src/smartapp_runtime/application"
        forbidden = []
        for source in application.glob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(("smartapp_runtime.adapters", "smartapp_runtime.infrastructure")):
                        forbidden.append((source.name, node.module))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(("smartapp_runtime.adapters", "smartapp_runtime.infrastructure")):
                            forbidden.append((source.name, alias.name))
        self.assertEqual(forbidden, [])


if __name__ == "__main__":
    unittest.main()
