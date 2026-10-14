import os
import time
import requests
from bs4 import BeautifulSoup

# ---------------- CONFIGURATION ----------------
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1559883969611304970/AOs1D3e4HwCzgiZPLqvkB6PwFC5cxGJrGVRQ2SjXw4DchV9THkrK3NqAcraTmM-GhazN"
MVP_RANKING_URL = "https://flux.muhro.eu/?module=ranking&action=mvp"

# Memory tracking to avoid duplicate pings
# Stores key-value pair: { "MVP_NAME - MAP": "LAST_KILLER_NAME" }
last_known_killers = {}

is_initial_killer_warmup = True
PING_DELAY_SECONDS = 2


def send_killer_alert(mvp_name, map_location, killer_name):
    """Sends a formatted embed alert to Discord when an MVP is killed."""
    payload = {
        "content": f"⚔️ **MVP DOWN!** Player **{killer_name}** killed **{mvp_name}**!",
        "embeds": [
            {
                "title": f"☠️ {mvp_name}",
                "color": 10038562,  # Dark Red
                "fields": [
                    {"name": "Last Killer", "value": str(killer_name), "inline": True},
                    {"name": "Map Location", "value": str(map_location), "inline": True},
                    {"name": "Timestamp", "value": time.strftime('%Y-%m-%d %H:%M:%S'), "inline": True}
                ],
                "footer": {"text": "MuhRO MVP Killer Tracker"}
            }
        ]
    }
    
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Webhook failed: {e}")


def process_mvp_killers():
    """Scrapes MuhRO FluxCP MVP ranking and alerts on new last killers."""
    global is_initial_killer_warmup
    timestamp = time.strftime('%H:%M:%S')
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        res = requests.get(MVP_RANKING_URL, headers=headers, timeout=15)
        if res.status_code != 200:
            print(f"[{timestamp}] Failed to fetch MVP ranking: HTTP {res.status_code}")
            return
    except Exception as e:
        print(f"[{timestamp}] Request error fetching MVP ranking: {e}")
        return

    soup = BeautifulSoup(res.text, 'html.parser')
    
    # Locate the ranking table
    tables = soup.find_all('table')
    target_table = None

    for table in tables:
        headers_text = table.text.lower()
        if "monster" in headers_text and "last killer" in headers_text:
            target_table = table
            break

    if not target_table:
        return

    rows = target_table.find_all('tr')
    
    for row in rows:
        cols = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
        
        # Expected row structure: [Monster, Map Location, Respawn, Last Killer]
        if len(cols) >= 4 and cols[0].lower() != "monster":
            mvp_name = cols[0]
            map_location = cols[1]
            last_killer = cols[3]

            # Skip entries where no killer is recorded yet
            if not last_killer or last_killer == "-":
                continue

            # Unique key for MVP + Map combination
            mvp_key = f"{mvp_name} @ {map_location}"

            # Check if killer changed
            if mvp_key in last_known_killers:
                previous_killer = last_known_killers[mvp_key]
                if previous_killer != last_killer:
                    if not is_initial_killer_warmup:
                        print(f"[{timestamp}] ⚔️ {mvp_name} on {map_location} killed by {last_killer} (was: {previous_killer})")
                        send_killer_alert(mvp_name, map_location, last_killer)
                        time.sleep(PING_DELAY_SECONDS)
                    last_known_killers[mvp_key] = last_killer
            else:
                # First time seeing this MVP entry
                last_known_killers[mvp_key] = last_killer

    if is_initial_killer_warmup:
        print(f"[{timestamp}] ✅ MVP Killer warm-up complete. Cached {len(last_known_killers)} last killers silently.")
        is_initial_killer_warmup = False
