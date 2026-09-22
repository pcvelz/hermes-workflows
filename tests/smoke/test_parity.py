"""Every `.example` names its own source of truth, right at the top.

A template gets copied out of this repo — into a runtime home, into another
project — edited there, and then nobody remembers which file it came from, so
the copy drifts from the version that keeps getting fixed here. Every
`.example` therefore carries ONE line:

    Source of truth: https://github.com/pcvelz/hermes-workflows/blob/main/<own path>

`tests/static/check-origin-headers.sh` already checks the line exists
somewhere in the first 8 lines. This test is stricter: the line must sit
right at the top — line 1, or line 2 if line 1 is a real prologue line that
has to come first. "Real prologue line" is deliberately narrow, so a file
cannot bury the header behind an arbitrary banner:

  * `#!...`            — a shebang MUST be line 1 (`.sh.example` / `.example.sh`).
  * `# @user-gated`     — the hard-gate marker MUST be line 1 when present.
  * `<!-- @user-gated -->` — same marker, HTML-comment form.

Two additional, explicitly documented exceptions (matching what the static
check already accepts, so the two checks never disagree):

  * JSON (`*.json.example` / `*.example.json`) has no comment syntax at all,
    so it cannot carry the header without changing the config a loader
    reads (docs/board-design.md D7). The static check skips JSON outright;
    this test does the same.
  * `.plist.example` (launchd) is XML with a MANDATORY two-line prologue —
    the `<?xml ...?>` declaration on line 1 and the `<!DOCTYPE ...>` on
    line 2 — required for the file to be a valid property list at all. That
    prologue is the plist equivalent of a shebang, so the header is allowed
    on line 3, immediately after it.
"""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
URL_BASE = "https://github.com/pcvelz/hermes-workflows/blob/main/"


def tracked_example_files():
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True, check=True,
    ).stdout
    for rel in out.decode().split("\0"):
        if rel and (rel.endswith(".example") or ".example." in rel):
            yield rel


def is_json(rel):
    return rel.endswith(".json.example") or rel.endswith(".example.json")


def is_plist(rel):
    return rel.endswith(".plist.example") or rel.endswith(".example.plist")


def is_shell(rel):
    return rel.endswith(".sh.example") or rel.endswith(".example.sh")


class OriginHeaderParity(unittest.TestCase):
    """The origin header sits at the very top of every non-JSON .example."""

    def test_every_example_names_its_own_source_on_top(self):
        expected_marker = "Source of truth: " + URL_BASE
        violations = []
        checked = 0
        skipped = 0

        for rel in sorted(tracked_example_files()):
            if is_json(rel):
                # D7: JSON has no comments — documented exception, matches
                # tests/static/check-origin-headers.sh.
                skipped += 1
                continue

            checked += 1
            path = REPO / rel
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            expected_line = f"Source of truth: {URL_BASE}{rel}"

            # Exactly one occurrence of the marker anywhere in the file.
            occurrences = sum(1 for ln in lines if expected_marker in ln)
            if occurrences == 0:
                violations.append(f"{rel}: missing origin line")
                continue
            if occurrences > 1:
                violations.append(
                    f"{rel}: origin line appears {occurrences} times, expected exactly 1"
                )
                continue

            # It must point at the file's OWN path.
            matching = [ln for ln in lines if expected_marker in ln]
            if expected_line not in matching[0]:
                violations.append(
                    f"{rel}: origin line does not point at its own path: {matching[0].strip()}"
                )
                continue

            # Position: line 1, or line 2 after a real prologue line, or
            # (plist only) line 3 after the mandatory 2-line XML prologue.
            idx = next(i for i, ln in enumerate(lines) if expected_marker in ln)

            if is_plist(rel):
                allowed_idx = 2  # 0-based -> line 3
                reason = "line 3 (after the mandatory <?xml?> + <!DOCTYPE> prologue)"
            elif idx == 0:
                continue  # line 1, always fine
            else:
                line1 = lines[0] if lines else ""
                prologue_ok = (
                    (is_shell(rel) and line1.startswith("#!"))
                    or line1 == "# @user-gated"
                    or line1 == "<!-- @user-gated -->"
                )
                if prologue_ok:
                    allowed_idx = 1  # 0-based -> line 2
                    reason = "line 2 (after the required line-1 prologue)"
                else:
                    allowed_idx = 0
                    reason = "line 1"

            if idx != allowed_idx:
                violations.append(
                    f"{rel}: origin line is on line {idx + 1}, expected {reason}"
                )

        self.assertEqual(
            violations,
            [],
            "origin header not at the top for:\n  " + "\n  ".join(violations)
            + f"\n\n(checked {checked} non-JSON .example files, skipped {skipped} JSON)",
        )


if __name__ == "__main__":
    unittest.main()
