import os
import sys
import time
import re
import logging
import threading
import requests
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from http.server import HTTPServer, SimpleHTTPRequestHandler

# Define Timezones
TZ_CEST = timezone(timedelta(hours=2)) # MuhRO Server Time (German CEST)
TZ_GMT8 = timezone(timedelta(hours=8)) # Philippines / Local Time (GMT+8)

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

# ==========================================
# HELPER: ACCURATE TIME & TIMEZONE PARSER
# ==========================================
def parse_fluxcp_time_to_gmt8(time_str):
    """
    Parses FluxCP timestamps (Server Time CEST / GMT+2) 
    and returns a datetime object converted directly into GMT+8 (+6 hours).
    """
    now_gmt8 = datetime.now(TZ_GMT8)
    
    # 1. Try parsing exact HH:MM:SS format from server
    time_match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', time_str)
    if time_match:
        h, m, s = map(int, time_match.groups())
        
        now_cest = datetime.now(TZ_CEST)
        kill_dt_cest = now_cest.replace(hour=h, minute=m, second=s, microsecond=0)
        
        # Midnight rollover handling
        if kill_dt_cest > now_cest:
            kill_dt_cest -= timedelta(days=1)
            
        # Convert CEST -> GMT+8 (+6 hour offset)
        return kill_dt_cest.astimezone(TZ_GMT8)

    # 2. Check for relative durations ('10 minutes ago', '2 hours ago')
    hours = re.search(r'(\d+)\s*(?:hour|hr)', time_str, re.IGNORECASE)
    minutes = re.search(r'(\d+)\s*(?:min|minute)', time_str, re.IGNORECASE)
    seconds = re.search(r'(\d+)\s*(?:sec|second)', time_str, re.IGNORECASE)

    h_val = int(hours.group(1)) if hours else 0
    m_val = int(minutes.group(1)) if minutes else 0
    s_val = int(seconds.group(1)) if seconds else 0

    if h_val or m_val or s_val:
        return now_gmt8 - timedelta(hours=h_val, minutes=m_val, seconds=s_val)

    return now_gmt8

def parse_duration_to_minutes(text):
    """Extracts total minutes from strings like '1 hour 10 minutes' or '2 hours'."""
    hours = re.search(r'(\d+)\s*(?:hour|hr)', text, re.IGNORECASE)
    minutes = re.search(r'(\d+)\s*(?:min|minute)', text, re.IGNORECASE)
    
    h_val = int(hours.group(1)) if hours else 0
    m_val = int(minutes.group(1)) if minutes else 0
    
    return (h_val * 60) + m_val

def calculate_spawn_range(death_dt_gmt8, respawn_text):
    """
    Calculates estimated spawn window using full YYYY-MM-DD HH:MM:SS timestamps for both min and max dates.
    """
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
    
    send_discord_alert("🧪 **MuhRO Tracker Online**: Full Date Formatting Applied (GMT+8)!")
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

                    # 1. Parse into a converted GMT+8 datetime object
                    death_dt_gmt8 = parse_fluxcp_time_to_gmt8(raw_time)
                    
                    # 2. Format the GMT+8 object for Time of Death display
                    time_of_death_gmt8 = death_dt_gmt8.strftime("%Y-%m-%d %H:%M:%S")
                    
                    # 3. Calculate Spawn Range formatted as YYYY-MM-DD HH:MM:SS ~ YYYY-MM-DD HH:MM:SS
                    est_spawn_str = calculate_spawn_range(death_dt_gmt8, respawn_text)

                    parsed_count += 1
                    kill_id = f"{mvp_name}_{killer_name}_{raw_time}"

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
                                    {"name": "Time of Death (GMT+8)", "value": time_of_death_gmt8, "inline": False},
                                    {"name": "Est. Spawn Window (GMT+8)", "value": est_spawn_str, "inline": False}
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
    print("   MuhRO MVP Tracker (Full Date Range | GMT+8)")
    print("==================================================")

    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    check_mvp_kills()

    while True:
        time.sleep(60)
        check_mvp_kills()
