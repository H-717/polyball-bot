"""Watch the Ticketcorner resale for Polyball tickets and push every new offer to your phone.

Usage:
  python polyball_bot.py run              poll the resale forever (what the Docker container runs)
  python polyball_bot.py check [EVENT]    one check: list current offers with details
  python polyball_bot.py notify-test      send a test notification
"""
import json
import os
import random
import signal
import sys
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi import requests

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", HERE))
CONFIG = DATA / "config.json"
STATE = DATA / "state.json"
LOG = DATA / "log.txt"
HEARTBEAT = DATA / ".heartbeat"     # touched after every successful check (Docker healthcheck)

API = "https://api-cloud.eventim.com/ecom/resale/offer-listing/prd/api/v2/platforms/8"  # 8 = Ticketcorner
ZURICH = ZoneInfo("Europe/Zurich")

# api-cloud.eventim.com sits behind Akamai, which drops plain Python clients based on their TLS
# handshake. curl_cffi sends Chrome's handshake; if one profile gets blocked we rotate.
IMPERSONATE = ["chrome", "chrome131", "edge", "safari"]
HEADERS = {"Origin": "https://www.ticketcorner.ch", "Referer": "https://www.ticketcorner.ch/",
           "Accept": "application/json"}

ALERT_AFTER = 10 * 60      # seconds without a successful check before warning the phone
MAX_BACKOFF = 10 * 60
SUMMARY_EVERY = 3600       # log a line with check counts this often
GONE_AFTER = 2             # an offer must be missing from this many checks in a row to count as gone


def load_config():
    if not CONFIG.exists():
        raise SystemExit(f"{CONFIG} not found - copy config.example.json there and fill it in.")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


cfg = load_config()


def now():
    return datetime.now(ZURICH)


def log(msg):
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except ValueError as ex:
        log(f"state.json unreadable ({ex}), starting fresh")
    return {"listed": []}


def save_state(state):
    # Write-then-rename, so being killed mid-write can't leave a broken file behind.
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


# --- notifications ----------------------------------------------------------

_pending = []   # notifications that couldn't be sent yet (offline)


def topics():
    t = cfg.get("ntfy_topic") or []
    return [t] if isinstance(t, str) else list(t)


def notify(title, msg, priority=3, tags=(), click=None):
    """Push notification via ntfy. Never raises; unsent messages are retried on the next check."""
    for topic in topics():
        m = {"topic": topic, "title": title, "message": msg, "priority": priority, "tags": list(tags)}
        if click:
            m["click"] = click
            m["actions"] = [{"action": "view", "label": "Open Ticketcorner", "url": click}]
        _pending.append(m)
    flush_notifications()


def flush_notifications():
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    while _pending:
        try:
            r = requests.post(server + "/", json=_pending[0], timeout=10)
        except Exception as ex:
            log(f"notification not sent yet, will retry: {ex}")
            return
        if r.status_code == 429 or r.status_code >= 500:
            log(f"notification not sent yet (HTTP {r.status_code}), will retry")
            return
        if r.status_code >= 400:
            # Retrying won't help (bad topic etc.) and would block every later notification.
            log(f"notification rejected by ntfy, dropped: HTTP {r.status_code} {r.text[:200]}")
        _pending.pop(0)


# --- ticketcorner -----------------------------------------------------------

class Blocked(Exception):
    """Akamai answered 403/429 - we're (temporarily) blocked."""


class Client:
    def __init__(self):
        self.profile = 0
        self.session = self._new_session()

    def _new_session(self):
        s = requests.Session(impersonate=IMPERSONATE[self.profile % len(IMPERSONATE)])
        s.headers.update(HEADERS)
        return s

    def rotate(self):
        self.profile += 1
        self.session.close()
        self.session = self._new_session()

    def get(self, path):
        r = self.session.get(API + path, timeout=15)
        if r.status_code in (403, 429):
            raise Blocked(f"HTTP {r.status_code}")
        r.raise_for_status()
        return r.json()     # ValueError if Akamai answers with an HTML challenge page

    def offers(self, event_id):
        """All resale offers currently listed for the event (summary form)."""
        data = self.get(f"/events/{event_id}/offers")
        if not isinstance(data, list) or not all(isinstance(o, dict) and "id" in o for o in data):
            raise ValueError(f"unexpected offer list: {str(data)[:200]}")
        return data

    def offer(self, offer_id):
        """Full offer: ticket types, per-ticket prices, fees. None if it's already gone."""
        try:
            return self.get(f"/offers/{offer_id}")
        except requests.exceptions.HTTPError as ex:
            if ex.response is not None and ex.response.status_code == 404:
                return None
            raise


def norm(s):
    """Lowercase without accents, so "Gönner" matches "gonner"/"goenner" style config entries."""
    s = str(s or "").lower().replace("ö", "oe").replace("ä", "ae").replace("ü", "ue")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def classify(ticket):
    """The ticket_types entry for a ticket: by Ticketcorner's ticket type id, else by name keyword.
    None if nothing matches."""
    types = cfg.get("ticket_types", [])
    type_id = ticket.get("tdlTicketTypeId")
    for t in types:
        if type_id is not None and str(type_id) in map(str, t.get("ids", [])):
            return t
    name = norm(ticket.get("tdlTicketTypeName"))
    for t in types:
        if name and any(norm(k) in name for k in t.get("match", [])):
            return t
    return None


def chf(x):
    return f"CHF {x:.2f}" if isinstance(x, (int, float)) else "CHF ?"


def describe(summary, detail):
    """Notification (title, message, priority), or None if the offer only has ignored types."""
    n = summary.get("numberOfTickets") or 1
    where = ", ".join(x for x in [summary.get("tdlPriceLevelName"), summary.get("ticketAreas")] if x)
    where = f" - {where}" if where else ""

    tickets = (detail or {}).get("tickets") or []
    if not tickets:  # no details - send what the list gives us rather than nothing
        fair = summary.get("fairPrice")
        return (f"Polyball resale: {n} ticket{'s' * (n > 1)}",
                f"Total {chf(summary.get('buyerPayableAmountOffer'))} incl. fees{where}. "
                f"{'Fair price.' if fair else 'Ticket type and face value unknown.'}\n"
                "Tap to open Ticketcorner. Be quick!", 5 if fair else 4)

    types = [classify(t) for t in tickets]
    if all(tp and tp.get("ignore") for tp in types):
        return None

    lines, labels, above, unchecked = [], [], 0.0, False
    for t, tp in zip(tickets, types):
        price = t.get("price")
        face = tp.get("face_value") if tp else None
        label = tp["label"] if tp else f"{t.get('tdlTicketTypeName') or '?'} (unknown type)"
        labels.append(label)
        verdict = ""
        if isinstance(face, (int, float)) and isinstance(price, (int, float)):
            above = max(above, price - face)
            verdict = " = face value" if abs(price - face) < 0.01 else (
                f" (face {chf(face)}, +{chf(price - face)})" if price > face else f" (face {chf(face)})")
        elif not (tp and tp.get("ignore")):
            unchecked = True
        lines.append(f"- {label}: {chf(price)}{verdict}")

    if above >= 0.01:
        status, fair = f" (+{chf(above)} above face value)", False
    elif detail.get("fairPrice") or not unchecked:
        status, fair = "", True
    else:
        status, fair = " (price not checked)", False
    kinds = " + ".join(dict.fromkeys(labels))
    fees = detail.get("buyerServiceFeeGrossAmountOffer")
    total = detail.get("buyerPayableAmountOffer", summary.get("buyerPayableAmountOffer"))
    msg = "\n".join(lines + [
        f"Total {chf(total)} incl. {chf(fees)} fees{where}",
        "Tap to open Ticketcorner. Be quick!"])
    return f"Polyball resale: {n}x {kinds}{status}", msg, 5 if fair else 4


# --- commands ---------------------------------------------------------------

def event_url():
    return cfg.get("event_url") or f"https://www.ticketcorner.ch/event/{cfg['event_id']}/"


def cmd_check(event_id=None):
    event_id = event_id or cfg["event_id"]
    client = Client()
    offers = client.offers(event_id)
    log(f"event {event_id}: {len(offers)} resale offer(s)")
    for o in offers:
        d = describe(o, client.offer(o["id"]))
        print(f"\n[{o['id']}]")
        print("  (ignored type)" if d is None else f"  {d[0]}  [priority {d[2]}]\n  " + d[1].replace("\n", "\n  "))


def cmd_notify_test():
    if not topics():
        raise SystemExit("No ntfy_topic in config.json.")
    notify("Polyball bot: test", "Notifications work. You'll get a message like this for every resale offer.",
           priority=4, tags=["tada"], click=event_url())
    if _pending:
        raise SystemExit("Could not reach ntfy - see log.")
    log("test notification sent (if it didn't arrive, check the topic name in the app and config.json)")


def announce_offer(client, oid, summary):
    """Look up a new offer and push it. Never raises: a strange offer must not crash the bot
    (Docker would restart it into the same offer forever, and you'd never hear about it)."""
    try:
        try:
            detail = client.offer(oid)
            if detail is None:
                log(f"offer {oid} appeared but was gone before we could look at it")
                return
        except Exception as ex:
            log(f"offer {oid}: details failed ({ex}), notifying with summary only")
            detail = None
        d = describe(summary, detail)
        if d is None:
            log(f"offer {oid}: ignored ticket type")
            return
    except Exception:
        log(f"offer {oid}: couldn't read it:\n{traceback.format_exc()}")
        d = ("Polyball resale: new offer", "Couldn't read the details. Tap to open Ticketcorner. Be quick!", 5)
    title, msg, priority = d
    log(f"NEW OFFER {oid}: {title} | {msg.replace(chr(10), ' | ')}")
    notify(title, msg, priority=priority, tags=["rotating_light", "ticket"], click=event_url())


def cmd_run():
    event_id = cfg["event_id"]
    interval = cfg.get("interval_seconds", 15)
    event_start = datetime.fromisoformat(cfg["event_start"]).replace(tzinfo=ZURICH) if cfg.get("event_start") else None
    state = load_state()
    listed = set(state.get("listed", []))
    missing = {}            # offer id -> checks in a row it was absent from the list
    client = Client()

    def finished():
        if not (event_start and now() > event_start):
            return False
        log("the event has started - not watching any more")
        if not state.get("stopped"):
            notify("Polyball bot stopped", "The ball has started, no point watching the resale any more. "
                   "Have fun!", priority=2, tags=["dancer"])
            state["stopped"] = True
            save_state(state)
        while True:     # stay up quietly so Docker doesn't restart us in a loop
            time.sleep(86400)

    finished()
    log(f"watching resale for event {event_id} every ~{interval}s ({len(listed)} offer(s) already known)")
    notify("Polyball bot online", f"Checking the Ticketcorner resale every ~{interval} s. "
           f"{len(listed)} offer(s) currently listed.", priority=2, tags=["white_check_mark"], click=event_url())

    last_ok, alerted, failures, last_error = time.time(), False, 0, ""
    checks, summary_at = 0, time.time()

    while True:
        finished()
        try:
            offers = client.offers(event_id)
        except Exception as ex:
            failures += 1
            last_error = f"{type(ex).__name__}: {str(ex)[:150]}"
            if failures == 1 or failures % 20 == 0:
                log(f"check failed ({failures}x in a row): {last_error}")
            if isinstance(ex, (Blocked, ValueError)):
                client.rotate()
            if not alerted and time.time() - last_ok > ALERT_AFTER:
                notify("Polyball bot: can't check the resale",
                       f"No successful check for {int((time.time() - last_ok) / 60)} min ({last_error}). "
                       "The bot keeps retrying; check the resale by hand meanwhile.",
                       priority=4, tags=["warning"], click=event_url())
                alerted = True
            time.sleep(min(interval * 2 ** min(failures, 6), MAX_BACKOFF) * random.uniform(0.8, 1.2))
            continue

        if failures:
            log(f"checks work again after {failures} failure(s)")
            if alerted:
                notify("Polyball bot: back to normal", "The resale is being checked again.",
                       priority=2, tags=["white_check_mark"])
        last_ok, alerted, failures = time.time(), False, 0
        checks += 1
        HEARTBEAT.touch()
        flush_notifications()

        current = {o["id"]: o for o in offers}
        changed = False
        for oid in current.keys() - listed:
            announce_offer(client, oid, current[oid])
            listed.add(oid)
            changed = True
        # A single check without an offer can be a stale cache; only call it gone when it stays away.
        # One that really went and comes back later (e.g. someone gave up at checkout) counts as new.
        for oid in list(listed):
            if oid in current:
                missing.pop(oid, None)
            else:
                missing[oid] = missing.get(oid, 0) + 1
                if missing[oid] >= GONE_AFTER:
                    log(f"offer {oid} is gone")
                    listed.discard(oid)
                    missing.pop(oid)
                    changed = True
        if changed:
            state["listed"] = sorted(listed)
            save_state(state)

        hb_hour = cfg.get("heartbeat_hour", 9)
        today = now().date().isoformat()
        if hb_hour is not None and now().hour == hb_hour and state.get("last_heartbeat") != today:
            notify("Polyball bot alive", f"{len(current)} offer(s) listed right now. Still watching.",
                   priority=1, tags=["heartbeat"])
            state["last_heartbeat"] = today
            save_state(state)

        if time.time() - summary_at >= SUMMARY_EVERY:
            log(f"{checks} checks in the last hour, {len(current)} offer(s) listed")
            checks, summary_at = 0, time.time()

        time.sleep(interval * random.uniform(0.7, 1.3))


def main():
    # `docker stop` sends SIGTERM. Python running as PID 1 would ignore it and get killed 10 s later.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        if cmd == "run":
            cmd_run()
        elif cmd == "check":
            cmd_check(sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "notify-test":
            cmd_notify_test()
        else:
            print(__doc__)
            sys.exit(2)
    except KeyboardInterrupt:
        log("stopped")
    except SystemExit as ex:
        if ex.code == 0:
            log("stopped")
        raise
    except Exception:
        log("crashed:\n" + traceback.format_exc())
        notify("Polyball bot crashed", f"{traceback.format_exc(limit=1).strip()[-300:]}\n"
               "Docker restarts it automatically; check the log if this repeats.", priority=4, tags=["warning"])
        sys.exit(1)


if __name__ == "__main__":
    main()
