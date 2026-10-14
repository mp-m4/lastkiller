import os
import sys
import time
import logging
import threading
import requests
from bs4 import BeautifulSoup
from http.server import HTTPServer, SimpleHTTPRequestHandler

# ==========================================
# CONFIGURATION & ENVIRONMENT SETUP
# ==========================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MVP_LOG_URL = "https://flux.muhro.eu/?module=ranking&action=mvp"

# Global state memory
seen_mvp_kills = set()
warmup_mvp = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==========================================
# KEEP-ALIVE WEB SERVER FOR RENDER
# ==========================================
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    logging.info(f"🌐 Keep-Alive HTTP Server bound to 0.0.0.0:{port}")
    
    # Send test ping to verify Discord webhook on boot
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
                cols = [td.text.strip() for td in row.find_all("td")]
                
                if len(cols) >= 3:
                    # Ignore header or invalid rows
                    if "MVP" in cols[0] or "Map" in cols[0] or not cols[0]:
                        continue

                    # Flexible column handling
                    if len(cols) == 3:
                        mvp_name, killer_name, kill_time = cols[0], cols[1], cols[2]
                    else:
                        mvp_name, killer_name, kill_time = cols[0], cols[-2], cols[-1]
                    
                    parsed_count += 1
                    kill_id = f"{mvp_name}_{killer_name}_{kill_time}"

                    if kill_id not in seen_mvp_kills:
                        seen_mvp_kills.add(kill_id)
                        
                        if not warmup_mvp:
                            new_alerts_sent += 1
                            logging.info(f"🎯 NEW KILL DETECTED: {mvp_name} by {killer_name} ({kill_time})")
                            embed = [{
                                "title": "⚔️ MVP Down!",
                                "color": 15158332,
                                "fields": [
                                    {"name": "MVP", "value": mvp_name, "inline": True},
                                    {"name": "Killed By", "value": killer_name, "inline": True},
                                    {"name": "Time", "value": kill_time, "inline": True}
                                ]
                            }]
                            send_discord_alert("🏆 **MuhRO MVP Kill Logged**", embed)

            # Prevent memory bloat over long runs
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

    # Initial warm-up run
    check_mvp_kills()

    # Fast 60-second polling loop
    while True:
        time.sleep(60)
        check_mvp_kills()
