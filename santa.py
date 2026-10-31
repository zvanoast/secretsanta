#!/usr/bin/env python3
"""Secret Santa: draw names fairly and email everyone their match.

Python 3.11+, standard library only. See README.md for the yearly routine.
"""
from __future__ import annotations

import argparse
import base64
import csv
import dataclasses
import datetime as dt
import difflib
import getpass
import json
import os
import random
import smtplib
import ssl
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "santa.toml"
MAX_ATTEMPTS = 100_000
secure_rng = random.SystemRandom()


class SantaError(Exception):
    """A problem the organizer can fix: bad config, impossible rules, login failure..."""


@dataclass(frozen=True)
class Person:
    name: str
    emails: tuple[str, ...]

    @property
    def key(self) -> str:
        return normalize(self.name)


@dataclass(frozen=True)
class Config:
    year: int
    participants: Path
    draws_dir: Path
    avoid_repeats_years: int
    exclude: tuple[tuple[str, str], ...]
    budget: str
    smtp_host: str
    smtp_port: int
    sender_email: str
    sender_name: str


def normalize(name: str) -> str:
    return " ".join(name.split()).casefold()


# ---------------------------------------------------------------- loading

def load_config(path: Path) -> Config:
    if not path.exists():
        raise SantaError(f"No config at {path}. Copy santa.example.toml to santa.toml and fill it in.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SantaError(f"{path.name}: {e}") from e
    rules, gift, email = raw.get("rules", {}), raw.get("gift", {}), raw.get("email", {})
    if not email.get("sender"):
        raise SantaError(f"{path.name}: set `sender` under [email]")
    exclude = rules.get("exclude", [])
    if not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(n, str) for n in pair)
               for pair in exclude):
        raise SantaError(f'{path.name}: `exclude` must be a list of name pairs, like [["Alice", "Bob"]]')
    base = path.parent
    return Config(
        year=int(raw.get("year", dt.date.today().year)),
        participants=base / raw.get("participants", "participants.csv"),
        draws_dir=base / raw.get("draws_dir", "draws"),
        avoid_repeats_years=int(rules.get("avoid_repeats_years", 1)),
        exclude=tuple((a, b) for a, b in exclude),
        budget=gift.get("budget", ""),
        smtp_host=email.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(email.get("smtp_port", 465)),
        sender_email=email["sender"],
        sender_name=email.get("sender_name", "Secret Santa"),
    )


def load_people(path: Path) -> list[Person]:
    """Read the participants CSV: a header row with name,email and optionally backup_email."""
    if not path.exists():
        raise SantaError(f"Participants file not found: {path}")
    people: list[Person] = []
    header: list[str] | None = None
    with path.open(newline="", encoding="utf-8-sig") as f:
        for lineno, row in enumerate(csv.reader(f), 1):
            cells = [c.strip() for c in row]
            if not any(cells) or cells[0].startswith("#"):
                continue
            if header is None:
                header = [c.lower() for c in cells]
                missing = {"name", "email"} - set(header)
                if missing:
                    raise SantaError(f"{path.name} line {lineno}: header row needs column(s) "
                                     f"{', '.join(sorted(missing))} (see participants.example.csv)")
                continue
            rec = dict(zip(header, cells))
            if not rec.get("name") or not rec.get("email"):
                raise SantaError(f"{path.name} line {lineno}: every row needs a name and an email")
            emails = tuple(e for e in (rec["email"], rec.get("backup_email", "")) if e)
            for e in emails:
                if "@" not in e or " " in e:
                    raise SantaError(f"{path.name} line {lineno}: '{e}' doesn't look like an email address")
            people.append(Person(rec["name"], emails))

    counts = Counter(p.key for p in people)
    dupes = sorted({p.name for p in people if counts[p.key] > 1})
    if dupes:
        raise SantaError(f"Duplicate names: {', '.join(dupes)}. Add a last initial so everyone is unique.")
    if len(people) < 3:
        raise SantaError("Need at least 3 people for a Secret Santa.")
    return people


# ---------------------------------------------------------------- drawing

Blocked = frozenset[tuple[str, str]]  # (giver key, receiver key) pairs that aren't allowed


def can_give(giver: Person, receiver: Person, blocked: Blocked = frozenset()) -> bool:
    return giver.key != receiver.key and (giver.key, receiver.key) not in blocked


def excluded_pairs(cfg: Config, people: list[Person]) -> Blocked:
    """The `exclude` pairs from the config, blocked in both directions."""
    known = {p.key for p in people}
    pairs = set()
    for a, b in cfg.exclude:
        for name in (a, b):
            if normalize(name) not in known:
                raise SantaError(f"Excluded name '{name}' isn't in the participants file.")
        pairs |= {(normalize(a), normalize(b)), (normalize(b), normalize(a))}
    return frozenset(pairs)


def draw(people: list[Person], blocked: Blocked = frozenset(), *,
         rand: random.Random = secure_rng, max_attempts: int = MAX_ATTEMPTS) -> list[tuple[Person, Person]]:
    """Return (giver, receiver) pairs. Every assignment that follows the rules is equally likely.

    Same method as the original script: shuffle, and reshuffle if any pair breaks a rule.
    """
    for p in people:
        if not any(can_give(p, r, blocked) for r in people):
            raise SantaError(f"{p.name} isn't allowed to draw anyone. Loosen the rules "
                             "(exclude or avoid_repeats_years).")
        if not any(can_give(g, p, blocked) for g in people):
            raise SantaError(f"Nobody is allowed to draw {p.name}. Loosen the rules "
                             "(exclude or avoid_repeats_years).")
    receivers = list(people)
    for _ in range(max_attempts):
        rand.shuffle(receivers)
        if all(can_give(g, r, blocked) for g, r in zip(people, receivers)):
            return list(zip(people, receivers))
    raise SantaError(f"No valid draw in {max_attempts:,} tries; the rules are probably too tight for this "
                     "group. Try avoid_repeats_years = 0, or fewer excluded pairs.")


# ---------------------------------------------------------------- sealed draws

def draw_path(cfg: Config, year: int) -> Path:
    return cfg.draws_dir / f"{year}.json"


GIFT_FIELDS = ("budget",)


def save_draw(path: Path, year: int, matches: dict[str, str], gift: dict[str, str] | None = None) -> None:
    """Save the year's record: gift details in the clear, and giver -> receiver names sealed.

    Sealing is base64, not encryption; it just keeps the organizer from seeing the matches at a glance.
    """
    gift = gift or {}
    sealed = base64.b64encode(json.dumps(matches, ensure_ascii=False).encode()).decode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "year": year,
        "saved": dt.datetime.now().isoformat(timespec="seconds"),
        "people": len(matches),
        **{field: gift.get(field, "") for field in GIFT_FIELDS},
        "about": "Matches are sealed so they aren't seen by accident. Use `santa.py resend NAME`, not a decoder.",
        "sealed": sealed,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def gift_details(cfg: Config) -> dict[str, str]:
    return {field: getattr(cfg, field) for field in GIFT_FIELDS}


def read_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_draw(path: Path) -> dict[str, str]:
    return json.loads(base64.b64decode(read_record(path)["sealed"]))


def history_paths(cfg: Config) -> dict[int, Path]:
    """The saved draws from the last `avoid_repeats_years` years."""
    paths = {year: draw_path(cfg, year) for year in range(cfg.year - cfg.avoid_repeats_years, cfg.year)}
    return {year: path for year, path in paths.items() if path.exists()}


def load_history(cfg: Config, renames: dict[str, str] | None = None) -> tuple[Blocked, list[int]]:
    """Pairs from recent draws, which can't happen again this year. `renames` maps an old name's key to a new one's."""
    renames = renames or {}
    pairs: set[tuple[str, str]] = set()
    paths = history_paths(cfg)
    for path in paths.values():
        for g, r in load_draw(path).items():
            pairs.add((renames.get(normalize(g), normalize(g)), renames.get(normalize(r), normalize(r))))
    return frozenset(pairs), list(paths)


def renamed_suspects(cfg: Config, people: list[Person]) -> list[tuple[str, Person]]:
    """(old name, current person) where a recent draw has a name that's gone and looks like a newcomer's.

    "Rob" last year and "Bob" this year might be one person spelled two ways, which would let a repeat slip by.
    Only newcomers are suggested: if last year already had a Bob, Rob is someone else.
    """
    past = {normalize(n): n for path in history_paths(cfg).values() for pair in load_draw(path).items() for n in pair}
    newcomers = {p.key: p for p in people if p.key not in past}
    current = {p.key for p in people}
    suspects = []
    for key in sorted(k for k in past if k not in current):
        close = difflib.get_close_matches(key, list(newcomers), n=1, cutoff=0.6)
        if close:
            suspects.append((past[key], newcomers.pop(close[0])))
    return suspects


# ---------------------------------------------------------------- email

def build_message(cfg: Config, giver: Person, receiver_name: str, *, test: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{'[TEST] ' if test else ''}Secret Santa {cfg.year}"
    msg["From"] = formataddr((cfg.sender_name, cfg.sender_email))
    msg["To"] = ", ".join(giver.emails)
    text = f"You ({giver.name}) will be buying a Secret Santa gift for {receiver_name}."
    if cfg.budget:
        text += f" This year's price range is {cfg.budget}."
    msg.set_content(text)
    return msg


def get_password(cfg: Config) -> str:
    password = os.environ.get("SANTA_PASSWORD") or getpass.getpass(f"App password for {cfg.sender_email}: ")
    # Gmail shows app passwords in groups of four ("abcd efgh ..."); the spaces aren't part of it.
    return password.replace(" ", "") if "gmail" in cfg.smtp_host else password


def connect(cfg: Config, password: str) -> smtplib.SMTP:
    context = ssl.create_default_context()
    server = None
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            server.starttls(context=context)
        server.login(cfg.sender_email, password)
        return server
    except smtplib.SMTPAuthenticationError as e:
        if server:
            server.close()
        raise SantaError("Login failed. Gmail needs an app password, not your normal one: "
                         "https://myaccount.google.com/apppasswords (requires 2-Step Verification).") from e
    except (OSError, smtplib.SMTPException) as e:
        if server:
            server.close()
        raise SantaError(f"Couldn't connect to {cfg.smtp_host}:{cfg.smtp_port}: {e}") from e


def confirm(prompt: str, word: str = "y") -> bool:
    return input(prompt).strip().lower() == word


def ask_renames(cfg: Config, people: list[Person], *, ask: bool = True) -> dict[str, str]:
    """Ask about past names that look like a current one, so a respelled name can't dodge the no-repeat rule."""
    renames = {}
    for old, person in renamed_suspects(cfg, people):
        question = (f"A recent draw has '{old}', who isn't in {cfg.participants.name}. "
                    f"Is that the same person as '{person.name}'?")
        if ask and confirm(f"{question} [y/N] "):
            renames[normalize(old)] = person.key
        elif not ask:
            print(f"Warning: {question} If so, fix the spelling or last year's match could repeat.")
    return renames


# ---------------------------------------------------------------- commands

def cmd_check(cfg: Config, args) -> int:
    people = load_people(cfg.participants)
    excluded = excluded_pairs(cfg, people)
    past, years = load_history(cfg, ask_renames(cfg, people))
    print(f"{len(people)} people, {len(cfg.exclude)} excluded pair(s)")
    if cfg.avoid_repeats_years:
        found = ", ".join(map(str, years)) or "none found"
        print(f"Avoiding repeats from the last {cfg.avoid_repeats_years} year(s): {found}")
    draw(people, excluded | past)
    print("✓ A valid draw exists. (A practice draw was made and thrown away: nothing saved, sent or shown.)")
    if draw_path(cfg, cfg.year).exists():
        print(f"Note: the {cfg.year} draw has already been sent. Use `resend NAME` for lost emails.")
    return 0


def cmd_test(cfg: Config, args) -> int:
    me = Person(cfg.sender_name, (cfg.sender_email,))
    msg = build_message(cfg, me, "a sample name (this is only a test)", test=True)
    with connect(cfg, get_password(cfg)) as server:
        server.send_message(msg)
    print(f"✓ Sent a sample email to {cfg.sender_email}. Check it looks right.")
    return 0


def cmd_send(cfg: Config, args) -> int:
    people = load_people(cfg.participants)
    path = draw_path(cfg, cfg.year)
    if path.exists() and not args.redraw:
        raise SantaError(f"The {cfg.year} draw was already sent. Use `resend NAME` for a lost email, "
                         "or `send --redraw` to start over (everyone gets a new match).")
    excluded = excluded_pairs(cfg, people)
    past, _ = load_history(cfg, ask_renames(cfg, people, ask=not args.yes))
    pairs = draw(people, excluded | past)

    print(f"Ready to email {len(people)} people their {cfg.year} match.")
    if not args.yes and not confirm("Type 'send' to go: ", "send"):
        print("Cancelled: nothing saved or sent.")
        return 1

    failed = []
    # Log in before saving, so a wrong password doesn't leave a saved-but-unsent draw.
    with connect(cfg, get_password(cfg)) as server:
        save_draw(path, cfg.year, {g.name: r.name for g, r in pairs}, gift_details(cfg))
        for giver, receiver in pairs:
            try:
                server.send_message(build_message(cfg, giver, receiver.name))
                print(f"  ✓ {giver.name}")
            except (smtplib.SMTPException, OSError) as e:  # OSError: timeouts and dropped connections
                failed.append(giver.name)
                print(f"  ✗ {giver.name}: {e}")

    print(f"Draw saved to {path} (sealed).")
    if failed:
        print("Some emails failed. Fix the problem, then for each one run: python3 santa.py resend NAME")
        return 1
    print("Done. Merry Christmas! 🎄")
    return 0


def cmd_resend(cfg: Config, args) -> int:
    path = draw_path(cfg, cfg.year)
    if not path.exists():
        raise SantaError(f"No {cfg.year} draw found at {path}.")
    matches = {normalize(g): r for g, r in load_draw(path).items()}
    giver = next((p for p in load_people(cfg.participants) if p.key == normalize(args.name)), None)
    if giver is None or giver.key not in matches:
        names = ", ".join(sorted(load_draw(path)))
        raise SantaError(f"'{args.name}' isn't in both the {cfg.year} draw and the participants file. "
                         f"Names in the draw: {names}")
    if not args.yes and not confirm(f"Re-send {giver.name}'s match to {', '.join(giver.emails)}? [y/N] "):
        print("Cancelled.")
        return 1
    # Re-send what everyone was told, even if santa.toml has changed since.
    record = read_record(path)
    as_sent = dataclasses.replace(cfg, **{f: record[f] for f in GIFT_FIELDS if f in record})
    with connect(cfg, get_password(cfg)) as server:
        server.send_message(build_message(as_sent, giver, matches[giver.key]))
    print(f"✓ Re-sent to {giver.name}.")
    return 0


def cmd_unseal(cfg: Config, args) -> int:
    year = args.year or cfg.year
    path = draw_path(cfg, year)
    if not path.exists():
        raise SantaError(f"No {year} draw found at {path}.")
    matches = load_draw(path)
    if args.name:
        # Past years had different people, so look the name up in that year's draw, not participants.csv.
        giver = next((g for g in matches if normalize(g) == normalize(args.name)), None)
        if giver is None:
            raise SantaError(f"'{args.name}' isn't in the {year} draw. Names in the draw: {', '.join(sorted(matches))}")
        if not args.yes and not confirm(f"Show {giver}'s {year} match? [y/N] "):
            print("Cancelled.")
            return 1
        print(f"{giver} → {matches[giver]}")
        return 0
    print(f"This shows all {len(matches)} matches for {year}, yours included. "
          "For one lost email, `resend NAME` is better: it sends without showing you.")
    if not args.yes and not confirm("Type 'unseal' to show them: ", "unseal"):
        print("Cancelled: nothing shown.")
        return 1
    for giver in sorted(matches, key=normalize):
        print(f"{giver} → {matches[giver]}")
    return 0


def cmd_stats(cfg: Config, args) -> int:
    if args.runs < 1:
        raise SantaError("--runs must be at least 1.")
    people = load_people(cfg.participants)
    blocked = excluded = excluded_pairs(cfg, people)
    # The real history is left out on purpose: showing which pairs are blocked would reveal last year's draw.
    rand = random.Random()
    if args.fake_last_year:
        fake = draw(people, excluded, rand=rand)
        blocked = excluded | {(g.key, r.key) for g, r in fake}
        print("Made-up last year (random, NOT the real one), which this year's draws must avoid:")
        print("  " + ", ".join(f"{g.name} → {r.name}" for g, r in fake))
    counts: Counter[tuple[str, str]] = Counter()
    for _ in range(args.runs):
        for g, r in draw(people, blocked, rand=rand):
            counts[g.key, r.key] += 1
    rules = "your exclusions" + (" and the made-up last year" if args.fake_last_year else "")
    print(f"{args.runs:,} simulated draws with {rules} (nothing saved or sent).")
    if not blocked:
        print(f"With no exclusions, every pairing should land near {1 / (len(people) - 1):.1%}.")
    print()
    for g in people:
        cells = [f"{r.name} {counts[g.key, r.key] / args.runs:.1%}" for r in people if can_give(g, r, blocked)]
        print(f"{g.name} → " + ", ".join(cells))
    return 0


def cmd_history(cfg: Config, args) -> int:
    paths = sorted(cfg.draws_dir.glob("*.json"))
    if not paths:
        print(f"No draws saved in {cfg.draws_dir} yet.")
        return 0
    for path in paths:
        record = read_record(path)
        budget = record.get("budget") or "budget not recorded"
        print(f"{record.get('year', path.stem)}: {len(load_draw(path))} people, {budget}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows pipes default to cp1252, which can't print ✓ (a crash mid-send would leave it half done).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Draw Secret Santa names and email everyone their match.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config file (default: santa.toml)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("check", help="validate the setup with a practice draw (nothing saved, sent or shown)")
    sub.add_parser("test", help="email yourself a sample, to check the login and how it looks")
    p = sub.add_parser("send", help="draw for real, save the sealed draw and email everyone")
    p.add_argument("--redraw", action="store_true", help="replace this year's draw (everyone gets a new match)")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("resend", help="re-send one person's email from this year's draw")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("unseal", help="show a year's matches (last resort: they're no longer secret to you)")
    p.add_argument("year", type=int, nargs="?", help="which year (default: this year)")
    p.add_argument("--name", help="show only this person's match")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("stats", help="simulate many draws to show they're fair")
    p.add_argument("--runs", type=int, default=100_000)
    p.add_argument("--fake-last-year", action="store_true",
                   help="also avoid a made-up random last year, to see how no-repeats shifts the odds")
    sub.add_parser("history", help="list past years: head count and price range (never the matches)")
    args = parser.parse_args(argv)

    commands = {"check": cmd_check, "test": cmd_test, "send": cmd_send, "resend": cmd_resend, "unseal": cmd_unseal,
                "stats": cmd_stats, "history": cmd_history}
    try:
        return commands[args.command](load_config(args.config), args)
    except SantaError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
