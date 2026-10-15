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

# Force specific MVPs to always display as ALIVE / WINDOW OPEN (Keep empty for automated tracking)
ALWAYS_ALIVE_OVERLOADS = []

# State Tracking
seen_mvp_kills = set()
mvp_database = {}  # In-memory database storing MVP status & timestamps
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
    """Parses scraped HH:MM:SS string and accurately calculates GMT+8 death time."""
    time_match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', time_str)
    now_gmt8 = datetime.utcnow() + timedelta(hours=8)
    
    if time_match:
        h, m, s = map(int, time_match.groups())
        scraped_dt_gmt8 = now_gmt8.replace(hour=h, minute=m, second=s, microsecond=0)
        
        # If scraped hour is greater than current GMT+8 hour, it belonged to yesterday
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
    if message.author == bot_client.user:
        return

    logging.info(f"📩 RECEIVED MESSAGE: '{message.content}' from {message.author} in #{message.channel}")

    raw_content = message.content.strip().lower()
    query = raw_content.lstrip("!?").strip()

    if len(query) < 3:
        return

    matched_mvp = None
    for mvp_key in mvp_database.keys():
        if query in mvp_key.lower():
            matched_mvp = mvp_key
            break

    if matched_mvp:
        data = mvp_database[matched_mvp]
        now_gmt8 = datetime.utcnow() + timedelta(hours=8)
        
        # Turn GREEN as soon as current time reaches or passes min_spawn_dt
        if matched_mvp.lower() in ALWAYS_ALIVE_OVERLOADS or now_gmt8 >= data["min_spawn_dt"]:
            status_str = "🟢 **ALIVE / WINDOW OPEN**"
            color_code = 3066993  # Green
        else:
            status_str = "🔴 **DEAD**"
            color_code = 15158332  # Red

        embed = discord.Embed(
            title=f"⚔️ MVP Status: {matched_mvp}",
            color=color_code
        )
        embed.add_field(name="Status", value=status_str, inline=False)
        embed.add
