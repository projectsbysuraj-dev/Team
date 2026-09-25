import os
import hmac
import hashlib
import urllib.parse
import json
import time
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ================= CONFIGURATION =================
app = Flask(__name__)
CORS(app) # Production mein specific frontend URL daal dena

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "RohitGiveawayBot")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_USER = os.getenv("CHANNEL_USERNAME")
MINI_APP_URL = os.getenv("MINI_APP_URL")
ADMIN_PIN = os.getenv("ADMIN_PIN", "1234")
ADMIN_TOKEN = "rg_secure_token_2024"

# Supabase Setup
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") # ⚠️ Yahan service_role key hi dalna!
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ================= SECURITY (AUTH) =================
def validate_init_data(init_data):
    if not BOT_TOKEN or not init_data: return None
    try:
        parsed = urllib.parse.parse_qs(init_data)
        received_hash = parsed.get("hash", [None])[0]
        if not received_hash: return None
        
        data_check_arr = [f"{k}={v[0]}" for k, v in parsed.items() if k != "hash"]
        data_check_arr.sort()
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, "\n".join(data_check_arr).encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(computed_hash, received_hash): return None
        
        user = json.loads(parsed.get("user", [None])[0])
        if int(time.time()) - int(parsed.get("auth_date", [0])[0]) > 86400: return None
        return {"telegram_id": user.get("id"), "username": user.get("username"), "first_name": user.get("first_name")}
    except: return None

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        init_data = request.headers.get("X-Telegram-Init-Data")
        user = validate_init_data(init_data)
        if not user: return jsonify({"error": "Unauthorized"}), 401
        request.telegram_user = user
        return f(*args, **kwargs)
    return wrapper

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-Admin-Token") != ADMIN_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

# ================= HELPER FUNCTIONS =================
def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: data["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=data)

def check_channel_membership(user_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
    res = requests.get(url, params={"chat_id": CHANNEL_ID, "user_id": user_id}).json()
    return res.get("result", {}).get("status") in ["member", "administrator", "creator"]

def get_or_create_user(tg_id, username, first_name, ref_code=None):
    res = supabase.table("users").select("*").eq("telegram_id", tg_id).execute()
    if res.data: return res.data[0]
    
    referred_by = None
    if ref_code:
        ref_user = supabase.table("users").select("*").eq("referral_code", ref_code.replace("ref_", "").upper()).execute()
        if ref_user.data and ref_user.data[0]["telegram_id"] != tg_id:
            referred_by = ref_user.data[0]["telegram_id"]

    import random, string
    new_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    new_user = {"telegram_id": tg_id, "username": username, "first_name": first_name, "referral_code": new_code, "referred_by": referred_by, "wallet_balance": 0.00, "spins": 0, "verified": False}
    return supabase.table("users").insert(new_user).execute().data[0]

# ================= PUBLIC & USER ROUTES =================
@app.route("/")
def home():
    return jsonify({"status": "ok", "message": "Rohit Giveaway API Running!"})

@app.route("/api/settings", methods=["GET"])
def get_settings():
    res = supabase.table("app_settings").select("key, value").in_("key", ["bot_username", "channel_link", "app_title", "spin_win_amount", "min_withdrawal", "theme_primary", "theme_accent"]).execute()
    return jsonify({item["key"]: item["value"] for item in res.data})

@app.route("/api/me", methods=["GET"])
@require_auth
def get_me():
    tg = request.telegram_user
    user = get_or_create_user(tg["telegram_id"], tg.get("username"), tg.get("first_name"))
    refs = supabase.table("referral_rewards").select("id", count="exact").eq("referrer_id", user["id"]).execute()
    return jsonify({
        "telegram_id": user["telegram_id"], "first_name": user["first_name"], "username": user["username"],
        "wallet_balance": float(user["wallet_balance"]), "spins": int(user["spins"]), "verified": user["verified"],
        "referral_count": refs.count or 0, "referral_link": f"https://t.me/{BOT_USERNAME}?start=ref_{user['referral_code']}"
    })

@app.route("/api/spin", methods=["POST"])
@require_auth
def spin():
    user = supabase.table("users").select("*").eq("telegram_id", request.telegram_user["telegram_id"]).execute().data[0]
    if int(user["spins"]) <= 0: return jsonify({"error": "No spins available"}), 400
    
    settings = supabase.table("app_settings").select("value").eq("key", "spin_win_amount").execute()
    win_amount = float(settings.data[0]["value"]) if settings.data else 5.0
    
    new_spins = int(user["spins"]) - 1
    new_balance = float(user["wallet_balance"]) + win_amount
    
    supabase.table("users").update({"spins": new_spins, "wallet_balance": new_balance}).eq("id", user["id"]).execute()
    supabase.table("transactions").insert({"user_id": user["id"], "type": "spin_win", "amount": win_amount, "description": "Lucky Spin Win"}).execute()
    return jsonify({"success": True, "win_amount": win_amount, "new_balance": new_balance, "new_spins": new_spins})

@app.route("/api/withdraw", methods=["POST"])
@require_auth
def withdraw():
    data = request.json or {}
    user = supabase.table("users").select("*").eq("telegram_id", request.telegram_user["telegram_id"]).execute().data[0]
    amount = float(data.get("amount", 0))
    method = data.get("method")
    
    min_wd = float(supabase.table("app_settings").select("value").eq("key", "min_withdrawal").execute().data[0]["value"])
    if amount < min_wd: return jsonify({"error": f"Minimum withdrawal is ₹{min_wd}"}), 400
    if float(user["wallet_balance"]) < amount: return jsonify({"error": "Insufficient balance"}), 400

    supabase.table("users").update({"wallet_balance": float(user["wallet_balance"]) - amount}).eq("id", user["id"]).execute()
    
    wd_data = {"user_id": user["id"], "amount": amount, "payment_method": method, "status": "pending"}
    if method == "upi": wd_data["upi_id"] = data.get("upi_id")
    else:
        wd_data["account_holder"] = data.get("account_holder")
        wd_data["bank_name"] = data.get("bank_name")
        wd_data["account_number"] = data.get("account_number")
        wd_data["ifsc_code"] = data.get("ifsc_code")
        
    supabase.table("withdrawals").insert(wd_data).execute()
    supabase.table("transactions").insert({"user_id": user["id"], "type": "withdrawal", "amount": -amount, "description": f"Withdrawal via {method}"}).execute()
    return jsonify({"success": True})

# ================= ADMIN ROUTES =================
@app.route("/admin/login", methods=["POST"])
def admin_login():
    if request.json.get("pin") == ADMIN_PIN:
        return jsonify({"success": True, "token": ADMIN_TOKEN})
    return jsonify({"error": "Invalid PIN"}), 401

@app.route("/admin/withdrawals", methods=["GET"])
@require_admin
def admin_withdrawals():
    res = supabase.table("withdrawals").select("*, users!withdrawals_user_id_fkey(telegram_id, username)").order("created_at", desc=True).execute()
    return jsonify(res.data)

@app.route("/admin/withdrawals/<int:wd_id>", methods=["PATCH"])
@require_admin
def admin_update_wd(wd_id):
    data = request.json or {}
    wd = supabase.table("withdrawals").select("*").eq("id", wd_id).execute().data[0]
    supabase.table("withdrawals").update({"status": data.get("status"), "transaction_ref": data.get("transaction_ref"), "processed_at": datetime.utcnow().isoformat()}).eq("id", wd_id).execute()
    
    if data.get("status") == "rejected":
        user = supabase.table("users").select("*").eq("id", wd["user_id"]).execute().data[0]
        supabase.table("users").update({"wallet_balance": float(user["wallet_balance"]) + float(wd["amount"])}).eq("id", wd["user_id"]).execute()
    return jsonify({"success": True})

@app.route("/admin/update_user", methods=["POST"])
@require_admin
def admin_update_user():
    data = request.json or {}
    user = supabase.table("users").select("*").eq("telegram_id", int(data.get("telegram_id"))).execute().data[0]
    updates = {}
    if data.get("spins") is not None: updates["spins"] = int(data["spins"])
    if data.get("balance") is not None: updates["wallet_balance"] = float(data["balance"])
    if updates: supabase.table("users").update(updates).eq("id", user["id"]).execute()
    return jsonify({"success": True})

@app.route("/admin/settings", methods=["POST"])
@require_admin
def admin_save_settings():
    data = request.json or {}
    for key, value in data.items():
        supabase.table("app_settings").update({"value": str(value)}).eq("key", key).execute()
    return jsonify({"success": True})

# ================= BOT WEBHOOK =================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.json
    if "message" in update:
        msg = update["message"]
        text = msg.get("text", "")
        user = msg.get("from", {})
        chat_id = user.get("id")
        
        if text.startswith("/start"):
            ref_code = text.split(" ", 1)[1] if len(text.split(" ")) > 1 else None
            get_or_create_user(user["id"], user.get("username"), user.get("first_name"), ref_code)
            
            kb = {
                "inline_keyboard": [
                    [{"text": "📢 Join Channel", "url": f"https://t.me/{CHANNEL_USER}"}],
                    [{"text": "✅ Verify & Open App", "callback_data": "verify"}],
                    [{"text": "🚀 Open App", "web_app": {"url": MINI_APP_URL}}]
                ]
            }
            send_telegram_message(chat_id, f"👋 Welcome {user.get('first_name')}!\nJoin channel and verify to start earning!", kb)
            
    elif "callback_query" in update:
        cb = update["callback_query"]
        if cb.get("data") == "verify":
            chat_id = cb["message"]["chat"]["id"]
            user_id = cb["from"]["id"]
            msg_id = cb["message"]["message_id"]
            
            if not check_channel_membership(user_id):
                kb = {"inline_keyboard": [[{"text": "📢 Join Channel", "url": f"https://t.me/{CHANNEL_USER}"}], [{"text": "🔄 Try Again", "callback_data": "verify"}]]}
                send_telegram_message(chat_id, "❌ Please join the channel first!", kb)
                return jsonify({"ok": True})
            
            # Verify & Credit Referrer
            user = supabase.table("users").select("*").eq("telegram_id", user_id).execute().data[0]
            supabase.table("users").update({"verified": True, "channel_joined": True}).eq("id", user["id"]).execute()
            
            if user.get("referred_by"):
                referrer = supabase.table("users").select("*").eq("telegram_id", user["referred_by"]).execute().data[0]
                if referrer:
                    supabase.table("users").update({"spins": int(referrer["spins"]) + 1}).eq("id", referrer["id"]).execute()
                    supabase.table("referral_rewards").insert({"referrer_id": referrer["id"], "referee_id": user["id"], "spins_awarded": 1}).execute()

            kb = {"inline_keyboard": [[{"text": "🚀 Open Earning App", "web_app": {"url": MINI_APP_URL}}]]}
            send_telegram_message(chat_id, "✅ Verified! You are ready to earn.", kb)
            
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)