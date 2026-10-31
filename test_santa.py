"""Run with: python3 -m unittest -v"""
import io
import os
import random
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import santa

CONFIG = """
year = 2026
[rules]
avoid_repeats_years = 1
exclude = [["alice", "Bob"]]
[gift]
budget = "$75–$85"
[email]
sender = "organizer@example.com"
"""

PEOPLE = """name,email,backup_email
# a comment
Alice,alice@example.com,
Bob,bob@example.com,bob2@example.com

Carol,carol@example.com,
Dave,dave@example.com
Zoë,zoe@example.com,
"""


def person(name):
    return santa.Person(name, (f"{name.lower()}@example.com",))


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TempProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "santa.toml").write_text(CONFIG, encoding="utf-8")
        (self.dir / "participants.csv").write_text(PEOPLE, encoding="utf-8")
        self.smtp = FakeSMTP()
        patches = [mock.patch.object(santa, "connect", return_value=self.smtp),
                   mock.patch.dict(os.environ, {"SANTA_PASSWORD": "x"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = santa.main(["--config", str(self.dir / "santa.toml"), *args])
        return code, out.getvalue()

    @property
    def cfg(self):
        return santa.load_config(self.dir / "santa.toml")


class DrawTests(unittest.TestCase):
    def test_never_self_or_blocked(self):
        people = [person(n) for n in "ABCDE"]
        blocked = frozenset({("a", "b"), ("b", "a"), ("c", "d")})
        for _ in range(2000):
            for g, r in santa.draw(people, blocked, rand=random.Random()):
                self.assertNotEqual(g.key, r.key)
                self.assertNotIn((g.key, r.key), blocked)

    def test_everyone_gives_and_receives_once(self):
        people = [person(n) for n in "ABCDEFG"]
        pairs = santa.draw(people)
        self.assertEqual(sorted(g.name for g, _ in pairs), list("ABCDEFG"))
        self.assertEqual(sorted(r.name for _, r in pairs), list("ABCDEFG"))

    def test_uniform_without_extra_rules(self):
        people = [person(n) for n in "ABCD"]
        rand = random.Random(1)
        counts = Counter((g.name, r.name) for _ in range(30_000) for g, r in santa.draw(people, rand=rand))
        for n in counts.values():  # each of 12 pairs expected 10,000 times
            self.assertAlmostEqual(n / 10_000, 1, delta=0.05)

    def test_impossible_rules_explained(self):
        people = [person("A"), person("B"), person("C")]
        blocked = frozenset({("a", "b"), ("a", "c")})
        with self.assertRaisesRegex(santa.SantaError, "A isn't allowed to draw anyone"):
            santa.draw(people, blocked)

    def test_too_tight_rules_give_up(self):
        # Everyone may draw someone, but no full assignment exists: A and B can only draw C.
        people = [person("A"), person("B"), person("C")]
        past = frozenset({("a", "b"), ("b", "a")})
        with self.assertRaisesRegex(santa.SantaError, "No valid draw"):
            santa.draw(people, past, max_attempts=500)


class LoadingTests(TempProject):
    def test_participants(self):
        people = santa.load_people(self.dir / "participants.csv")
        self.assertEqual([p.name for p in people], ["Alice", "Bob", "Carol", "Dave", "Zoë"])
        self.assertEqual(people[1].emails, ("bob@example.com", "bob2@example.com"))
        self.assertEqual(people[3].emails, ("dave@example.com",))

    def test_exclusions_both_directions_and_checked(self):
        people = santa.load_people(self.dir / "participants.csv")
        self.assertEqual(santa.excluded_pairs(self.cfg, people), {("alice", "bob"), ("bob", "alice")})
        cfg = santa.Config(**{**self.cfg.__dict__, "exclude": (("Alice", "Bobby"),)})
        with self.assertRaisesRegex(santa.SantaError, "'Bobby' isn't in the participants"):
            santa.excluded_pairs(cfg, people)

    def test_bad_exclude_format(self):
        (self.dir / "santa.toml").write_text(CONFIG.replace('[["alice", "Bob"]]', '["Alice", "Bob"]'), encoding="utf-8")
        with self.assertRaisesRegex(santa.SantaError, "name pairs"):
            self.cfg

    def test_duplicate_names_rejected(self):
        path = self.dir / "dupes.csv"
        path.write_text("name,email\nAl,a@x.com\nal ,b@x.com\nC,c@x.com\n", encoding="utf-8")
        with self.assertRaisesRegex(santa.SantaError, "Duplicate"):
            santa.load_people(path)

    def test_bad_email_and_missing_header(self):
        path = self.dir / "bad.csv"
        path.write_text("name,email\nAl,not-an-email\n", encoding="utf-8")
        with self.assertRaisesRegex(santa.SantaError, "line 2"):
            santa.load_people(path)
        path.write_text("Al,a@x.com\n", encoding="utf-8")
        with self.assertRaisesRegex(santa.SantaError, "header"):
            santa.load_people(path)

    def test_seal_round_trip_hides_names(self):
        path = self.dir / "draws" / "2025.json"
        santa.save_draw(path, 2025, {"Alice": "Zoë"})
        self.assertNotIn("Zoë", path.read_text(encoding="utf-8"))
        self.assertEqual(santa.load_draw(path), {"Alice": "Zoë"})

    def test_parse_old_backup_last_run_wins(self):
        text = ("Alice is buying a gift for Bob.\nBob is buying a gift for Alice.\n"
                "Alice is buying a gift for Carol.\nBob is buying a gift for Dave.\n")
        self.assertEqual(santa.parse_old_backup(text), {"Alice": "Carol", "Bob": "Dave"})


class MessageTests(TempProject):
    def test_message_handles_unicode_and_headers(self):
        msg = santa.build_message(self.cfg, person("Zoë"), "José")
        self.assertEqual(msg.get_content().strip(), "You (Zoë) will be buying a Secret Santa gift for José. "
                                                    "This year's price range is $75–$85.")
        self.assertEqual(msg["Subject"], "Secret Santa 2026")
        self.assertEqual(msg["To"], "zoë@example.com")
        self.assertIn("organizer@example.com", msg["From"])
        msg.as_bytes()  # must serialize without UnicodeEncodeError


class CommandTests(TempProject):
    def test_check_reveals_nothing(self):
        code, out = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("5 people", out)
        self.assertEqual(self.smtp.sent, [])
        self.assertFalse((self.dir / "draws").exists())

    def test_send_then_refuse_then_resend(self):
        code, out = self.run_cli("send", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.smtp.sent), 5)
        bob = next(m for m in self.smtp.sent if "You (Bob)" in m.get_content())
        self.assertEqual(bob["To"], "bob@example.com, bob2@example.com")
        self.assertNotIn("Alice", bob.get_content().split("gift for ")[1].split(".")[0])  # excluded pair

        sent_lines = [line for line in out.splitlines() if line.startswith("  ✓")]
        self.assertEqual(sent_lines, [f"  ✓ {n}" for n in ["Alice", "Bob", "Carol", "Dave", "Zoë"]])
        matches = santa.load_draw(self.dir / "draws" / "2026.json")
        self.assertEqual(len(matches), 5)

        code, _ = self.run_cli("send", "--yes")
        self.assertEqual(code, 1)  # already drawn this year
        self.assertEqual(len(self.smtp.sent), 5)

        code, _ = self.run_cli("resend", "carol", "--yes")
        self.assertEqual(code, 0)
        self.assertIn(f"gift for {matches['Carol']}.", self.smtp.sent[-1].get_content())

    def test_yearly_record_keeps_gift_details(self):
        self.run_cli("send", "--yes")
        path = self.dir / "draws" / "2026.json"
        record = santa.read_record(path)
        self.assertEqual((record["budget"], record["people"]), ("$75–$85", 5))
        self.assertIn("$75–$85", path.read_text(encoding="utf-8"))  # readable, not escaped

        # Later config edits don't change what a resend says.
        (self.dir / "santa.toml").write_text(CONFIG.replace("$75–$85", "$20"), encoding="utf-8")
        self.run_cli("resend", "Dave", "--yes")
        self.assertIn("price range is $75–$85.", self.smtp.sent[-1].get_content())

    def test_history_shows_years_not_matches(self):
        santa.save_draw(self.dir / "draws" / "2025.json", 2025, {"Alice": "Carol", "Carol": "Alice"},
                        {"budget": "$50"})
        self.run_cli("send", "--yes")
        code, out = self.run_cli("history")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["2025: 2 people, $50", "2026: 5 people, $75–$85"])

    def test_last_year_not_repeated(self):
        # Force a known 2025 draw, then check 2026 never repeats any of it.
        last = {"Alice": "Carol", "Bob": "Dave", "Carol": "Zoë", "Dave": "Alice", "Zoë": "Bob"}
        santa.save_draw(self.dir / "draws" / "2025.json", 2025, last)
        past, years = santa.load_history(self.cfg)
        self.assertEqual(years, [2025])
        people = santa.load_people(self.dir / "participants.csv")
        for _ in range(500):
            for g, r in santa.draw(people, past, rand=random.Random()):
                self.assertNotEqual(last[g.name], r.name)

    def test_import_old(self):
        backup = self.dir / "backup 2025.txt"
        backup.write_text("Alice is buying a gift for Carol.\nBob is buying a gift for Dave.\n", encoding="utf-8")
        code, out = self.run_cli("import-old", str(backup), "--year", "2025", "--budget", "$75-$85")
        self.assertEqual(code, 0)
        self.assertNotIn("Carol", out)
        self.assertEqual(santa.read_record(self.dir / "draws" / "2025.json")["budget"], "$75-$85")
        self.assertEqual(santa.load_draw(self.dir / "draws" / "2025.json"), {"Alice": "Carol", "Bob": "Dave"})

    def test_unseal(self):
        santa.save_draw(self.dir / "draws" / "2025.json", 2025, {"Alice": "Carol", "Carol": "Alice"})
        with mock.patch("builtins.input", return_value="y"):  # wrong word: needs 'unseal'
            code, out = self.run_cli("unseal", "2025")
        self.assertEqual(code, 1)
        self.assertNotIn("→", out)

        with mock.patch("builtins.input", return_value="unseal"):
            code, out = self.run_cli("unseal", "2025")
        self.assertEqual(code, 0)
        self.assertIn("Alice → Carol\nCarol → Alice", out)

        code, out = self.run_cli("unseal", "2025", "--name", "carol", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Carol → Alice")

        self.assertEqual(self.run_cli("unseal", "2025", "--name", "Dave", "--yes")[0], 1)  # not in that year
        self.assertEqual(self.run_cli("unseal", "--yes")[0], 1)  # no 2026 draw yet

    def test_stats_runs(self):
        code, out = self.run_cli("stats", "--runs", "2000")
        self.assertEqual(code, 0)
        self.assertIn("Alice → ", out)
        self.assertNotIn("Bob → Alice", out)  # excluded pair never appears


if __name__ == "__main__":
    unittest.main()
