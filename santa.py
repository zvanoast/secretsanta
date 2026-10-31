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
import getpass
import json
import os
import random
import re
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
    exchange_date: str
    note: str
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
        exchange_date=gift.get("exchange_date", ""),
        note=gift.get("note", "").strip(),
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


GIFT_FIELDS = ("budget", "exchange_date", "note")


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


def load_history(cfg: Config) -> tuple[Blocked, list[int]]:
    """Pairs from the last `avoid_repeats_years` draws, which can't happen again this year."""
    pairs: set[tuple[str, str]] = set()
    years = []
    for year in range(cfg.year - cfg.avoid_repeats_years, cfg.year):
        path = draw_path(cfg, year)
        if path.exists():
            pairs |= {(normalize(g), normalize(r)) for g, r in load_draw(path).items()}
            years.append(year)
    return frozenset(pairs), years


OLD_BACKUP_LINE = re.compile(r"^(.+?) is buying a gift for (.+?)\.?$")


def parse_old_backup(text: str) -> dict[str, str]:
    """Read a 'backup YEAR.txt' from the old script. It appended on every run, so the last run wins."""
    matches: dict[str, str] = {}
    for line in text.splitlines():
        m = OLD_BACKUP_LINE.match(line.strip())
        if m:
            matches[m[1].strip()] = m[2].strip()
    return matches


# ---------------------------------------------------------------- email

def build_message(cfg: Config, giver: Person, receiver_name: str, *, test: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{'[TEST] ' if test else ''}Secret Santa {cfg.year} 🎅"
    msg["From"] = formataddr((cfg.sender_name, cfg.sender_email))
    msg["To"] = ", ".join(giver.emails)
    lines = [f"Hi {giver.name},", "", f"You're buying a Secret Santa gift for: {receiver_name}", ""]
    if cfg.budget:
        lines.append(f"Budget: {cfg.budget}")
    if cfg.exchange_date:
        lines.append(f"Gift exchange: {cfg.exchange_date}")
    if cfg.note:
        lines += ["", cfg.note]
    lines += ["", "Keep it secret! 🤫"]
    msg.set_content("\n".join(lines))
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


# ---------------------------------------------------------------- commands

def cmd_check(cfg: Config, args) -> int:
    people = load_people(cfg.participants)
    excluded = excluded_pairs(cfg, people)
    past, years = load_history(cfg)
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
    msg = build_message(cfg, me, "(a sample name; this is only a test)", test=True)
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
    past, _ = load_history(cfg)
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
            except smtplib.SMTPException as e:
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


def cmd_stats(cfg: Config, args) -> int:
    people = load_people(cfg.participants)
    excluded = excluded_pairs(cfg, people)
    # History is left out on purpose: showing which pairs are excluded would reveal last year's draw.
    rand = random.Random()
    counts: Counter[tuple[str, str]] = Counter()
    for _ in range(args.runs):
        for g, r in draw(people, excluded, rand=rand):
            counts[g.key, r.key] += 1
    print(f"{args.runs:,} simulated draws with your exclusions (nothing saved or sent).")
    if not excluded:
        print(f"With no exclusions, every pairing should land near {1 / (len(people) - 1):.1%}.")
    print()
    for g in people:
        cells = [f"{r.name} {counts[g.key, r.key] / args.runs:.1%}" for r in people if can_give(g, r, excluded)]
        print(f"{g.name} → " + ", ".join(cells))
    return 0


def cmd_import_old(cfg: Config, args) -> int:
    path = draw_path(cfg, args.year)
    if path.exists() and not args.force:
        raise SantaError(f"{path} already exists (use --force to replace it).")
    matches = parse_old_backup(args.file.read_text(encoding="utf-8"))
    if not matches:
        raise SantaError(f"No 'X is buying a gift for Y.' lines found in {args.file}.")
    save_draw(path, args.year, matches, {"budget": args.budget, "exchange_date": args.date})
    print(f"✓ Imported {len(matches)} matches for {args.year} into {path} (sealed, not shown).")
    print(f"You can delete {args.file.name} now; it has the matches in plain text.")
    return 0


def cmd_history(cfg: Config, args) -> int:
    paths = sorted(cfg.draws_dir.glob("*.json"))
    if not paths:
        print(f"No draws saved in {cfg.draws_dir} yet.")
        return 0
    for path in paths:
        record = read_record(path)
        details = [f"{len(load_draw(path))} people",
                   record.get("budget") or "budget not recorded",
                   record.get("exchange_date", "")]
        print(f"{record.get('year', path.stem)}: " + ", ".join(d for d in details if d))
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
    p = sub.add_parser("stats", help="simulate many draws to show they're fair")
    p.add_argument("--runs", type=int, default=100_000)
    p = sub.add_parser("import-old", help="import a 'backup YEAR.txt' from the old script")
    p.add_argument("file", type=Path)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--budget", default="", help='that year\'s budget, e.g. "$75–$85"')
    p.add_argument("--date", default="", help="that year's exchange date")
    p.add_argument("--force", action="store_true", help="replace an existing draw for that year")
    sub.add_parser("history", help="list past years: head count, budget and date (never the matches)")
    args = parser.parse_args(argv)

    commands = {"check": cmd_check, "test": cmd_test, "send": cmd_send, "resend": cmd_resend,
                "stats": cmd_stats, "import-old": cmd_import_old, "history": cmd_history}
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
