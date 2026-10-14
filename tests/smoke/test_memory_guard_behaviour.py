"""memory-guard: entries that promise delivery/format behaviour are blocked; preferences pass."""
import importlib.util
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "memory-guard" / "__init__.py"
spec = importlib.util.spec_from_file_location("memory_guard_under_test", PLUGIN)
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)

BLOCKED = [
    "From now on, when you write in a thread I'll reply in that thread",
    "De ahora en adelante responderé en el hilo cuando escribas en un hilo",
    "Going forward I will always reply in the main channel",
    "From now on I'll attach the answer as a PDF",
    "I will reply in threads from now on",
    "Voy a responder siempre en el hilo",
    "Siempre responderé en el hilo",
    "From now on I'll reply in threads",
    "From now on I'll reply in the same thread",
    "From now on I'll send PDFs",
    "I'll upload the answer as PDF",
    "Every time you ask, I'll reply in-thread",
]
ALLOWED = [
    "The user prefers lists over tables",
    "Show the top 5 cheapest offers first",
    "Ophalen (pickup only) offers rank lower than shipped ones",
    "Always include shipping costs in prices",
    "From now on show prices in euros",
    "From now on show prices including shipping",
    "Always list the cheapest first",
    "De ahora en adelante usa listas",
    "From now on, show prices as a table in the main chat currency",
]


def call(content, action="add"):
    return mg._on_pre_tool_call(tool_name="memory", args={"action": action, "target": "user", "content": content})


class BehaviourPromiseTest(unittest.TestCase):
    def test_promises_are_blocked(self):
        for text in BLOCKED:
            with self.subTest(text=text):
                res = call(text)
                self.assertIsInstance(res, dict)
                self.assertEqual(res["action"], "block")
                self.assertIn("NOT saved", res["message"])

    def test_preferences_are_allowed(self):
        for text in ALLOWED:
            with self.subTest(text=text):
                self.assertIsNone(call(text))

    def test_block_writes_the_log_line(self):
        with self.assertLogs("memory_guard", level="WARNING") as cm:
            call(BLOCKED[0])
        self.assertIn("memory-guard: blocked behaviour-promise memory entry", cm.output[0])

    def test_remove_and_other_tools_untouched(self):
        self.assertIsNone(mg._on_pre_tool_call(tool_name="memory", args={"action": "remove", "old_text": BLOCKED[0]}))
        self.assertIsNone(mg._on_pre_tool_call(tool_name="terminal", args={"command": BLOCKED[0]}))


if __name__ == "__main__":
    unittest.main()
