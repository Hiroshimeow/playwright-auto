#!/usr/bin/env python3
"""Offline learning ledger for Freelancer bids. Never submits or checks a bid."""

import argparse
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata

ROOT = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
ROOMS = ("tactics", "job_classes", "clients", "language", "failures", "outcomes")
NO_AWARD = {"closed_unawarded", "frozen_unawarded", "withdrawn"}
SUBMITTED = NO_AWARD | {"submitted", "award_pending_funding", "awarded"}
INCIDENTS = {"failed", "blocked", "ambiguous"}
RESULTS = SUBMITTED | INCIDENTS | {"not_submitted"}
UPDATE_FIELDS = ("bid_id", "result", "lesson", "root_cause", "replied")


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve()
        if not all((root / room).is_dir() for room in ROOMS):
            raise ValueError(f"not a complete palace directory: {root}")
        if args.command == "lesson":
            with write_lock(root):
                action = save_lesson(root, args)
            print(f"{action}: tactics/{args.tactic_id}.json")
            return 0
        if args.command == "record":
            with write_lock(root):
                action, path = record(root, args)
            print(f"{action}: {path.relative_to(root).as_posix()}")
            return 0
        attempts, _, incidents = load_ledger(root)
        classes = load_room(root, "job_classes")
        tactics = load_room(root, "tactics")
        totals = statistics(attempts.values(), classes, tactics)
        if args.command == "recall":
            recall(root, args, attempts, incidents, classes, tactics, totals)
        elif args.json:
            print(json.dumps(totals, indent=2))
        else:
            print_stats(totals)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"bid_memory: {error}\n")
    return 0


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="palace directory (default: beside this script)")
    commands = parser.add_subparsers(dest="command", required=True)
    lookup = commands.add_parser("recall", help="look up a class, keyword, or all general guidance")
    lookup.add_argument("keyword", nargs="?", type=nonempty)
    lookup.add_argument("--class", dest="job_class", help="registered class slug or exact alias")
    lookup.add_argument("--failures", action="store_true", help="show the full incident history, optionally filtered")
    new = commands.add_parser("record", help="record a first observation or append an update with --event-id")
    new.add_argument("--attempt-id", required=True, type=slug)
    new.add_argument("--event-id", type=slug, help="unique, stable ID of an update to an existing attempt")
    new.add_argument("--project-id", type=numeric_id)
    new.add_argument("--bid-id", type=numeric_id, help="verified Freelancer bid ID, not the project ID")
    new.add_argument("--class", dest="job_class", type=slug)
    new.add_argument("--amount", type=positive_number)
    new.add_argument("--currency", type=currency_code)
    new.add_argument("--basis", choices=("fixed", "hourly"))
    new.add_argument("--duration-days", type=positive_integer)
    new.add_argument("--weekly-limit", type=positive_integer)
    new.add_argument("--market-average", type=positive_number, help="same currency and basis as the quote")
    new.add_argument("--age-minutes", type=nonnegative_integer)
    new.add_argument("--bid-count", type=nonnegative_integer)
    new.add_argument("--bid-at", type=timestamp, help="actual send time, ISO 8601 with offset")
    new.add_argument("--client-flag", action="append", type=slug)
    new.add_argument("--tactic", action="append", type=slug, help="actually used tactic ID; repeatable")
    new.add_argument("--result", choices=sorted(RESULTS))
    new.add_argument("--replied", choices=("yes", "no", "unknown"))
    new.add_argument("--lesson", type=nonempty)
    new.add_argument("--root-cause", type=nonempty)
    new.add_argument("--source", required=True, type=nonempty, help="receipt or evidence reference; no private text")
    edit = commands.add_parser("lesson", help="create or revise a tactic and attach a cited evidence item")
    edit.add_argument("tactic_id", type=slug)
    edit.add_argument("--title", type=nonempty)
    edit.add_argument("--rule", type=nonempty)
    edit.add_argument("--class", dest="classes", action="append", type=slug, help="repeatable class scope; default is general")
    edit.add_argument("--general", action="store_true", help="clear existing class scope")
    edit.add_argument("--evidence-id", required=True, type=slug)
    edit.add_argument("--kind", required=True, choices=("support", "counter", "guidance"))
    edit.add_argument("--evidence", required=True, type=nonempty)
    edit.add_argument("--source", required=True, type=nonempty)
    commands.add_parser("stats", help="aggregate verified bids by class and by tactic").add_argument("--json", action="store_true")
    return parser


def record(root, args):
    attempts, events, _ = load_ledger(root)
    changes = {key: getattr(args, key) for key in UPDATE_FIELDS if getattr(args, key) is not None}
    if "replied" in changes:
        changes["replied"] = {"yes": True, "no": False, "unknown": None}[args.replied]
    snapshot = {key: getattr(args, key) for key in (
        "project_id", "job_class", "amount", "currency", "basis", "duration_days", "weekly_limit",
        "market_average", "age_minutes", "bid_count", "bid_at", "client_flag", "tactic",
    )}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.event_id:
        if any(value is not None for value in snapshot.values()):
            raise ValueError("updates cannot change the bid-time snapshot")
        path = root / "outcomes" / "updates" / f"{args.event_id}.json"
        payload = dict(attempt_id=args.attempt_id, source=args.source, changes=changes)
        if args.event_id in events:
            return check_replay(path, events[args.event_id], payload, "revision")
        if args.attempt_id not in attempts or not changes:
            raise ValueError("an update requires an existing attempt and at least one changed field")
        previous = attempts[args.attempt_id]
        if all(previous.get(key) == value for key, value in changes.items()):
            raise ValueError("update does not change the current outcome")
        if previous["bid_id"] and changes.get("bid_id", previous["bid_id"]) != previous["bid_id"]:
            raise ValueError("cannot replace a verified bid ID")
        current = previous | changes
        validate_result(current, changes)
        check_bid_id(attempts, current)
        payload["revision"] = previous["revision"] + 1
    else:
        if not all((args.project_id, args.job_class, args.result, args.lesson)):
            raise ValueError("first observation requires --project-id, --class, --result and --lesson")
        check_classes(root, [args.job_class])
        for tactic in args.tactic or ():
            if not (root / "tactics" / f"{tactic}.json").is_file():
                raise ValueError(f"unknown tactic: {tactic}")
        validate_quote(snapshot)
        path = root / "outcomes" / f"{args.attempt_id}.json"
        payload = dict(snapshot, attempt_id=args.attempt_id, source=args.source,
                       bid_id=None, replied=None, root_cause=None) | changes
        payload["client_flags"] = sorted(set(payload.pop("client_flag") or ())) or None
        payload["tactics"] = sorted(set(payload.pop("tactic") or ()))
        if args.attempt_id in attempts:
            return check_replay(path, read_json(path), payload)
        validate_result(payload, changes)
        check_bid_id(attempts, payload)
    write_json(path, dict(payload, recorded_at=now))
    return "recorded", path


def validate_quote(snapshot):
    if snapshot["amount"] is not None and not (snapshot["currency"] and snapshot["basis"]):
        raise ValueError("--amount requires --currency and --basis")
    if snapshot["market_average"] is not None and not (snapshot["currency"] and snapshot["basis"]):
        raise ValueError("--market-average requires --currency and --basis")
    if snapshot["weekly_limit"] is not None and snapshot["basis"] != "hourly":
        raise ValueError("--weekly-limit requires hourly basis")
    if snapshot["duration_days"] is not None and snapshot["basis"] != "fixed":
        raise ValueError("--duration-days requires fixed basis")


def validate_result(current, changes):
    if current["result"] not in RESULTS or not any(current["replied"] is x for x in (True, False, None)):
        raise ValueError("invalid result or reply evidence")
    if (current["result"] in SUBMITTED) != bool(current["bid_id"]):
        raise ValueError("submitted results require a verified --bid-id; non-submitted results must not have one")
    if current["result"] in INCIDENTS and not current["root_cause"]:
        raise ValueError("failed, blocked and ambiguous attempts need --root-cause (say unknown if unresolved)")
    if changes.get("result") in INCIDENTS and "root_cause" not in changes:
        raise ValueError("record the root cause of the new incident status with --root-cause")
    if current["replied"] is not None and current["result"] not in SUBMITTED:
        raise ValueError("cannot record reply evidence for an unverified bid")


def check_bid_id(attempts, candidate):
    if candidate["bid_id"] and any(
        row["bid_id"] == candidate["bid_id"] and key != candidate["attempt_id"] for key, row in attempts.items()
    ):
        raise ValueError(f"bid ID already belongs to another attempt: {candidate['bid_id']}")


def check_replay(path, existing, payload, *extra_metadata):
    old = {key: value for key, value in existing.items() if key not in ("recorded_at", *extra_metadata)}
    if old != payload:
        raise ValueError(f"different payload already recorded at {path.name}; use a new event ID for new evidence")
    return "already recorded", path


def load_ledger(root):
    # An observed update must refer to an already published initial file.
    events = load_room(root, "outcomes/updates")
    attempts = load_room(root, "outcomes")
    incidents = []
    for key, row in attempts.items():
        if row["attempt_id"] != key:
            raise ValueError(f"initial attempt ID does not match {key}.json")
        validate_result(row, {})
        row["revision"] = 0
        if row["result"] in INCIDENTS:
            incidents.append(dict(row))
    for key, event in sorted(events.items(), key=lambda pair: pair[1]["revision"]):
        row = attempts.get(event["attempt_id"])
        if row is None or event["revision"] != row["revision"] + 1:
            raise ValueError(f"orphan, duplicate or missing revision at {key}.json")
        changes = event["changes"]
        if not isinstance(changes, dict) or not changes or set(changes) - set(UPDATE_FIELDS):
            raise ValueError(f"invalid changes at {key}.json")
        if row["bid_id"] and changes.get("bid_id", row["bid_id"]) != row["bid_id"]:
            raise ValueError(f"verified bid ID changed at {key}.json")
        row.update(changes, revision=event["revision"], source=event["source"], recorded_at=event["recorded_at"])
        validate_result(row, changes)
        if row["result"] in INCIDENTS:
            incidents.append(dict(row))
    bid_ids = [row["bid_id"] for row in attempts.values() if row["bid_id"]]
    if len(set(bid_ids)) != len(bid_ids):
        raise ValueError("duplicate verified bid ID in ledger")
    return attempts, events, incidents


def save_lesson(root, args):
    path = root / "tactics" / f"{args.tactic_id}.json"
    old = read_json(path) if path.exists() else {}
    title = args.title or old.get("title")
    rule = args.rule or old.get("rule")
    if not title or not rule:
        raise ValueError("new tactic needs --title and --rule")
    if args.general and args.classes:
        raise ValueError("choose --general or --class, not both")
    classes = [] if args.general else sorted(set(args.classes or old.get("classes", [])))
    check_classes(root, classes)
    evidence = dict(old.get("evidence", {}))
    entry = dict(kind=args.kind, note=args.evidence, source=args.source)
    if args.evidence_id in evidence and evidence[args.evidence_id] != entry:
        raise ValueError("evidence ID already in use with different content")
    evidence[args.evidence_id] = entry
    payload = dict(title=title, rule=rule, classes=classes, evidence=evidence)
    if payload == {key: value for key, value in old.items() if key != "last_updated"}:
        return "unchanged"
    write_json(path, dict(payload, last_updated=datetime.now(JST).date().isoformat()))
    return "saved"


def statistics(attempts, classes, tactics):
    by_class = defaultdict(list, {key: [] for key in classes})
    by_tactic = defaultdict(list, {key: [] for key in tactics})
    by_tactic["(untagged)"] = []
    rows = list(attempts)
    for row in rows:
        by_class[row["job_class"]].append(row)
        for tactic in row["tactics"] or ["(untagged)"]:
            by_tactic[tactic].append(row)
    return {
        "overall": measure(rows),
        "by_class": {key: measure(items) for key, items in sorted(by_class.items())},
        "by_tactic": {key: measure(items) for key, items in sorted(by_tactic.items())},
    }


def measure(rows):
    bids = [row for row in rows if row["result"] in SUBMITTED]
    n = len(bids)
    replies = sum(row["replied"] is True for row in bids)
    awards = sum(row["result"] == "awarded" for row in bids)
    return dict(attempts=len(rows), bids=n, replies=replies, awards=awards,
                reply_unknown=sum(row["replied"] is None for row in bids),
                award_unresolved=sum(row["result"] not in NO_AWARD | {"awarded"} for row in bids),
                reply_rate=replies / n if n else None, award_rate=awards / n if n else None)


def print_stats(totals):
    print("Observed rates = confirmed / verified bids (lower bounds while outcomes are missing).")
    print(f"Overall: {format_stats(totals['overall'])}")
    for group in ("by_class", "by_tactic"):
        print(f"\n{group.replace('_', ' ').capitalize()}:")
        for key, row in totals[group].items():
            print(f"  {key}: {format_stats(row)}")


def format_stats(row):
    def rate(key):
        value = row[key]
        return f"{value:.1%}" if value is not None else "n/a"

    return (f"{row['bids']} bids / {row['attempts']} attempts; "
            f"{row['replies']} replies ({rate('reply_rate')}, {row['reply_unknown']} unknown); "
            f"{row['awards']} funded awards ({rate('award_rate')}, {row['award_unresolved']} unresolved)")


def recall(root, args, attempts, incidents, classes, tactics, totals):
    keyword = normalize(args.keyword or "")
    class_names = {key: [normalize(name) for name in [key, note["title"], *note["aliases"]]]
                   for key, note in classes.items()}
    if args.job_class:
        scope = {key for key, names in class_names.items() if normalize(args.job_class) in names}
        if len(scope) != 1:
            raise ValueError(f"unknown or ambiguous class: {args.job_class}; use a slug from stats")
    else:
        scope = {key for key, names in class_names.items()
                 if keyword and any(keyword in name for name in names)}

    def matches(value):
        return bool(keyword and keyword in normalize(value))

    if args.failures:
        print("Incident history (including incidents resolved by a later update):")
        for row in incidents:
            if (not scope and not keyword) or row["job_class"] in scope or matches(row):
                print(f"  #{row['project_id']} / {row['attempt_id']} / revision {row['revision']} / "
                      f"{row['result']}: {row['root_cause']} {row['lesson']} [{row['source']}]")
        return

    if not args.job_class and keyword:
        scope.update(row["job_class"] for row in attempts.values()
                     if matches([row["project_id"], row["bid_id"], row["attempt_id"]]))

    print("PRE-BID RECALL — Freelancer / JST / written-only")
    print("Rates count confirmed results only; unresolved outcomes are not evidence of failure.")
    print(format_stats(totals["overall"]))
    for key in sorted(scope):
        print(f"\nClass: {classes[key]['title']} [{key}]")
        print(format_stats(totals["by_class"][key]))
        print(f"Pricing: {classes[key]['pricing_notes']}")
    print("\nTactics (guidance is not conversion evidence):")
    for key, note in tactics.items():
        if not note["classes"] or scope.intersection(note["classes"]) or matches([key, note]):
            counts = {kind: sum(item["kind"] == kind for item in note["evidence"].values())
                      for kind in ("support", "counter", "guidance")}
            print(f"  {key}: {note['rule']} (updated {note['last_updated']}; "
                  f"{counts['support']} support, {counts['counter']} counter, {counts['guidance']} guidance)")
            print(f"    {format_stats(totals['by_tactic'][key])}")
            for evidence_id, entry in note["evidence"].items():
                if keyword and matches([key, evidence_id, entry]):
                    print(f"    {entry['kind']}: {entry['note']} [{entry['source']}]")
    for room in ("clients", "language", "failures"):
        notes = [note for key, note in load_room(root, room).items()
                 if note.get("general") or scope.intersection(note.get("classes", [])) or matches([key, note])]
        if notes:
            print(f"\n{room.capitalize()}:")
            for note in notes:
                print(f"  {note['title']}: {note['lesson']}")
                for opening in note.get("verified_openings", [])[-5:]:
                    print(f"    {opening['locale']} / bid {opening['bid_id']}: "
                          f"{opening['opening']} [{opening['source']}]")
    history = [row for row in attempts.values() if row["job_class"] in scope or matches(row)]
    if history:
        print(f"\nMatching latest outcomes (showing up to 8 of {len(history)}):")
        for row in sorted(history, key=lambda item: (item["recorded_at"], item["attempt_id"]), reverse=True)[:8]:
            cause = f" Root cause: {row['root_cause']}" if row["root_cause"] else ""
            print(f"  #{row['project_id']} / {row['attempt_id']} / {row['result']}: {row['lesson']}{cause}")


def check_classes(root, classes):
    for key in classes:
        if not (root / "job_classes" / f"{key}.json").is_file():
            raise ValueError(f"unknown class: {key}; add a job_classes card first")


def load_room(root, room):
    return {path.stem: read_json(path) for path in sorted((root / room).glob("*.json"))}


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ValueError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


@contextmanager
def write_lock(root):
    lock = root / ".write-lock"
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise ValueError(f"writer busy or interrupted: {lock}; see INTEGRATION.md") from error
    try:
        yield
    finally:
        lock.rmdir()


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, suffix=".tmp", delete=False)
    draft = Path(stream.name)
    try:
        with stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        draft.replace(path)
    finally:
        draft.unlink(missing_ok=True)


def normalize(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFC", str(value)).casefold().replace("_", " ")))


def nonempty(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be blank")
    return value.strip()


def slug(value):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 100:
        raise argparse.ArgumentTypeError("expected a lowercase slug (letters, digits and hyphens)")
    if re.fullmatch(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", value):
        raise argparse.ArgumentTypeError("reserved filename")
    return value


def numeric_id(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("expected a positive numeric ID")
    return value


def currency_code(value):
    if not re.fullmatch(r"[A-Za-z]{3}", value):
        raise argparse.ArgumentTypeError("expected a three-letter currency code")
    return value.upper()


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is not None:
            return result.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        pass
    raise argparse.ArgumentTypeError("expected an ISO 8601 timestamp with UTC offset")


def nonnegative_integer(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return n


def positive_integer(value):
    return int(numeric_id(value))


def positive_number(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return n


if __name__ == "__main__":
    raise SystemExit(main())
