"""One human lane, one name.

The person at the end of the board is the role `user`. An older name for the
same lane kept coming back in prose, configs and diagrams; two names for one
lane is the same defect as a raw status on a column. This scan walks every
tracked file, so the next file cannot bring the old name back unnoticed.
"""
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The retired names of the human lane. "architecture" and "architectural"
#: are ordinary words and do not match (word boundary).
RETIRED_ROLE_NAMES = ("architect",)


def tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    for rel in out.decode().split("\0"):
        path = REPO / rel
        if rel and path.is_file():
            yield rel, path


class RoleVocabulary(unittest.TestCase):

    def test_no_retired_name_for_the_human_lane(self):
        pattern = re.compile(r"\b(" + "|".join(RETIRED_ROLE_NAMES) + r")s?\b", re.I)
        hits = []
        for rel, path in tracked_files():
            if rel == "tests/smoke/test_vocabulary.py":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertEqual(hits, [], "the human lane is called `user`; retired name found:\n"
                         + "\n".join(hits))


class CriterionOutcomes(unittest.TestCase):
    """A hand-off reports each criterion as met, not met, or could not check
    (docs/evidence.md). Every place that tells a worker what a hand-off
    carries says so, so the third outcome cannot quietly drop out of one."""

    def test_every_hand_off_instruction_names_could_not_check(self):
        import yaml
        board = yaml.safe_load((REPO / "config" / "board.yaml").read_text())
        comments = (board.get("board", board) or {}).get("comments") \
            or board.get("comments") or {}
        self.assertEqual(comments.get("criterion_outcomes"),
                         ["met", "not met", "could not check"])
        for rel in ("AGENTS.md", "plugins/kanban-harness/__init__.py", "docs/evidence.md"):
            with self.subTest(rel):
                self.assertIn("could not check", (REPO / rel).read_text())


if __name__ == "__main__":
    unittest.main()
