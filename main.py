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
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1559883969611304970/AOs1D3e4HwCzgiZPLqvkB6PwFC5cxGJrGVRQ2SjXw4DchV9THkrK3NqAcraTmM-GhazN")
MVP_LOG_URL = "https://flux.muhro.eu/?module=ranking&action=mvp"

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
    httpd.serve_forever()

# ==========================================
# DISCORD ALERT HELPER
# ==========================================
def send_discord_alert(content, embeds=None):
    if not DISCORD_WEBHOOK_URL:
        logging.warning("DISCORD_WEBHOOK_URL environment variable is missing. Skipping alert.")
        return

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("🚀 Discord Webhook alert sent successfully!")
    except Exception as e:
        logging.error(f"Failed to send Discord alert: {e}")

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
            
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 3:
                    mvp_name = cols[0].text.strip()
                    killer_name = cols[1].text.strip()
                    kill_time = cols[2].text.strip()
                    
                    # Ignore empty headers or non-entry rows
                    if not mvp_name or "MVP" in mvp_name:
                        continue
                        
                    parsed_count += 1
                    kill_id = f"{mvp_name}_{killer_name}_{kill_time}"

                    if kill_id not in seen_mvp_kills:
                        seen_mvp_kills.add(kill_id)
                        
                        if not warmup_mvp:
                            logging.info(f"🎯 New MVP Kill detected: {kill_id}")
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

            logging.info(f"📊 Processed {parsed_count} MVP entries.")

            if warmup_mvp:
                warmup_mvp = False
                logging.info("✅ MVP Killer warm-up complete. Initial logs cached silently.")

    except Exception as e:
        logging.error(f"Error checking MVP kills: {e}")

# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================
if __name__ == "__main__":
    print("==================================================")
    print("      MuhRO MVP Killer Tracker Started")
    print("      Checking every 300 seconds...")
    print("==================================================")

    # 1. Start HTTP Server in background
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()

    # 2. Silent Warm-up
    check_mvp_kills()

    # 3. Main Loop
    while True:
        time.sleep(300)
        check_mvp_kills()
