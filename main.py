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

seen_mvp_kills = set()
warmup_mvp = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Known MVP Respawn Delays in Minutes (Default: 60)
MVP_SPAWN_DELAYS = {
    "Randgris": 480,      # Valkyrie Randgris (8 Hours)
    "Valkyrie": 480,
    "Ifrit": 660,         # Ifrit (11 Hours)
    "Beelzebub": 720,     # Beelzebub (12 Hours)
    "Lord of Death": 120, # Lord of Death (2 Hours)
    "Tao Gunka": 300,     # Tao Gunka (5 Hours)
    "Baphomet": 120,      # Baphomet (2 Hours)
    "Pharaoh": 60,
    "Drake": 120,
    "Osiris": 60,
    "Edgga": 120,
    "Maya": 120,
    "Phreeoni": 120,
    "Doppelganger": 120,
    "Orc Hero": 60,
    "Orc Lord": 120,
}

# ==========================================
# HELPER: TIME CALCULATION
# ==========================================
def parse_relative_time_to_dt(time_str):
    """Parses relative time strings ('8 hours 10 minutes') into a datetime object."""
    now = datetime.now()
    
    hours = re.search(r'(\d+)\s*(?:hour|hr)', time_str, re.IGNORECASE)
    minutes = re.search(r'(\d+)\s*(?:min|minute)', time_str, re.IGNORECASE)
    seconds = re.search(r'(\d+)\s*(?:sec|second)', time_str, re.IGNORECASE)

    h_val = int(hours.group(1)) if hours else 0
    m_val = int(minutes.group(1)) if minutes else 0
    s_val = int(seconds.group(1)) if seconds else 0

    if h_val or m_val or s_val:
        return now - timedelta(hours=h_val, minutes=m_val, seconds=s_val)
    
    return now

def calculate_spawn_time(mvp_name, death_dt):
    """Calculates estimated spawn timestamp based on known spawn windows."""
    delay_minutes = 60
    for key, delay in MVP_SPAWN_DELAYS.items():
        if key.lower() in mvp_name.lower():
            delay_minutes = delay
            break

    estimated_spawn_dt = death_dt + timedelta(minutes=delay_minutes)
    return estimated_spawn_dt.strftime("%Y-%m-%d %H:%M:%S")

# ==========================================
# KEEP-ALIVE WEB SERVER FOR RENDER
# ==========================================
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    logging.info(f"🌐 Keep-Alive HTTP Server bound to 0.0.0.0:{port}")
    
    send_discord_alert("🧪 **MuhRO Tracker Online**: Webhook connected. Monitoring every 60s!")
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
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
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
        res = requests.get(MVP_LOG_URL, headers=headers, timeout=15)
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            rows = soup.find_all("tr")
            parsed_count = 0
            new_alerts_sent = 0
            
            for row in rows:
                cols = [" ".join(td.text.split()) for td in row.find_all("td")]
                
                if len(cols) >= 3:
                    if "MVP" in cols[0] or "Map" in cols[0] or not cols[0]:
                        continue

                    mvp_name = cols[0]
                    raw_time = cols[-1]
                    
                    if len(cols) == 3:
                        killer_name = cols[1]
                    elif len(cols) == 4:
                        killer_name = cols[2]
                    else:
                        killer_name = cols[-2]

                    # Column swap correction
                    if any(t in killer_name.lower() for t in ["min", "hour", "sec", "ago"]):
                        killer_name, raw_time = raw_time, killer_name

                    # Calculate Exact Timestamps
                    death_dt = parse_relative_time_to_dt(raw_time)
                    time_of_death = death_dt.strftime("%Y-%m-%d %H:%M:%S")
                    est_spawn_time = calculate_spawn_time(mvp_name, death_dt)

                    parsed_count += 1
                    kill_id = f"{mvp_name}_{killer_name}_{raw_time}"

                    if kill_id not in seen_mvp_kills:
                        seen_mvp_kills.add(kill_id)
                        
                        if not warmup_mvp:
                            new_alerts_sent += 1
                            logging.info(f"🎯 NEW KILL DETECTED: {mvp_name} by {killer_name}")
                            embed = [{
                                "title": "⚔️ MVP Down!",
                                "color": 15158332,
                                "fields": [
                                    {"name": "MVP", "value": mvp_name, "inline": True},
                                    {"name": "Killed By", "value": killer_name, "inline": True},
                                    {"name": "Time of Death", "value": time_of_death, "inline": False},
                                    {"name": "Est. Spawn Time", "value": f"⏳ **{est_spawn_time}**", "inline": False}
                                ]
                            }]
                            send_discord_alert("🏆 **MuhRO MVP Kill Logged**", embed)

            if len(seen_mvp_kills) > 300:
                seen_mvp_kills.clear()

            logging.info(f"📊 Processed {parsed_count} entries. Alerts sent: {new_alerts_sent}")

            if warmup_mvp:
                warmup_mvp = False
                logging.info("✅ Warm-up complete. Initial logs cached silently.")

    except Exception as e:
        logging.error(f"Error checking MVP kills: {e}")

# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    print("==================================================")
    print("      MuhRO MVP Tracker (60s Fast Loop)")
    print("==================================================")

    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    check_mvp_kills()

    while True:
        time.sleep(60)
        check_mvp_kills()
