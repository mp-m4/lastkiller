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

# ==========================================
# CONFIGURATION & ENVIRONMENT SETUP
# ==========================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MVP_LOG_URL = "https://flux.muhro.eu/?module=ranking&action=mvp"
CACHE_FILE = "seen_kills.txt"

seen_mvp_kills = set()
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

# Load existing cache from disk if available
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, "r") as f:
            seen_mvp_kills = set(line.strip() for line in f if line.strip())
        logging.info(f"📂 Loaded {len(seen_mvp_kills)} existing kills from disk cache.")
        warmup_mvp = False  # Skip warmup if cache exists
    except Exception as e:
        logging.error(f"Failed to load cache file: {e}")

def save_cache():
    """Saves up to 1000 recent kill IDs to local disk."""
    try:
        with open(CACHE_FILE, "w") as f:
            for kill_id in list(seen_mvp_kills)[-1000:]:
                f.write(f"{kill_id}\n")
    except Exception as e:
        logging.error(f"Failed to save cache file: {e}")

# ==========================================
# HELPER: DIRECT +8 HOURS ADDITION
# ==========================================
def get_gmt8_from_scraped_time(time_str):
    """Takes the raw website time string (HH:MM:SS) and adds EXACTLY 8 hours."""
    time_match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', time_str)
    if time_match:
        h, m, s = map(int, time_match.groups())
        now = datetime.utcnow()
        scraped_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
        return scraped_dt + timedelta(hours=8)

    return datetime.utcnow() + timedelta(hours=8)

def parse_duration_to_minutes(text):
    """Extracts total minutes from strings like '1 hour 10 minutes' or '2 hours'."""
    hours = re.search(r'(\d+)\s*(?:hour|hr)', text, re.IGNORECASE)
    minutes = re.search(r'(\d+)\s*(?:min|minute)', text, re.IGNORECASE)
    
    h_val = int(hours.group(1)) if hours else 0
    m_val = int(minutes.group(1)) if minutes else 0
    
    return (h_val * 60) + m_val

def calculate_spawn_range(death_dt_gmt8, respawn_text):
    """Calculates spawn window by adding respawn minutes to death_dt_gmt8."""
    if not respawn_text or "N/A" in respawn_text.upper():
        est_dt = death_dt_gmt8 + timedelta(minutes=60)
        return f"⏳ **{est_dt.strftime('%Y-%m-%d %H:%M:%S')}**"

    parts = respawn_text.split("-")
    
    if len(parts) == 2:
        min_delay = parse_duration_to_minutes(parts[0])
        max_delay = parse_duration_to_minutes(parts[1])
        
        if min_delay == 0:
            min_delay = max_delay

        min_spawn = (death_dt_gmt8 + timedelta(minutes=min_delay)).strftime("%Y-%m-%d %H:%M:%S")
        max_spawn = (death_dt_gmt8 + timedelta(minutes=max_delay)).strftime("%Y-%m-%d %H:%M:%S")
        
        return f"⏳ **{min_spawn} ~ {max_spawn}**"
    else:
        delay = parse_duration_to_minutes(respawn_text)
        if delay == 0:
            delay = 60
        spawn_dt = death_dt_gmt8 + timedelta(minutes=delay)
        return f"⏳ **{spawn_dt.strftime('%Y-%m-%d %H:%M:%S')}**"

# ==========================================
# KEEP-ALIVE WEB SERVER FOR RENDER
# ==========================================
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    logging.info(f"🌐 Keep-Alive HTTP Server bound to 0.0.0.0:{port}")
    
    send_discord_alert("🧪 **MuhRO Tracker Online**: Strict Collision-Proof Tracker Active!")
    httpd.serve_forever()

# ==========================================
# DISCORD ALERT HELPER
# ==========================================
def send_discord_alert(content, embeds=None):
    if not DISCORD_WEBHOOK_URL:
        logging.warning("⚠️ DISCORD_WEBHOOK_URL environment variable is missing!")
        return

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    try:
        response = session.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("🚀 Discord alert delivered!")
    except Exception as e:
        logging.error(f"❌ Failed to send Discord alert: {e}")

# ==========================================
# MVP KILLER MONITORING LOGIC
# ==========================================
def check_mvp_kills():
    global warmup_mvp
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        logging.info("🔍 Scraping FluxCP MVP rankings...")
        res = session.get(MVP_LOG_URL, headers=headers, timeout=10)
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            rows = soup.find_all("tr")
            parsed_count = 0
            new_alerts_sent = 0
            
            # Process table rows
            for idx, row in enumerate(rows):
                cols = [" ".join(td.text.split()) for td in row.find_all("td")]
                
                if len(cols) >= 4:
                    if "MVP" in cols[0] or "Monster" in cols[0] or not cols[0]:
                        continue

                    mvp_name = cols[0]
                    map_name = cols[1]
                    respawn_text = cols[2]
                    killer_name = cols[-2]
                    raw_time = cols[-1]

                    if any(t in killer_name.lower() for t in ["min", "hour", "sec", "ago", ":"]):
                        killer_name, raw_time = raw_time, killer_name

                    death_dt_gmt8 = get_gmt8_from_scraped_time(raw_time)
                    time_of_death_str = death_dt_gmt8.strftime("%Y-%m-%d %H:%M:%S")
                    est_spawn_str = calculate_spawn_range(death_dt_gmt8, respawn_text)

                    parsed_count += 1
                    
                    # Create strict unique identifier combining content + timestamp
                    kill_id = f"{mvp_name}_{map_name}_{killer_name}_{raw_time}"

                    if kill_id not in seen_mvp_kills:
                        seen_mvp_kills.add(kill_id)
                        
                        if not warmup_mvp:
                            new_alerts_sent += 1
                            logging.info(f"🎯 NEW KILL DETECTED: {mvp_name} on {map_name} by {killer_name}")
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
                logging.info("✅ Initial warm-up complete. Cache file saved to disk.")

            logging.info(f"📊 Processed {parsed_count} entries. Alerts sent: {new_alerts_sent}")

    except Exception as e:
        logging.error(f"Error checking MVP kills: {e}")

# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    print("==================================================")
    print("   MuhRO MVP Tracker (Disk Cache + Fast 15s Loop)")
    print("==================================================")

    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    check_mvp_kills()

    while True:
        time.sleep(15)  # Fast 15-second loop to prevent missing fast successive kills
        check_mvp_kills()
