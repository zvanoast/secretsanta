# Secret Santa

Draws names fairly and emails each person who they're buying for. The organizer never sees the matches.

One file, Python 3.11+, no packages to install.

## Rules
- Nobody draws themselves.
- **Excluded pairs** never draw each other, in either direction. List them under `exclude` in `santa.toml`. That file is private, so nobody sees who you've kept apart.
- Nobody gets the same person as **last year** (set `avoid_repeats_years`; 0 turns it off).

Every draw that follows the rules is equally likely: the script shuffles, and reshuffles if any rule is broken.

## First-time setup
1. `cp santa.example.toml santa.toml` and fill in your email, budget and date.
2. `cp participants.example.csv participants.csv` and list everyone. `backup_email` is optional.
3. Gmail needs an **app password**, not your normal one: <https://myaccount.google.com/apppasswords> (2-Step Verification must be on). The script asks for it every time, or reads `SANTA_PASSWORD`. It's never saved.
4. Got a `backup 2025.txt` from the old script? Import it so last year's matches aren't repeated, then delete the text file:
   `python3 santa.py import-old "backup 2025.txt" --year 2025`

`santa.toml`, `participants.csv` and `draws/` are git-ignored. This repo is public, so keep it that way.

## Every year
```
python3 santa.py check     # practice draw: catches typos and impossible rules; nothing sent or shown
python3 santa.py test      # emails you a sample, to check the login and how it looks
python3 santa.py send      # the real draw: saves it sealed in draws/YEAR.json and emails everyone
```
Someone lost their email? `python3 santa.py resend "Name"` sends it again without showing it to you.

**Keep the `draws/` folder** between years; that's how last year's matches are avoided.

## Proving it's fair
`python3 santa.py stats` simulates 100,000 draws and shows how often each person drew each other person.
- With no exclusions, every pairing lands near 1 in (people − 1).
- With exclusions, the numbers aren't all equal, and that's expected. Someone with an excluded pair has fewer people left to draw, so each of those gets drawn by them more often. Every valid draw is still equally likely.

`stats` leaves last year's draw out of the simulation, so it can't reveal it.

## Notes
- **Sealed, not encrypted.** Draw files are base64-encoded so nobody sees matches by accident. The organizer could decode them on purpose; don't.
- **Your Sent folder.** Gmail keeps a copy of every email it sends, matches included. Don't look there, or send from a separate Gmail account.
- **Other email providers:** set `smtp_host` and `smtp_port` (465 for SSL, 587 for STARTTLS).
- **Tests:** `python3 -m unittest -v`
