import os
import sys
import time
import re
import logging
import threading
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from http.server import HTTPServer, SimpleHTTPRequestHandler
import discord

# ==========================================
# CONFIGURATION & ENVIRONMENT SETUP
# ==========================================
DISCORD_WEBHOOK_1 = os.environ.get("DISCORD_WEBHOOK_1", "")
DISCORD_WEBHOOK_2 = os.environ.get("DISCORD_WEBHOOK_2", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")

MVP_LOG_URL = "https://flux.muhro.eu/?module=ranking&action=mvp"
CACHE_FILE = "seen_kills.txt"

ALWAYS_ALIVE_OVERLOADS = []

seen_mvp_kills = set()
mvp_database = {}
warmup_mvp = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=3)
session.mount('https://', adapter)
session.mount('http://', adapter)

if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            seen_mvp_kills = set(line.strip() for line in f if line.strip())
        logging.info(f"📂 Loaded {len(seen_mvp_kills)} existing kills from disk cache.")
        warmup_mvp = False
    except Exception as e:
        logging.error(f"Failed to load cache file: {e}")

def save_cache():
    try:
        with open(CACHE_FILE, "w") as f:
            for kill_id in list(seen_mvp_kills)[-1000:]:
                f.write(f"{kill_id}\n")
    except Exception as e:
        logging.error(f"Failed to save cache file: {e}")

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def get_gmt8_from_scraped_time(time_str):
    time_match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', time_str)
    now_gmt8 = datetime.utcnow() + timedelta(hours=8)
    
    if time_match:
        h, m, s = map(int, time_match.groups())
        scraped_dt_gmt8 = now_gmt8.replace(hour=h, minute=m, second=s, microsecond=0)
        
        if scraped_dt_gmt8 > now_gmt8:
            scraped_dt_gmt8 -= timedelta(days=1)
            
        return scraped_dt_gmt8

    return None

def parse_duration_to_minutes(text):
    hours = re.search(r'(\d+)\s*(?:hour|hr)', text, re.IGNORECASE)
    minutes = re.search(r'(\d+)\s*(?:min|minute)', text, re.IGNORECASE)
    h_val = int(hours.group(1)) if hours else 0
    m_val = int(minutes.group(1)) if minutes else 0
    return (h_val * 60) + m_val

def calculate_spawn_range_dts(death_dt_gmt8, respawn_text):
    if not respawn_text or "N/A" in respawn_text.upper() or respawn_text.strip() == "0 min":
        return death_dt_gmt8, death_dt_gmt8

    parts = respawn_text.split("-")
    if len(parts) == 2:
        min_delay = parse_duration_to_minutes(parts[0]) or parse_duration_to_minutes(parts[1])
        max_delay = parse_duration_to_minutes(parts[1])
        return death_dt_gmt8 + timedelta(minutes=min_delay), death_dt_gmt8 + timedelta(minutes=max_delay)
    else:
        delay = parse_duration_to_minutes(respawn_text) or 0
        spawn_dt = death_dt_gmt8 + timedelta(minutes=delay)
        return spawn_dt, spawn_dt

def format_spawn_range_str(min_dt, max_dt):
    if min_dt == max_dt:
        return f"⏳ **{min_dt.strftime('%Y-%m-%d %H:%M:%S')}**"
    return f"⏳ **{min_dt.strftime('%Y-%m-%d %H:%M:%S')} ~ {max_dt.strftime('%Y-%m-%d %H:%M:%S')}**"

# ==========================================
# DISCORD BOT CLIENT
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot_client = discord.Client(intents=intents)

@bot_client.event
async def on_ready():
    logging.info(f"🤖 Discord Bot Logged In as {bot_client.user}")

@bot_client.event
async def on_message(message):
    if message.author == bot_client.user or message.author.bot:
        return

    raw_content = message.content.strip().lower()
    query = raw_content.lstrip("!?").strip()

    if len(query) < 2:
        return

    matched_mvp = None
    
    for mvp_key in mvp_database.keys():
        if query in mvp_key.lower() or mvp_key.lower() in query:
            matched_mvp = mvp_key
            break

    if not matched_mvp:
        query_words = query.split()
        for mvp_key in mvp_database.keys():
            key_lower = mvp_key.lower()
            if all(word in key_lower for word in query_words):
                matched_mvp = mvp_key
                break

    if matched_mvp:
        data = mvp_database[matched_mvp]
        now_gmt8 = datetime.utcnow() + timedelta(hours=8)
        
        if matched_mvp.lower() in ALWAYS_ALIVE_OVERLOADS or now_gmt8 >= data["min_spawn_dt"]:
            status_str = "🟢 **ALIVE / WINDOW OPEN**"
            color_code = 3066993
        else:
            status_str = "🔴 **DEAD**"
            color_code = 15158332

        embed = discord.Embed(
            title=f"⚔️ MVP Status: {matched_mvp}",
            color=color_code
        )
        embed.add_field(name="Status", value=status_str, inline=False)
        embed.add_field(name="Last Killed By", value=data["killer_name"], inline=True)
        embed.add_field(name="Map", value=data["map_name"], inline=True)
        embed.add_field(name="Time of Death (GMT+8)", value=data["time_of_death_str"], inline=False)
        embed.add_field(name="Est. Spawn Window (GMT+8)", value=data["est_spawn_str"], inline=False)
        
        await message.channel.send(embed=embed)
        logging.info(f"✅ Replied with status embed for matched MVP: '{matched_mvp}'")

def run_discord_bot():
    if DISCORD_BOT_TOKEN:
        bot_client.run(DISCORD_BOT_TOKEN)
    else:
        logging.warning("⚠️ DISCORD_BOT_TOKEN missing from environment variables!")

# ==========================================
# KEEP-ALIVE WEB SERVER
# ==========================================
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    logging.info(f"🌐 Keep-Alive HTTP Server bound to 0.0.0.0:{port}")
    httpd.serve_forever()

def send_discord_alert(content, embeds=None):
    webhooks = [url for url in [DISCORD_WEBHOOK_1, DISCORD_WEBHOOK_2] if url.strip()]
    if not webhooks:
        logging.error("❌ WEBHOOK ERROR: No webhook URLs found in environment variables!")
        return

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    for url in webhooks:
        try:
            response = session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logging.info(f"🚀 Successfully sent Discord webhook alert!")
        except Exception as e:
            logging.error(f"❌ Failed to send alert to webhook: {e}")

# ==========================================
# MVP SCRAPER LOOP (WITH ROW DEBUGGER)
# ==========================================
def check_mvp_kills():
    global warmup_mvp
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        res = session.get(MVP_LOG_URL, headers=headers, timeout=10)
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            table = soup.find("table")
            rows = table.find_all("tr") if table else soup.find_all("tr")
            parsed_count = 0
            new_alerts_sent = 0
            
            for row in rows:
                cols = [td.text.strip() for td in row.find_all("td") if td.text.strip()]
                if not cols:
                    continue
                
                # DEBUG: Print columns of every row to inspect the parsed layout
                logging.info(f"🔍 DEBUG ROW COLS: {cols}")

                # Anchor-based search: Find the column containing the timestamp pattern
                time_idx = -1
                raw_time = None
                for idx, col in enumerate(cols):
                    if re.search(r'\d{1,2}:\d{2}:\d{2}', col):
                        time_idx = idx
                        raw_time = col
                        break

                if time_idx == -1 or not raw_time:
                    continue

                mvp_name = cols[0]
                map_name = cols[1] if len(cols) > 1 else "unknown"
                respawn_text = cols[2] if len(cols) > 2 else "2 hours"
                killer_name = cols[time_idx - 1] if time_idx >= 1 else "Unknown"

                parsed_dt = get_gmt8_from_scraped_time(raw_time)
                if parsed_dt is None:
                    continue
                
                death_dt_gmt8 = parsed_dt
                time_of_death_str = death_dt_gmt8.strftime("%Y-%m-%d %H:%M:%S")
                min_spawn_dt, max_spawn_dt = calculate_spawn_range_dts(death_dt_gmt8, respawn_text)
                est_spawn_str = format_spawn_range_str(min_spawn_dt, max_spawn_dt)

                kill_id = f"{mvp_name}_{time_of_death_str}"

                mvp_database[mvp_name] = {
                    "killer_name": killer_name,
                    "map_name": map_name,
                    "time_of_death_str": time_of_death_str,
                    "death_dt_gmt8": death_dt_gmt8,
                    "est_spawn_str": est_spawn_str,
                    "min_spawn_dt": min_spawn_dt,
                    "max_spawn_dt": max_spawn_dt
                }

                parsed_count += 1

                if kill_id not in seen_mvp_kills:
                    seen_mvp_kills.add(kill_id)
                    
                    if not warmup_mvp:
                        new_alerts_sent += 1
                        logging.info(f"🎯 NEW KILL DETECTED: {mvp_name} on {map_name} by {killer_name} at {raw_time}")
                        embed = [{
                            "title": "⚔️ MVP Down!",
                            "color": 15158332,
                            "fields": [
                                {"name": "MVP", "value": mvp_name, "inline": True},
                                {"name": "Killed By", "value": killer_name, "inline": True},
                                {"name": "Map", "value": map_name, "inline": True},
                                {"name": "Time of Death (GMT+8)", "value": time_of_death_str, "inline": False},
                                {"name": "Est. Spawn Window (GMT+8)", "value": est_spawn_str, "inline": False}
                            ]
                        }]
                        send_discord_alert("🏆 **MuhRO MVP Kill Logged**", embed)

            save_cache()

            if warmup_mvp:
                warmup_mvp = False
                logging.info(f"✅ Initial warm-up complete. Scraped {len(mvp_database)} active MVPs into database.")

            logging.info(f"📊 Processed {parsed_count} entries. Alerts sent: {new_alerts_sent}")

    except Exception as e:
        logging.error(f"Error checking MVP kills: {e}")

def run_scraper_loop():
    check_mvp_kills()
    while True:
        time.sleep(15)
        check_mvp_kills()

if __name__ == "__main__":
    print("==================================================")
    print("   MuhRO MVP Tracker & Interactive Discord Bot")
    print("==================================================")

    threading.Thread(target=run_web_server, daemon=True).start()
    time.sleep(1)

    send_discord_alert("🔌 **MVP Tracker Started & Webhook Connected!**")

    threading.Thread(target=run_scraper_loop, daemon=True).start()

    run_discord_bot()
