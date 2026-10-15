import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import discord
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# MuhRO MVP Tracker — Render-ready configuration
# ============================================================
MVP_LOG_URL = os.getenv(
    "MVP_LOG_URL",
    "https://flux.muhro.eu/?module=ranking&action=mvp",
)
DISCORD_WEBHOOK_1 = os.getenv("DISCORD_WEBHOOK_1", "").strip()
DISCORD_WEBHOOK_2 = os.getenv("DISCORD_WEBHOOK_2", "").strip()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()

# On Render, attach a persistent disk mounted at /var/data and set
# STATE_FILE=/var/data/muhro_state.json to preserve state across deploys.
STATE_FILE = os.getenv("STATE_FILE", "muhro_state.json")
PORT = int(os.getenv("PORT", "10000"))
POLL_SECONDS = max(15, int(os.getenv("POLL_SECONDS", "30")))
ALERT_ON_STARTUP = os.getenv("ALERT_ON_STARTUP", "false").lower() in {
    "1", "true", "yes", "on"
}
TIMEZONE = ZoneInfo("Asia/Manila")
ALWAYS_ALIVE_OVERLOADS = {
    name.strip().casefold()
    for name in os.getenv("ALWAYS_ALIVE_OVERLOADS", "").split(",")
    if name.strip()
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("muhro-mvp")

session = requests.Session()
retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset({"GET", "POST"}),
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; MuhRO-MVP-Tracker/2.0)",
    "Accept": "text/html,application/xhtml+xml",
})

state_lock = threading.RLock()
# Key is "MVP name||map". Stored fields: last_killer, first_seen_at,
# min_spawn_at, max_spawn_at, estimated_spawn_at, and display strings.
mvp_database = {}
known_last_killers = {}
discord_client = None


def now_local():
    return datetime.now(TIMEZONE)


def dt_to_iso(value):
    return value.astimezone(TIMEZONE).isoformat()


def iso_to_dt(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE)
    except (TypeError, ValueError):
        return None


def load_state():
    """Load prior observations; malformed or missing state starts cleanly."""
    global mvp_database, known_last_killers
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            saved = json.load(handle)
        with state_lock:
            mvp_database = saved.get("mvp_database", {})
            known_last_killers = saved.get("known_last_killers", {})
        log.info("Loaded %d MVP records from %s", len(mvp_database), STATE_FILE)
    except FileNotFoundError:
        log.info("No saved state found; first scrape will establish a baseline.")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        log.warning("Could not load state file (%s); starting with empty state.", exc)


def save_state():
    """Atomic replacement prevents a partial JSON file after interruption."""
    directory = os.path.dirname(os.path.abspath(STATE_FILE))
    try:
        os.makedirs(directory, exist_ok=True)
        temp_path = STATE_FILE + ".tmp"
        with state_lock:
            snapshot = {
                "mvp_database": mvp_database,
                "known_last_killers": known_last_killers,
            }
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, STATE_FILE)
    except OSError as exc:
        log.error("Failed to save state to %s: %s", STATE_FILE, exc)


_DURATION_PARTS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
    re.IGNORECASE,
)


def parse_duration_seconds(value):
    """Parse strings such as '1 hour 10 minutes' or '2 hours 5 minutes'."""
    if not value:
        return None
    text = value.strip().casefold()
    if text in {"n/a", "na", "none", "-", "unknown"}:
        return None

    matches = list(_DURATION_PARTS.finditer(text))
    if not matches:
        # A bare number is not interpreted as minutes because units are unknown.
        return None

    seconds = 0.0
    for match in matches:
        amount = float(match.group(1))
        unit = match.group(2).casefold()
        if unit.startswith("d"):
            seconds += amount * 86400
        elif unit.startswith("h") or unit.startswith("hr"):
            seconds += amount * 3600
        elif unit.startswith("m"):
            seconds += amount * 60
        else:
            seconds += amount
    return int(seconds)


def parse_respawn_range(respawn_text):
    """
    Return (minimum seconds, maximum seconds).
    MuhRO's current page displays fixed respawn intervals, not kill timestamps.
    """
    text = (respawn_text or "").strip()
    if not text or "n/a" in text.casefold():
        return None, None

    # Handle common range separators while retaining units inside each duration.
    parts = re.split(r"\s+(?:-|–|—|to)\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2:
        low = parse_duration_seconds(parts[0])
        high = parse_duration_seconds(parts[1])
        if low is not None and high is not None:
            return min(low, high), max(low, high)

    exact = parse_duration_seconds(text)
    if exact is not None:
        return exact, exact
    return None, None


def fmt_dt(value):
    return value.astimezone(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def fmt_spawn_range(min_dt, max_dt):
    if not min_dt or not max_dt:
        return "Unknown (respawn interval could not be parsed)"
    if min_dt == max_dt:
        return f"⏳ **{fmt_dt(min_dt)}** (GMT+8)"
    return f"⏳ **{fmt_dt(min_dt)} ~ {fmt_dt(max_dt)}** (GMT+8)"


def normalize_key(name, map_name):
    return f"{name.strip().casefold()}||{map_name.strip().casefold()}"


def extract_cell_text(cell):
    # Prefer visible text; link/image alt text can hold the killer/monster name.
    text = " ".join(cell.stripped_strings)
    if text:
        return re.sub(r"\s+", " ", text).strip()
    for tag in cell.find_all(["img", "a"]):
        candidate = tag.get("alt") or tag.get("title") or tag.get_text(" ", strip=True)
        if candidate:
            return re.sub(r"\s+", " ", candidate).strip()
    return ""


def fetch_mvp_rows():
    """Fetch and parse the live MVP Spawn List table."""
    response = session.get(MVP_LOG_URL, timeout=(8, 20))
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    target_table = None
    for table in soup.find_all("table"):
        headers = [
            extract_cell_text(cell).casefold()
            for cell in table.find_all("th")
        ]
        header_text = " ".join(headers)
        if "monster" in header_text and "respawn" in header_text:
            target_table = table
            break

    if target_table is None:
        # Fallback for pages whose table header uses td elements instead of th.
        for table in soup.find_all("table"):
            first_row = table.find("tr")
            if not first_row:
                continue
            header_text = " ".join(
                extract_cell_text(cell).casefold()
                for cell in first_row.find_all(["th", "td"])
            )
            if "monster" in header_text and "respawn" in header_text:
                target_table = table
                break

    if target_table is None:
        raise ValueError("Could not find the MuhRO MVP table; website markup may have changed.")

    rows = []
    for row in target_table.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 4:
            continue
        values = [extract_cell_text(cell) for cell in cells]
        if any(word in " ".join(values).casefold() for word in ("monster", "map location", "last killer")):
            continue
        monster, map_name, respawn_text, killer = values[:4]
        if not monster or not map_name:
            continue
        rows.append({
            "mvp_name": monster,
            "map_name": map_name,
            "respawn_text": respawn_text,
            "killer_name": killer or "Unknown",
        })

    if not rows:
        raise ValueError("The MVP table was found, but no data rows were parsed.")
    return rows


def make_embed(title, color, fields):
    embed = discord.Embed(title=title, color=color, timestamp=now_local())
    for name, value, inline in fields:
        # Discord embed field values cannot be empty and are limited to 1024 chars.
        value = str(value).strip() or "Unknown"
        embed.add_field(name=name[:256], value=value[:1024], inline=inline)
    embed.set_footer(text="MuhRO MVP Tracker • Times are GMT+8")
    return embed


def send_webhook_alert(embed_dict, content="🏆 **MuhRO MVP Tracker**"):
    """Send to each configured webhook independently."""
    urls = [url for url in (DISCORD_WEBHOOK_1, DISCORD_WEBHOOK_2) if url]
    if not urls:
        log.warning("No Discord webhook configured; skipping webhook alert.")
        return

    payload = {"content": content, "embeds": [embed_dict]}
    for url in urls:
        try:
            response = session.post(url, json=payload, timeout=(8, 15))
            response.raise_for_status()
            log.info("Webhook alert delivered.")
        except requests.RequestException as exc:
            # Never log the URL because it contains the webhook secret.
            log.error("Discord webhook delivery failed: %s", exc)


def embed_to_dict(title, color, fields):
    return {
        "title": title,
        "color": color,
        "fields": [
            {"name": name, "value": str(value)[:1024] or "Unknown", "inline": inline}
            for name, value, inline in fields
        ],
        "footer": {"text": "MuhRO MVP Tracker • Times are GMT+8"},
        "timestamp": now_local().isoformat(),
    }


def build_kill_fields(record):
    return [
        ("MVP", record["mvp_name"], True),
        ("Killed By (last listed)", record["killer_name"], True),
        ("Map", record["map_name"], True),
        ("First Detected (GMT+8)", record["first_seen_display"], False),
        ("Estimated Spawn Window (GMT+8)", record["spawn_display"], False),
       ]


def check_mvp_kills():
    """
    Detect changes to the site's 'Last Killer' field.

    Important limitation: the current ranking page exposes a respawn interval
    and last-killer name, but no kill timestamp or unique kill ID. Thus this
    tracker can only estimate from the time a changed killer is first observed.
    Consecutive kills by the same player may not be detectable from this page.
    """
    rows = fetch_mvp_rows()
    now = now_local()
    changed_records = []
    first_run_baseline = False

    with state_lock:
        if not known_last_killers:
            first_run_baseline = True

        for row in rows:
            mvp_name = row["mvp_name"]
            map_name = row["map_name"]
            killer = row["killer_name"]
            respawn_text = row["respawn_text"]
            key = normalize_key(mvp_name, map_name)
            old_killer = known_last_killers.get(key)

            min_seconds, max_seconds = parse_respawn_range(respawn_text)
            # If no saved record exists, use the observation time as a baseline.
            # Do not pretend it is the actual kill time.
            is_changed = old_killer is not None and killer.casefold() != old_killer.casefold()
            is_new_key = old_killer is None

            if is_changed or (is_new_key and ALERT_ON_STARTUP):
                first_seen = now
                min_dt = now + timedelta(seconds=min_seconds) if min_seconds is not None else None
                max_dt = now + timedelta(seconds=max_seconds) if max_seconds is not None else None
                record = {
                    "mvp_name": mvp_name,
                    "map_name": map_name,
                    "killer_name": killer,
                    "respawn_text": respawn_text,
                    "first_seen_at": dt_to_iso(first_seen),
                    "first_seen_display": fmt_dt(first_seen),
                    "min_spawn_at": dt_to_iso(min_dt) if min_dt else None,
                    "max_spawn_at": dt_to_iso(max_dt) if max_dt else None,
                    "spawn_display": fmt_spawn_range(min_dt, max_dt),
                    "last_change_at": dt_to_iso(now),
                }
                mvp_database[key] = record
                changed_records.append(record)
            elif is_new_key:
                # Initial baseline only: the page does not say when this kill occurred.
                # Do not fabricate a spawn estimate until a later killer change is detected.
                mvp_database[key] = {
                    "mvp_name": mvp_name,
                    "map_name": map_name,
                    "killer_name": killer,
                    "respawn_text": respawn_text,
                    "first_seen_at": dt_to_iso(now),
                    "first_seen_display": fmt_dt(now),
                    "min_spawn_at": None,
                    "max_spawn_at": None,
                    "spawn_display": "Unknown — no kill timestamp was available at startup",
                    "last_change_at": dt_to_iso(now),
                }
            else:
                # Keep the last observed kill estimate; update only current display fields.
                existing = mvp_database.get(key, {})
                existing.update({
                    "mvp_name": mvp_name,
                    "map_name": map_name,
                    "killer_name": killer,
                    "respawn_text": respawn_text,
                })
                mvp_database[key] = existing

            known_last_killers[key] = killer

    save_state()

    if first_run_baseline:
        log.info("Initial baseline captured (%d rows); no historical alerts sent.", len(rows))

    for record in changed_records:
        log.info(
            "Detected changed last-killer entry: %s on %s (listed killer: %s)",
            record["mvp_name"], record["map_name"], record["killer_name"]
        )
        embed = embed_to_dict(
            "⚔️ MVP Ranking Update!",
            15158332,
            build_kill_fields(record),
        )
        send_webhook_alert(embed)

    log.info(
        "Processed %d MVP rows; changed last-killer entries: %d",
        len(rows), len(changed_records)
    )


def find_mvp_match(query):
    """Prefer exact matches, then unique substring/token matches."""
    query = query.strip().lstrip("!?").casefold()
    if len(query) < 2:
        return None, []

    with state_lock:
        records = list(mvp_database.values())

    exact = [
        r for r in records
        if query in (r["mvp_name"] or "").casefold()
        or query == (r["map_name"] or "").casefold()
    ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        # Exact monster name can occur on several maps; ask user to narrow it.
        return None, exact

    query_words = query.split()
    matches = []
    for record in records:
        monster = record.get("mvp_name", "").casefold()
        map_name = record.get("map_name", "").casefold()
        combined = f"{monster} {map_name}"
        if all(word in combined for word in query_words):
            matches.append(record)

    if len(matches) == 1:
        return matches[0], []
    return None, matches


intents = discord.Intents.default()
intents.message_content = True
bot_client = discord.Client(intents=intents)


@bot_client.event
async def on_ready():
    log.info("Discord bot logged in as %s", bot_client.user)


@bot_client.event
async def on_message(message):
    if message.author.bot:
        return

    query = message.content.strip()
    if not query or len(query.lstrip("!?").strip()) < 2:
        return

    record, matches = find_mvp_match(query)
    if record is None:
        if matches:
            choices = "\n".join(
                f"• **{r['mvp_name']}** — `{r['map_name']}`"
                for r in matches[:10]
            )
            await message.channel.send(
                "I found multiple matches. Include the map name to narrow it down:\n" + choices
            )
        return

    min_dt = iso_to_dt(record.get("min_spawn_at"))
    max_dt = iso_to_dt(record.get("max_spawn_at"))
    now = now_local()
    name = record.get("mvp_name", "Unknown")
    if name.casefold() in ALWAYS_ALIVE_OVERLOADS:
        status = "🟢 **ALWAYS ALIVE / SPECIAL RULE**"
        color = 3066993
    elif not min_dt or not max_dt:
        status = "⚪ **UNKNOWN** — no reliable spawn estimate is available."
        color = 9807270
    elif now < min_dt:
        status = "🔴 **NOT YET IN ESTIMATED SPAWN WINDOW**"
        color = 15158332
    elif now <= max_dt:
        status = "🟢 **ESTIMATED SPAWN WINDOW OPEN**"
        color = 3066993
    else:
        status = "🟠 **ESTIMATED WINDOW PASSED** — kill time may be inaccurate."
        color = 15105570

    embed = make_embed(
        f"⚔️ MVP Status: {name}",
        color,
        [
            ("Status", status, False),
            ("Last Killer (website)", record.get("killer_name", "Unknown"), True),
            ("Map", record.get("map_name", "Unknown"), True),
            ("Respawn Interval (website)", record.get("respawn_text", "Unknown"), True),
            ("First Detected (GMT+8)", record.get("first_seen_display", "Unknown"), False),
            ("Estimated Spawn Window (GMT+8)", record.get("spawn_display", "Unknown"), False),
        ],
    )
    await message.channel.send(embed=embed)


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal Render health endpoint; do not expose the app's files."""
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        body = json.dumps({
            "status": "ok",
            "service": "muhro-mvp-tracker",
            "last_scrape": getattr(self.server, "last_scrape", None),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps({
            "status": "ok",
            "service": "muhro-mvp-tracker",
            "last_scrape": getattr(self.server, "last_scrape", None),
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # No body for HEAD - that's the fix

    def log_message(self, fmt, *args):
        return


def run_web_server():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.daemon_threads = True
    log.info("Health server listening on 0.0.0.0:%d", PORT)
    server.serve_forever()


def run_scraper_loop():
    while True:
        try:
            check_mvp_kills()
        except requests.RequestException as exc:
            log.warning("Could not fetch MuhRO ranking page: %s", exc)
        except Exception:
            log.exception("MVP scrape failed.")
        time.sleep(POLL_SECONDS)


def validate_configuration():
    if not DISCORD_BOT_TOKEN:
        log.warning("DISCORD_BOT_TOKEN is missing; interactive Discord bot will not start.")
    if not DISCORD_WEBHOOK_1 and not DISCORD_WEBHOOK_2:
        log.warning("No webhook URL configured; webhook alerts are disabled.")
    if POLL_SECONDS < 15:
        log.warning("Very short polling intervals can cause unnecessary traffic.")


def main():
    global discord_client
    validate_configuration()
    load_state()

    threading.Thread(target=run_web_server, name="health-server", daemon=True).start()
    threading.Thread(target=run_scraper_loop, name="mvp-scraper", daemon=True).start()

    # Send a startup notice to configured webhooks.
    startup_embed = embed_to_dict(
        "🔌 MVP Tracker Started",
        3447003,
        [
            ("Status", "Service started successfully.", False),
            ("Poll Interval", f"{POLL_SECONDS} seconds", True),
            ("Timezone", "Asia/Manila (GMT+8)", True),
        ],
    )
    send_webhook_alert(startup_embed, "🔌 **MuhRO MVP Tracker Started**")

    if DISCORD_BOT_TOKEN:
        try:
            bot_client.run(DISCORD_BOT_TOKEN, log_handler=None)
        except Exception:
            log.exception("Discord bot stopped unexpectedly.")
            raise
    else:
        # Keep Render's health service alive even when only webhooks are used.
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
