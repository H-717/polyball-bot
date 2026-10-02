# polyball-bot

Polyball tickets sell out almost instantly, and after that the only way in is the official
Ticketcorner resale, where fans resell their tickets. Those offers are gone within minutes too.
This bot checks the resale every ~15 seconds and pushes every new offer to your phone, with the
ticket type and how the price compares to face value. You still buy by hand; it just makes sure
you're first to know.

## How it works

The "Resale offers" box on the
[event page](https://www.ticketcorner.ch/en/event/polyball-confoederatio-polytechnica-eth-zuerich-22138112/)
is a widget that loads its data from a JSON API (no login needed):

- `GET https://api-cloud.eventim.com/ecom/resale/offer-listing/prd/api/v2/platforms/8/events/22138112/offers`
  lists the current offers (`[]` when there are none). Platform 8 is Ticketcorner.
- `GET .../v2/platforms/8/offers/{offerId}` returns one offer in full: ticket type
  (`tdlTicketTypeName`), per-ticket price, service fee and total, plus a `fairPrice` flag that
  Ticketcorner documents as "the offer costs no more than the original ticket price".

The API sits behind Akamai, which drops normal Python clients (`requests`, `curl`) based on their
TLS handshake. The bot uses [curl_cffi](https://github.com/lexiforest/curl_cffi), which makes
the same handshake as Chrome, so it needs no browser. That keeps the Docker image small enough
for a Raspberry Pi.

Ticketcorner doesn't generally cap resale prices: other events have plenty of offers above face
value. The organiser may cap Polyball, though. Either way, each notification shows the price next
to the face value.

Each run:

1. fetches the offer list every `interval_seconds` (±30% random jitter)
2. for each offer it hasn't seen before, fetches the details and sends a notification
3. remembers the listed offers in `state.json`, so a restart doesn't repeat them. An offer that
   is missing from two checks in a row and then comes back (e.g. someone gave up at checkout)
   counts as new. A single miss is usually a stale cache and is ignored.
4. backs off if Ticketcorner blocks it or the internet is down, and warns you after 10 minutes
5. stops by itself once the ball has started

## Setup on the Raspberry Pi

Needs a Pi 4 or 5 with a 64-bit OS: `uname -m` must print `aarch64`.

Install Docker if it isn't installed yet:

```
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER      # then log out and back in
```

Copy this folder to the Pi (e.g. `scp -r polyBall pi@raspberrypi.local:~/polyball-bot`), then:

```
cd ~/polyball-bot
mkdir -p data
cp -n config.example.json data/config.json    # -n: keeps an existing config
nano data/config.json              # check ntfy_topic, see Notifications
docker compose up -d --build
docker compose run --rm polyball-bot python polyball_bot.py notify-test
```

The container starts again by itself after a reboot or crash (`restart: unless-stopped`).

## Configuration

`data/config.json`:

| key | meaning |
| --- | --- |
| `event_id` | number at the end of the Ticketcorner event URL (`22138112` for Polyball 2026) |
| `event_url` | link the notification opens |
| `event_start` | local time the bot stops watching (`2026-11-28T19:00`) |
| `interval_seconds` | time between checks. 15 s is about 5,800 requests a day. Going much lower risks a temporary IP block, and the API is cached for 10 s anyway |
| `ntfy_topic` | a topic, or a list of topics (e.g. one for you and one for your friend) |
| `ticket_types` | see below |
| `heartbeat_hour` | hour (Zurich time) for a quiet daily "still alive" message, `null` to turn it off |
| `ntfy_server` | optional, defaults to `https://ntfy.sh` |

### Ticket types

The resale API identifies each ticket by a numeric type ID. The English names you see on the page
come from a table in the event page's HTML. For Polyball 2026 (price category "Stehplatz") it is:

| id | name on the page | in `config.example.json` |
| --- | --- | --- |
| 70288318 | Regular price | face value CHF 104.90 |
| 70288316 | Mit Legi | face value CHF 69.70 |
| 70292397 | Gönner | `"ignore": true` |
| 70294488 / 70294487 / 70294486 | VIP / Helfer / Freibilllet | notified, price not compared |

Each `ticket_types` entry has a `label` (what the notification shows), `ids`, `match` (keywords
for the ticket type name, used only if the ID is unknown), an optional `face_value` and an
optional `"ignore": true`. An offer that matches nothing is still sent, labelled "(unknown type)"
with the raw name, so nothing gets missed.

To find the IDs for another year's event, open its page in a browser, view the source and search
for `ticketTypeNameById`.

## Notifications

The bot pushes through [ntfy](https://ntfy.sh) (free, no account), the same as asvz-bot. A
"topic" is a channel name: the bot publishes to it and your phone subscribes to it. Anyone who
knows the name can read it, so use a long random one, not the ASVZ topic.

1. Generate a topic name:
   `python -c "import secrets; print('polyball-' + secrets.token_hex(10))"`
2. Put it in `data/config.json` as `"ntfy_topic": "polyball-..."`. Use a list,
   `["topic-a", "topic-b"]`, to notify several people separately.
3. Install the ntfy app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy),
   [iPhone](https://apps.apple.com/app/ntfy/id1625396347)), tap **+**, enter the topic name, leave
   the server as `ntfy.sh` and tap **Subscribe**. Several phones can subscribe to the same topic.
4. Run `notify-test` (see [Usage](#usage)). The test message should show up within seconds.
5. On Android, open the topic in the app, then **⋮ > Settings**, and allow urgent messages to
   override Do Not Disturb. On iPhone, allow ntfy notifications in the system settings.

You can also check a topic in a browser at `https://ntfy.sh/<topic>`.

| message | priority |
| --- | --- |
| new offer at or below face value | 5 (urgent) |
| new offer above face value, or price couldn't be checked | 4 (high) |
| can't check for 10 min / crashed | 4 |
| bot online, back to normal, daily heartbeat, stopped after the event | 1-2 (quiet) |

Tapping a notification opens the event page. The resale box is near the bottom.

**Be ready to buy:** stay logged in to Ticketcorner on your phone. You can only have one resale
offer in your cart at a time, and Legi tickets need a valid student ID at the entrance.

## Usage

```
docker compose logs -f                 # live log (also in data/log.txt)
docker compose ps                      # "healthy" = a successful check in the last 5 min
docker compose run --rm polyball-bot python polyball_bot.py check   # list current offers once
docker compose restart                 # after editing config.json
docker compose up -d --build           # after updating polyball_bot.py
docker compose down                    # stop for good
```

`check EVENT_ID` works with any Ticketcorner event, which is handy for seeing what an offer looks
like while Polyball has none.

Without Docker: `pip install -r requirements.txt`, put `config.json` next to the script, then
`python polyball_bot.py run`.

## Notes

- The API isn't public or documented, so it can change without notice. If it breaks, you get a
  "can't check the resale" notification.
- If Ticketcorner blocks the Pi's IP (HTTP 403), the bot switches to a different browser
  fingerprint and slows down. Blocks like this are usually temporary.
