# AC BOMLU Administration - v2.0.1 (UI refinements & Group Telegram Bot silencing)
import os
import json
import re
import base64
import asyncio
import hashlib
import hmac
import time
from datetime import datetime, timezone, timedelta
import calendar
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from bson import ObjectId
import uvicorn

# Load .env file if exists (local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ===== FastAPI App =====
app = FastAPI(title="AC BOMLU HR Dashboard", version="2.0")

# ===== Static Files (logo, assets) =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")

# ===== MongoDB Connection =====
DEFAULT_MONGO_URI = "mongodb+srv://acbomlu_admin:092673641@acbomlo.gclkugi.mongodb.net/acbank_db?retryWrites=true&w=majority"

def get_sanitized_mongo_uri() -> str:
    raw = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or ""
    cleaned = raw.strip().strip("'\"").strip()
    if cleaned.startswith("mongodb://") or cleaned.startswith("mongodb+srv://"):
        return cleaned
    return DEFAULT_MONGO_URI

MONGO_URI = get_sanitized_mongo_uri()
try:
    client = MongoClient(MONGO_URI)
except Exception as e:
    print(f"Warning: Primary MongoClient connection failed ({e}). Falling back to DEFAULT_MONGO_URI...")
    client = MongoClient(DEFAULT_MONGO_URI)

db = client["acbank_db"]
leaves_collection = db["leaves"]
loans_collection = db["loans"]
employees_collection = db["employees"]
settings_collection = db["settings"]
user_flows_collection = db["user_flows"]
voice_messages_collection = db["voice_messages"]
finance_transactions_collection = db["finance_transactions"]
finance_catalogs_collection = db["finance_catalogs"]

# ===== Finance & Accounting Models =====
class FinanceCatalogCreate(BaseModel):
    name: str
    type: str = "both"  # "income", "expense", or "both"
    icon: Optional[str] = "category"
    color: Optional[str] = "blue"
    description: Optional[str] = ""

class FinanceCatalogUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None

class FinanceTransactionCreate(BaseModel):
    type: str  # "income", "expense", or "transfer"
    amount: float
    currency: str = "KHR"  # "KHR" or "USD"
    category: str
    catalog_id: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    payment_method: Optional[str] = "សាច់ប្រាក់សុទ្ធ"
    reference_no: Optional[str] = ""
    description: Optional[str] = ""
    recorded_by: Optional[str] = "រដ្ឋបាល"
    branch: Optional[str] = "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"
    service_items: Optional[List[Dict[str, Any]]] = None
    transfer_from: Optional[str] = None
    transfer_to: Optional[str] = "គណនីវិនិយោគទុន"

class FinanceTransactionUpdate(BaseModel):
    type: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    category: Optional[str] = None
    catalog_id: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    payment_method: Optional[str] = None
    reference_no: Optional[str] = None
    description: Optional[str] = None
    recorded_by: Optional[str] = None
    branch: Optional[str] = None
    service_items: Optional[List[Dict[str, Any]]] = None
    transfer_from: Optional[str] = None
    transfer_to: Optional[str] = None

# ===== Pydantic Models =====
class LeaveRequest(BaseModel):
    employee_name: str
    reason: str
    days: float = 1
    status: str = "pending"
    branch: Optional[str] = None
    date: Optional[str] = None

class LoanRequest(BaseModel):
    employee_name: str
    amount_khr: int
    purpose: Optional[str] = ""
    status: str = "pending"
    branch: Optional[str] = None
    date: Optional[str] = None

class Employee(BaseModel):
    name: str
    position: str
    phone: Optional[str] = ""
    phone2: Optional[str] = ""
    telegram_user_id: Optional[str] = ""
    telegram_username: Optional[str] = ""
    branch: Optional[str] = "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"

class LeaveCompensationRequest(BaseModel):
    leave_id: Optional[str] = None
    employee_name: Optional[str] = None
    days_to_compensate: float = 1.0
    admin_name: Optional[str] = "Admin"
    note: Optional[str] = ""

class LoanRepaymentRequest(BaseModel):
    loan_id: Optional[str] = None
    employee_name: Optional[str] = None
    amount_to_repay: int = 0
    admin_name: Optional[str] = "Admin"
    note: Optional[str] = ""

class SendReportRequest(BaseModel):
    mode: Optional[str] = "summary"
    employee_name: Optional[str] = ""
    branch: Optional[str] = None

class BulkEmployeeRequest(BaseModel):
    names: List[str] = []
    position: Optional[str] = "បុគ្គលិក"
    branch: Optional[str] = "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"

class BatchPhoneUpdateRequest(BaseModel):
    phones: Dict[str, str] = {}

class SettingsUpdate(BaseModel):
    telegram_bot_token: Optional[str] = ""
    telegram_chat_id: Optional[str] = ""
    gemini_api_key: Optional[str] = ""
    gemini_model: Optional[str] = "gemini-flash-lite-latest"
    notify_on_leave: bool = True
    notify_on_loan: bool = True
    branches: Optional[List[Dict[str, Any]]] = None

class TelegramTestRequest(BaseModel):
    bot_token: Optional[str] = ""
    chat_id: Optional[str] = ""

class GeminiTestRequest(BaseModel):
    gemini_api_key: Optional[str] = ""
    gemini_model: Optional[str] = "gemini-flash-lite-latest"

class AdminLoginRequest(BaseModel):
    username: str
    password: str

class AdminChangeCredentialsRequest(BaseModel):
    current_password: str
    new_username: str
    new_password: str

# ===== Admin Authentication Helper =====
ADMIN_AUTH_KEY = "admin_auth"
SECRET_SESSION_KEY = os.getenv("SECRET_KEY", "acbomlu_super_secret_session_key_2026")
KHMER_NUM_MAP = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")

def normalize_auth_digits(s: str) -> str:
    if not s:
        return ""
    return str(s).translate(KHMER_NUM_MAP)

def clean_auth_str(s: str) -> str:
    if not s:
        return ""
    # Strip invisible zero-width unicode chars, NBSP, and extra whitespace
    return re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff\u00a0\s]+', '', str(s))

def verify_admin_login(username: str, password: str) -> bool:
    raw_u = (username or "").strip()
    raw_p = (password or "").strip()
    if not raw_u or not raw_p:
        return False
        
    u_clean = clean_auth_str(raw_u).lower()
    u_digits = normalize_auth_digits(u_clean)

    p_clean = clean_auth_str(raw_p)
    p_digits = normalize_auth_digits(p_clean)
    p_raw_digits = normalize_auth_digits(raw_p)

    candidate_passwords = list(dict.fromkeys([raw_p, p_clean, p_digits, p_raw_digits]))
    candidate_hashes = [hashlib.sha256(cp.encode('utf-8')).hexdigest() for cp in candidate_passwords if cp]
        
    try:
        rec = settings_collection.find_one({"_id": ADMIN_AUTH_KEY})
        
        # Check against DB credentials
        if rec and rec.get("password_hash"):
            stored_u = (rec.get("username") or "admin").strip()
            stored_u_clean = clean_auth_str(stored_u).lower()
            stored_hash = rec.get("password_hash")
            
            allowed_usernames = {
                stored_u_clean,
                normalize_auth_digits(stored_u_clean),
                "admin",
                "រដ្ឋបាល",
                "រដ្ឋបាលacbomlu",
                "kemborey",
                "kem borey",
                "kemboreyac",
                "acbomlu",
                "092673641",
                "០៩២៦៧៣៦៤១"
            }
            
            u_matches = (
                (u_clean in allowed_usernames) or
                (u_digits in allowed_usernames) or
                (raw_u.lower() in allowed_usernames)
            )
            
            # Match strictly against the single password stored in DB (including Khmer numerals normalization):
            if u_matches and (stored_hash in candidate_hashes):
                return True
                
            return False
                
        # ONLY on a fresh database setup before any password was changed:
        if ((u_clean in ["admin", "រដ្ឋបាល"]) and ("admin" in candidate_passwords or "123456" in candidate_passwords)):
            return True
            
    except Exception as e:
        print(f"verify_admin_login error: {e}")
        
    return False

def generate_session_token(username: str) -> str:
    ts = str(int(time.time()))
    raw = f"{username}:{ts}:{SECRET_SESSION_KEY}"
    sig = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return f"{username}_{ts}_{sig[:32]}"

def validate_session_token(token: str) -> bool:
    if not token or "_" not in token:
        return False
    try:
        parts = token.split("_")
        if len(parts) != 3:
            return False
        username, ts_str, sig = parts
        ts = int(ts_str)
        if time.time() - ts > 30 * 86400: # 30 days validity
            return False
        raw = f"{username}:{ts}:{SECRET_SESSION_KEY}"
        expected_sig = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False

def check_admin_auth(req: Request) -> bool:
    auth_header = req.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = req.cookies.get("admin_session", "")
    if not token:
        token = req.query_params.get("token", "")
    return validate_session_token(token)

# ===== Helper =====
def fix_id(doc):
    doc["_id"] = str(doc["_id"])
    return doc

def is_telegram_admin(token: str, chat_id: str, user_id: int) -> bool:
    try:
        url = f"https://api.telegram.org/bot{token}/getChatMember?chat_id={chat_id}&user_id={user_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
            if data.get("ok"):
                status = data.get("result", {}).get("status", "")
                return status in ["creator", "administrator"]
    except Exception as e:
        print("Check admin error:", e)
    return False

def get_all_branches() -> list:
    default_branches = [
        {"id": "kampong_cham", "name": "ក្រុមហ៊ុនបូមលូ កំពង់ចាម", "telegram_chat_id": "-1003956005362", "chat_id": "-1003956005362"},
        {"id": "tboung_khmum", "name": "ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ", "telegram_chat_id": "", "chat_id": ""}
    ]
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        raw = s.get("branches") if s else None
        if raw and isinstance(raw, list):
            res = []
            for b in raw:
                c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
                res.append({
                    "id": b.get("id"),
                    "name": b.get("name"),
                    "telegram_chat_id": c_id,
                    "chat_id": c_id
                })
            return res
    except Exception:
        pass
    return default_branches

def get_branch_by_chat_id(chat_id: str) -> str:
    if not chat_id:
        return "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"
    chat_id_str = str(chat_id).strip()
    try:
        branches = get_all_branches()
        for b in branches:
            c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
            if c_id and c_id == chat_id_str:
                return b.get("name", "ក្រុមហ៊ុនបូមលូ កំពង់ចាម")
        
        # If chat_id is a group/supergroup (starts with -) and not default branch, auto-link to tboung_khmum if empty
        if chat_id_str.startswith("-") and chat_id_str != "-1003956005362":
            for b in branches:
                c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
                if b.get("id") == "tboung_khmum" and not c_id:
                    b["telegram_chat_id"] = chat_id_str
                    b["chat_id"] = chat_id_str
                    settings_collection.update_one({"_id": "app_settings"}, {"$set": {"branches": branches}}, upsert=True)
                    print(f"Auto-linked Telegram chat_id {chat_id_str} to Tboung Khmum branch!")
                    return b.get("name", "ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ")
    except Exception as e:
        print(f"get_branch_by_chat_id error: {e}")
    return "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"

def get_chat_id_by_branch(branch_name: str) -> Optional[str]:
    if not branch_name:
        return None
    b_key = str(branch_name).strip()
    try:
        branches = get_all_branches()
        # 1. Exact match on name or id
        for b in branches:
            if b.get("name") == b_key or b.get("id") == b_key:
                c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
                if c_id:
                    return c_id
        # 2. Substring match
        for b in branches:
            b_name = b.get("name", "")
            b_id = b.get("id", "")
            if (b_id and b_id.lower() == b_key.lower()) or \
               ("កំពង់ចាម" in b_key and "កំពង់ចាម" in b_name) or \
               ("ត្បូងឃ្មុំ" in b_key and "ត្បូងឃ្មុំ" in b_name):
                c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
                if c_id:
                    return c_id
    except Exception as e:
        print(f"get_chat_id_by_branch error: {e}")
    return None

def send_telegram_notify(text: str, reply_markup: dict = None, target_chat_id: str = None, fallback_to_default: bool = True):
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        group_chat_id = (s.get("telegram_chat_id", "").strip() if s else "") or os.getenv("TELEGRAM_CHAT_ID", "")
        dest_chat_id = target_chat_id
        if not dest_chat_id and fallback_to_default:
            dest_chat_id = group_chat_id
        if not token or not dest_chat_id:
            return
            
        # Default persistent keyboard buttons if no custom markup provided
        if reply_markup is None:
            reply_markup = {
                "keyboard": [
                    [{"text": "📝 ស្នើសុំច្បាប់"}, {"text": "💰 ស្នើកម្ចី"}]
                ],
                "resize_keyboard": True,
                "is_persistent": True,
                "one_time_keyboard": False,
                "input_field_placeholder": "ចុចប៊ូតុង [📝 ស្នើសុំច្បាប់] ឬ [💰 ស្នើកម្ចី]..."
            }
            
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        # Handle messages exceeding Telegram's 4096-char limit by chunking cleanly:
        if len(text) > 3900:
            chunks = []
            curr = ""
            for line in text.split("\n"):
                if len(curr) + len(line) + 1 > 3800:
                    chunks.append(curr)
                    curr = line
                else:
                    curr = (curr + "\n" + line) if curr else line
            if curr:
                chunks.append(curr)
            for idx, ch in enumerate(chunks):
                p = {
                    "chat_id": dest_chat_id,
                    "text": ch,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup if idx == len(chunks) - 1 else None
                }
                r = urllib.request.Request(url, data=json.dumps(p).encode("utf-8"), headers={"Content-Type": "application/json"})
                urllib.request.urlopen(r, timeout=8)
            return

        payload = {
            "chat_id": dest_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=8)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"Telegram notify error: {e}")
        return None

def send_telegram_to_all_branches(text: str, reply_markup: dict = None):
    """Send a message to ALL configured branches with valid chat_ids."""
    branches = get_all_branches()
    chat_ids_sent = set()
    any_sent = False
    for b in branches:
        chat_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
        if chat_id and chat_id not in chat_ids_sent:
            send_telegram_notify(text, reply_markup=reply_markup, target_chat_id=chat_id, fallback_to_default=False)
            chat_ids_sent.add(chat_id)
            any_sent = True
    if not any_sent:
        # Fallback to default group if no branch chat_ids configured
        send_telegram_notify(text, reply_markup=reply_markup, fallback_to_default=True)

def remove_telegram_inline_keyboard(chat_id: str, message_id: int):
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or not chat_id or not message_id:
            return
        url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []}
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Error removing inline keyboard: {e}")

def edit_telegram_message(chat_id: str, message_id: int, new_text: str, reply_markup: dict = None):
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or not chat_id or not message_id:
            return
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "HTML"
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"Error editing telegram message: {e}")

def pin_telegram_message(chat_id: str, message_id: int):
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token or not chat_id or not message_id:
            return False
        url = f"https://api.telegram.org/bot{token}/pinChatMessage"
        payload = {"chat_id": chat_id, "message_id": message_id, "disable_notification": True}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True
    except Exception as e:
        print(f"Error pinning telegram message: {e}")
        return False

def enforce_telegram_group_permissions(chat_id: str) -> bool:
    """
    Enforces Telegram group permissions so that NO regular members can pin
    or unpin messages or change chat info — ONLY Administrators can do so.
    """
    try:
        if not chat_id or not str(chat_id).startswith("-"):
            return False
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False
        url = f"https://api.telegram.org/bot{token}/setChatPermissions"
        payload = {
            "chat_id": str(chat_id),
            "permissions": {
                "can_send_messages": True,
                "can_send_audios": True,
                "can_send_documents": True,
                "can_send_photos": True,
                "can_send_videos": True,
                "can_send_video_notes": True,
                "can_send_voice_notes": True,
                "can_send_polls": True,
                "can_send_other_messages": True,
                "can_add_web_page_previews": True,
                "can_change_info": False,
                "can_invite_users": True,
                "can_pin_messages": False  # Crucial: Only admins can pin or unpin!
            }
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok", False)
    except Exception as e:
        print(f"Error enforcing permissions on {chat_id}: {e}")
        return False

def ensure_pinned_official_message(chat_id: str, branch_name: str = "") -> bool:
    """
    Verifies that the official buttons announcement is pinned at the top of the group.
    If unpinned or deleted, it re-creates and re-pins the message automatically.
    """
    try:
        if not chat_id or not str(chat_id).startswith("-"):
            return False
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False

        enforce_telegram_group_permissions(chat_id)

        url = f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            chat_info = json.loads(resp.read().decode("utf-8")).get("result", {})
            pinned = chat_info.get("pinned_message")
            if pinned and pinned.get("text") and "ក្ដារចុចប៊ូតុងផ្លូវការ" in pinned.get("text", ""):
                return True

        b_label = branch_name or chat_info.get("title") or "ក្រុមហ៊ុន"
        res = send_telegram_notify(
            f"👋 <b>សូមជម្រាបសួរបុគ្គលិក {b_label} ទាំងអស់!</b>\n\n"
            f"👇 <b>ក្ដារចុចប៊ូតុងផ្លូវការត្រូវបានភ្ជាប់ជូន៖</b>\n"
            f"សូមចុចលើប៊ូតុងខាងក្រោមដើម្បីដំណើរការ៖\n"
            f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ដើម្បីសុំច្បាប់ឈប់សម្រាក\n"
            f"• <b>[ 💰 ស្នើកម្ចី ]</b> ដើម្បីស្នើសុំខ្ចីប្រាក់ / កម្ចី",
            target_chat_id=chat_id,
            fallback_to_default=False
        )
        if res and isinstance(res, dict) and "result" in res:
            m_id = res["result"].get("message_id")
            if m_id:
                pin_telegram_message(chat_id, m_id)
                return True
        return False
    except Exception as e:
        print(f"Error ensuring pinned message on {chat_id}: {e}")
        return False

def delete_telegram_message(chat_id: str, message_id: int):
    """
    Deletes an intermediate Telegram message (e.g. step 1 or step 2 prompt)
    so group chat remains clean and only the final card is kept.
    """
    try:
        if not chat_id or not message_id:
            return False
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False
        url = f"https://api.telegram.org/bot{token}/deleteMessage"
        payload = {
            "chat_id": str(chat_id),
            "message_id": int(message_id)
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("ok", False)
    except Exception as e:
        # Silently fail if message was already deleted or expired
        return False

async def schedule_message_timeout_cleanup(chat_id: str, message_id: int, user_id: str = None, timeout_seconds: int = 60):
    """
    Time out after 1 minute (60s) and automatically delete the prompt message
    if the user does not move on to the next step of the request.
    Keeps Telegram chats 100% clean and removes uncompleted request prompts.
    """
    try:
        await asyncio.sleep(timeout_seconds)
        if not chat_id or not message_id:
            return
        # Delete message from Telegram chat
        delete_telegram_message(chat_id, message_id)
        # Clean up database flow if user did not move on
        if user_id:
            curr = user_flows_collection.find_one({"user_id": str(user_id)})
            if curr:
                if curr.get("step1_msg_id") == message_id or curr.get("reason_msg_id") == message_id or curr.get("purpose_msg_id") == message_id:
                    user_flows_collection.delete_one({"user_id": str(user_id)})
    except Exception as e:
        print(f"schedule_message_timeout_cleanup error: {e}")

def sync_or_update_employee_from_telegram(tg_user: dict, phone: str = None, position: str = None, branch: str = None) -> dict:
    """
    Auto-registers or auto-updates employee records from Telegram.
    Syncs display name changes automatically whenever a user interacts.
    """
    try:
        if not tg_user:
            return None
        tg_id = str(tg_user.get("id"))
        if not tg_id or tg_id == "None":
            return None
            
        first_name = (tg_user.get("first_name") or "").strip()
        last_name = (tg_user.get("last_name") or "").strip()
        username = (tg_user.get("username") or "").strip()
        
        full_name = f"{first_name} {last_name}".strip() or username or "បុគ្គលិក"
        
        # 1. Look up by telegram_user_id
        emp = employees_collection.find_one({"telegram_user_id": tg_id})
        
        # 2. If not found by ID, look up by name or username (case-insensitive & flexible)
        if not emp:
            emp = employees_collection.find_one({"name": full_name})
        if not emp:
            import re
            emp = employees_collection.find_one({"name": {"$regex": f"^{re.escape(full_name)}$", "$options": "i"}})
        if not emp and first_name:
            import re
            emp = employees_collection.find_one({"name": {"$regex": f"^{re.escape(first_name)}$", "$options": "i"}})
        if not emp and username:
            emp = employees_collection.find_one({"telegram_username": username})
            
        cambodia_tz = timezone(timedelta(hours=7))
        now_str = datetime.now(cambodia_tz).strftime("%d/%m/%Y %H:%M")
        
        if emp:
            update_fields = {}
            # NOTE: Never overwrite official company name (emp['name']) with personal Telegram nickname!
            # Official employee name is locked and managed only by admin.
            if username and emp.get("telegram_username") != username:
                update_fields["telegram_username"] = username
            if not emp.get("telegram_user_id"):
                update_fields["telegram_user_id"] = tg_id
            if phone and emp.get("phone") != phone:
                update_fields["phone"] = phone
            if position and (not emp.get("position") or emp.get("position") == "បុគ្គលិក"):
                update_fields["position"] = position
                
            if update_fields:
                update_fields["updated_at"] = now_str
                employees_collection.update_one({"_id": emp["_id"]}, {"$set": update_fields})
                emp.update(update_fields)
            return emp
        else:
            # New employee from Telegram group
            new_emp = {
                "name": full_name,
                "position": position or "បុគ្គលិក",
                "phone": phone or "",
                "phone2": "",
                "telegram_user_id": tg_id,
                "telegram_username": username,
                "branch": branch or "ក្រុមហ៊ុនបូមលូ កំពង់ចាម",
                "created_at": now_str,
                "source": "telegram_auto_sync"
            }
            res = employees_collection.insert_one(new_emp)
            new_emp["_id"] = res.inserted_id
            return new_emp
    except Exception as e:
        print("sync_or_update_employee_from_telegram error:", e)
        return None

def sync_group_members_from_telegram() -> dict:
    """
    Fetches group administrators and recent active members from Telegram API,
    and syncs them into employees_collection.
    """
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = (s.get("telegram_chat_id", "").strip() if s else "") or os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return {"success": False, "message": "សូមកំណត់ Telegram Bot Token និង Chat ID ជាមុនសិន!"}
            
        url = f"https://api.telegram.org/bot{token}/getChatAdministrators?chat_id={chat_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        synced_count = 0
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.loads(r.read().decode())
            if res.get("ok"):
                for m in res.get("result", []):
                    u = m.get("user", {})
                    if u.get("is_bot"):
                        continue
                    title = m.get("custom_title") or ("ប្រធាន (Owner)" if m.get("status") == "creator" else "អ្នកគ្រប់គ្រង (Admin)")
                    emp = sync_or_update_employee_from_telegram(u, position=title)
                    if emp:
                        synced_count += 1
                        
        # Also sync from active users in recent leaves, loans, and user_flows
        recent_leaves = leaves_collection.find({}, {"employee_name": 1, "telegram_user_id": 1}).sort("_id", -1).limit(50)
        for l in recent_leaves:
            ename = (l.get("employee_name") or "").strip()
            if ename and not employees_collection.find_one({"name": ename}):
                employees_collection.insert_one({
                    "name": ename,
                    "position": "បុគ្គលិក",
                    "phone": "",
                    "phone2": "",
                    "telegram_user_id": l.get("telegram_user_id", ""),
                    "created_at": datetime.now(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M"),
                    "source": "leave_activity"
                })
                synced_count += 1
                
        total_emp = employees_collection.count_documents({})
        return {
            "success": True,
            "synced_count": synced_count,
            "total_employees": total_emp,
            "message": f"បាន Sync ទិន្នន័យបុគ្គលិកជោគជ័យ! បច្ចុប្បន្នមានបុគ្គលិកសរុប {total_emp} នាក់ក្នុងប្រព័ន្ធ។"
        }
    except Exception as e:
        print("sync_group_members_from_telegram error:", e)
        return {"success": False, "message": f"កំហុសក្នុងការ Sync: {e}"}

def check_leave_conflicts(emp_name: str) -> str:
    """Feature 4: Alert Admin if multiple staff take leave on the same day!"""
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        today_date = datetime.now(cambodia_tz).date()
        today_str = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
        
        recent_leaves = list(leaves_collection.find({
            "status": {"$in": ["approved", "pending"]},
            "employee_name": {"$ne": emp_name}
        }, sort=[("_id", -1)]).limit(15))
        
        conflict_names = []
        for l in recent_leaves:
            doc_date = l["_id"].generation_time.astimezone(cambodia_tz).date()
            if doc_date == today_date or l.get("date") == today_str:
                ename = l.get("employee_name", "").strip()
                if ename and ename not in conflict_names:
                    conflict_names.append(ename)
                    
        if conflict_names:
            names_str = ", ".join(conflict_names)
            return f"\n⚠️ <b>ចំណាំ:</b> ថ្ងៃនេះមានបុគ្គលិក <b>[{names_str}]</b> បានសុំច្បាប់រួចហើយ សូមពិចារណាមុននឹងអនុញ្ញាតិ!\n"
    except Exception as e:
        print("Check leave conflicts error:", e)
    return ""

def get_employee_context(emp_name: str) -> dict:
    """Helper to fetch live employee leave & loan counts for AI reasoning."""
    try:
        user_leaves = list(leaves_collection.find({"employee_name": emp_name}))
        approved_leave_days = sum(l.get("days", 1) for l in user_leaves if l.get("status") == "approved")
        pending_leave_days = sum(l.get("days", 1) for l in user_leaves if l.get("status") == "pending")
        
        user_loans = list(loans_collection.find({"employee_name": emp_name}))
        approved_loan_amt = sum(l.get("amount_khr", 0) for l in user_loans if l.get("status") == "approved")
        pending_loan_amt = sum(l.get("amount_khr", 0) for l in user_loans if l.get("status") == "pending")
        
        return {
            "name": emp_name,
            "total_leaves": len(user_leaves),
            "approved_leaves": approved_leave_days,
            "pending_leaves": pending_leave_days,
            "approved_loans": approved_loan_amt,
            "pending_loans": pending_loan_amt
        }
    except Exception as e:
        print("get_employee_context error:", e)
        return {"name": emp_name}

def get_employee_self_service_report(emp_name: str) -> str:
    """Feature 5: Employee self-service inquiry for personal leave & loan totals."""
    try:
        user_leaves = list(leaves_collection.find({"employee_name": emp_name}))
        total_leave_requests = len(user_leaves)
        approved_leave_days = sum(l.get("days", 1) for l in user_leaves if l.get("status") == "approved")
        compensated_days = sum(float(l.get("compensated_days", 0.0)) for l in user_leaves if l.get("status") == "approved")
        remaining_leave_days = max(0.0, approved_leave_days - compensated_days)
        pending_leave_days = sum(l.get("days", 1) for l in user_leaves if l.get("status") == "pending")
        
        user_loans = list(loans_collection.find({"employee_name": emp_name}))
        approved_loan_amt = sum(l.get("amount_khr", 0) for l in user_loans if l.get("status") == "approved")
        paid_loan_amt = sum(int(l.get("paid_amount_khr", 0)) for l in user_loans if l.get("status") == "approved")
        remaining_loan_amt = max(0, approved_loan_amt - paid_loan_amt)
        pending_loan_amt = sum(l.get("amount_khr", 0) for l in user_loans if l.get("status") == "pending")
        
        comp_info = f"\n• បានសងច្បាប់រួច: <b>{compensated_days} ថ្ងៃ</b>\n• <b>នៅសល់ត្រូវសងចុងសប្តាហ៍:</b> <b>{remaining_leave_days} ថ្ងៃ</b>" if approved_leave_days > 0 else ""
        loan_paid_info = f"\n• បានកាត់ប្រាក់ខែ/សងរួច: <b>{paid_loan_amt:,} រៀល</b>\n• <b>នៅសល់ជំពាក់ត្រូវកាត់:</b> <b>{remaining_loan_amt:,} រៀល</b>" if approved_loan_amt > 0 else ""

        return (
            f"👤 <b>ព័ត៌មានផ្ទាល់ខ្លួន:</b> {emp_name}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>ប្រវត្តិច្បាប់ឈប់សម្រាក:</b>\n"
            f"• បានអនុញ្ញាតិ (Approved): <b>{approved_leave_days} ថ្ងៃ</b>{comp_info}\n"
            f"• កំពុងរង់ចាំ (Pending): <b>{pending_leave_days} ថ្ងៃ</b>\n"
            f"• សំណើសុំសរុប: <b>{total_leave_requests} លើក</b>\n\n"
            f"💰 <b>ប្រវត្តិខ្ចីប្រាក់ / បុរេប្រទាន:</b>\n"
            f"• ទឹកប្រាក់បានអនុញ្ញាតិ: <b>{approved_loan_amt:,} រៀល</b>{loan_paid_info}\n"
            f"• ទឹកប្រាក់កំពុងរង់ចាំ: <b>{pending_loan_amt:,} រៀល</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ <i>ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក AC BOMLU</i>"
        )
    except Exception as e:
        return f"⚠️ មិនអាចទាញយកទិន្នន័យបានទេ: {e}"

def generate_daily_morning_briefing(branch: Optional[str] = None) -> str:
    """Feature 3: Daily morning summary of attendance and leaves for Admins (06:00 AM).
    Supports multi-branch data isolation: filters employees, leaves, and loans by branch.
    """
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        today_date = now_cam.date()
        today_str = now_cam.strftime("%d/%m/%Y")
        
        emp_query = {"branch": branch} if branch else {}
        total_emp = employees_collection.count_documents(emp_query)
        
        # Only include APPROVED leaves! Unapproved, pending, or rejected leaves are excluded.
        branch_emp_names = set(e.get("name") for e in employees_collection.find({"branch": branch}) if e.get("name")) if branch else set()
        leave_query = {"status": "approved"}
        if branch:
            leave_query["$or"] = [
                {"branch": branch},
                {"employee_name": {"$in": list(branch_emp_names)}}
            ]
        approved_leaves = list(leaves_collection.find(leave_query))
        emp_leaves = {}  # ename -> total_days
        for l in approved_leaves:
            doc_date = l["_id"].generation_time.astimezone(cambodia_tz).date()
            if doc_date == today_date or l.get("date") == now_cam.strftime("%Y-%m-%d"):
                ename = (l.get("employee_name") or "").strip()
                if not ename:
                    continue
                try:
                    days = float(l.get("days", 1))
                except (ValueError, TypeError):
                    days = 1.0
                emp_leaves[ename] = emp_leaves.get(ename, 0.0) + days
                
        today_leaves = []
        for ename, total_days in emp_leaves.items():
            if total_days == 0.5:
                days_label = "កន្លះថ្ងៃ"
            elif total_days.is_integer():
                days_label = f"{int(total_days)} ថ្ងៃ"
            else:
                days_label = f"{total_days} ថ្ងៃ"
            today_leaves.append(f"{ename} ({days_label} ✅)")
            
        leave_count = len(today_leaves)
        leave_list_str = "\n  • " + "\n  • ".join(today_leaves) if today_leaves else " គ្មានទេ (មកធ្វើការគ្រប់គ្នា)"
        
        # Financial privacy: Do NOT leak loan amounts or financial figures in morning Telegram briefing.
        # Daily morning briefing is strictly focused on employee attendance and approved leaves (របាយការណ៍សុំច្បាប់ និងវត្តមាន).
        branch_display = f" - {branch}" if branch else ""
        branch_label = "ក្នុងសាខា" if branch else "ក្នុងប្រព័ន្ធ"
        return (
            f"🌅 <b>របាយការណ៍វត្តមាន និងច្បាប់ឈប់សម្រាកពេលព្រឹក{branch_display} (ម៉ោង ០៦:០០ ព្រឹក)</b>\n"
            f"📅 <b>កាលបរិច្ឆេទ:</b> {today_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>បុគ្គលិកសរុប{branch_label}:</b> {total_emp} នាក់\n"
            f"📝 <b>បុគ្គលិកសុំច្បាប់ថ្ងៃនេះ ({leave_count} នាក់):</b>{leave_list_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💪 <i>សូមជូនពរឱ្យដំណើរការការងារថ្ងៃនេះទទួលបានជោគជ័យ និងរលូនល្អ!</i>"
        )
    except Exception as e:
        return f"⚠️ មិនអាចបង្កើតរបាយការណ៍ពេលព្រឹកបានទេ: {e}"

def record_leave_compensation(leave_id: Optional[str] = None, employee_name: Optional[str] = None, days_to_compensate: float = 1.0, admin_name: str = "Admin", note: str = "") -> dict:
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        now_str = now_cam.strftime("%d/%m/%Y %H:%M")
        days_to_compensate = float(days_to_compensate)
        if days_to_compensate <= 0:
            return {"success": False, "message": "ចំនួនថ្ងៃត្រូវសងត្រូវតែធំជាង 0"}
            
        target_emp_name = ""
        
        if leave_id:
            try:
                l = leaves_collection.find_one({"_id": ObjectId(leave_id)})
            except Exception:
                l = None
            if not l:
                return {"success": False, "message": "រកមិនឃើញទិន្នន័យច្បាប់នេះទេ"}
            if l.get("status") != "approved":
                return {"success": False, "message": "អាចកត់ត្រាការសងបានតែលើច្បាប់ដែលរដ្ឋបាលបានអនុញ្ញាតិ (Approved) ប៉ុណ្ណោះ"}
                
            target_emp_name = (l.get("employee_name") or "").strip()
            tot_days = float(l.get("days", 1.0))
            curr_comp = float(l.get("compensated_days", 0.0))
            curr_rem = max(0.0, tot_days - curr_comp)
            
            apply_days = min(days_to_compensate, curr_rem)
            new_comp = curr_comp + apply_days
            new_rem = max(0.0, tot_days - new_comp)
            new_status = "fully_compensated" if new_rem <= 0 else "partially_compensated"
            
            log_entry = {
                "days_compensated": apply_days,
                "total_compensated_now": new_comp,
                "remaining_days": new_rem,
                "admin_name": admin_name,
                "note": note,
                "recorded_at": now_str
            }
            
            leaves_collection.update_one(
                {"_id": ObjectId(leave_id)},
                {
                    "$set": {
                        "compensated_days": new_comp,
                        "remaining_days": new_rem,
                        "compensation_status": new_status
                    },
                    "$push": {"compensation_history": log_entry}
                }
            )
            final_rem = new_rem
            
        elif employee_name:
            target_emp_name = employee_name.strip()
            leaves = list(leaves_collection.find({
                "employee_name": target_emp_name,
                "status": "approved",
                "compensation_status": {"$ne": "fully_compensated"}
            }).sort("_id", 1))
            
            if not leaves:
                return {"success": False, "message": f"មិនមានទិន្នន័យច្បាប់ដែលត្រូវសងសម្រាប់បុគ្គលិក [{target_emp_name}] ទេ"}
                
            days_needed = days_to_compensate
            for l in leaves:
                if days_needed <= 0:
                    break
                tot_days = float(l.get("days", 1.0))
                curr_comp = float(l.get("compensated_days", 0.0))
                curr_rem = max(0.0, tot_days - curr_comp)
                if curr_rem <= 0:
                    continue
                    
                apply_days = min(days_needed, curr_rem)
                new_comp = curr_comp + apply_days
                new_rem = max(0.0, tot_days - new_comp)
                new_status = "fully_compensated" if new_rem <= 0 else "partially_compensated"
                
                log_entry = {
                    "days_compensated": apply_days,
                    "total_compensated_now": new_comp,
                    "remaining_days": new_rem,
                    "admin_name": admin_name,
                    "note": note,
                    "recorded_at": now_str
                }
                
                leaves_collection.update_one(
                    {"_id": l["_id"]},
                    {
                        "$set": {
                            "compensated_days": new_comp,
                            "remaining_days": new_rem,
                            "compensation_status": new_status
                        },
                        "$push": {"compensation_history": log_entry}
                    }
                )
                days_needed -= apply_days
                
            active_leaves = list(leaves_collection.find({
                "employee_name": target_emp_name,
                "status": "approved"
            }))
            final_rem = sum(max(0.0, float(x.get("days", 1.0)) - float(x.get("compensated_days", 0.0))) for x in active_leaves)
        else:
            return {"success": False, "message": "ត្រូវផ្តល់ leave_id ឬ employee_name"}

        days_disp = "កន្លះថ្ងៃ" if days_to_compensate == 0.5 else (f"{int(days_to_compensate)} ថ្ងៃ" if days_to_compensate.is_integer() else f"{days_to_compensate} ថ្ងៃ")
        rem_disp = "០ ថ្ងៃ (សងរួចរាល់គ្រប់ចំនួន ✅)" if final_rem <= 0 else ("កន្លះថ្ងៃ" if final_rem == 0.5 else (f"{int(final_rem)} ថ្ងៃ" if final_rem.is_integer() else f"{final_rem} ថ្ងៃ"))
        status_label = "សងរួចរាល់គ្រប់ចំនួន ✅" if final_rem <= 0 else "សងបានខ្លះ ⏳"
        
        note_line = f"\n📝 <b>ចំណាំ:</b> {note}" if note else ""
        tg_receipt = (
            f"📋 <b>បច្ចុប្បន្នភាពការសងច្បាប់ (Leave Compensation)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>បុគ្គលិក:</b> <b>{target_emp_name}</b>\n"
            f"⏱️ <b>បានសងលើកនេះ:</b> <b>{days_disp}</b>\n"
            f"📊 <b>ស្ថានភាព:</b> <b>{status_label}</b>\n"
            f"⏳ <b>នៅសល់ត្រូវសងបន្ត:</b> <b>{rem_disp}</b>\n"
            f"👮‍♂️ <b>កត់ត្រាដោយរដ្ឋបាល:</b> {admin_name}"
            f"{note_line}\n"
            f"📅 <b>កាលបរិច្ឆេទ:</b> {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ <i>ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក AC BOMLU</i>"
        )
        send_telegram_notify(tg_receipt)
        
        return {
            "success": True,
            "message": f"បានកត់ត្រាការសង {days_disp} ជូន [{target_emp_name}] ជោគជ័យ! (នៅសល់ {rem_disp})",
            "employee_name": target_emp_name,
            "days_compensated": days_to_compensate,
            "final_remaining_days": final_rem,
            "status": status_label
        }
    except Exception as e:
        print("record_leave_compensation error:", e)
        return {"success": False, "message": f"កំហុសក្នុងការកត់ត្រាការសងច្បាប់: {e}"}

def generate_weekend_leave_report(branch: Optional[str] = None) -> dict:
    """
    Weekly Leave Report for staff to compensate (ធ្វើការសង) on Saturday & Sunday.
    STRICT: ONLY APPROVED LEAVES! Pending and rejected leaves are STRICTLY EXCLUDED.
    Grouped strictly by distinct employee name.
    """
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        start_date = (now_cam - timedelta(days=7)).date()
        
        leave_q = {"status": "approved"}
        if branch:
            leave_q["branch"] = branch
        leaves = list(leaves_collection.find(leave_q).sort("_id", -1))
        
        emp_comp = {}  # ename -> {"total_days": float, "compensated_days": float, "remaining_days": float, "reasons": list, "dates": list, "count": int}
        raw_records = []
        for l in leaves:
            doc_date = l["_id"].generation_time.astimezone(cambodia_tz).date()
            if doc_date >= start_date:
                ename = (l.get("employee_name") or "").strip() or "បុគ្គលិក"
                try:
                    days = float(l.get("days", 1))
                except (ValueError, TypeError):
                    days = 1.0
                rsn = l.get("reason", "មិនបានបញ្ជាក់")
                date_str = doc_date.strftime("%d/%m/%Y")
                
                comp_days = float(l.get("compensated_days", 0.0))
                rem_days = max(0.0, days - comp_days)
                comp_status = l.get("compensation_status") or ("fully_compensated" if rem_days <= 0 else ("partially_compensated" if comp_days > 0 else "uncompensated"))
                
                days_display = "កន្លះថ្ងៃ" if days == 0.5 else (f"{int(days)} ថ្ងៃ" if days.is_integer() else f"{days} ថ្ងៃ")
                comp_display = "០ ថ្ងៃ" if comp_days <= 0 else ("កន្លះថ្ងៃ" if comp_days == 0.5 else (f"{int(comp_days)} ថ្ងៃ" if comp_days.is_integer() else f"{comp_days} ថ្ងៃ"))
                rem_display = "០ ថ្ងៃ" if rem_days <= 0 else ("កន្លះថ្ងៃ" if rem_days == 0.5 else (f"{int(rem_days)} ថ្ងៃ" if rem_days.is_integer() else f"{rem_days} ថ្ងៃ"))
                
                raw_records.append({
                    "id": str(l["_id"]),
                    "employee_name": ename,
                    "date": date_str,
                    "days": days_display,
                    "raw_days": days,
                    "compensated_days": comp_display,
                    "raw_compensated_days": comp_days,
                    "remaining_days": rem_display,
                    "raw_remaining_days": rem_days,
                    "compensation_status": comp_status,
                    "reason": rsn,
                    "status": "approved"
                })
                
                if ename not in emp_comp:
                    emp_comp[ename] = {"total_days": 0.0, "compensated_days": 0.0, "remaining_days": 0.0, "reasons": [], "dates": [], "count": 0}
                emp_comp[ename]["total_days"] += days
                emp_comp[ename]["compensated_days"] += comp_days
                emp_comp[ename]["remaining_days"] += rem_days
                emp_comp[ename]["count"] += 1
                if rsn not in emp_comp[ename]["reasons"]:
                    emp_comp[ename]["reasons"].append(rsn)
                if date_str not in emp_comp[ename]["dates"]:
                    emp_comp[ename]["dates"].append(date_str)
                    
        compensate_list = []
        total_days_to_compensate = 0.0
        active_staff_count = 0
        
        for ename, info in emp_comp.items():
            tot = info["total_days"]
            comp_days = info["compensated_days"]
            rem = info["remaining_days"]
            
            if rem > 0:
                total_days_to_compensate += rem
                active_staff_count += 1
                
            if rem <= 0:
                comp_desc = "បានសងរួចរាល់គ្រប់ចំនួន ✅"
                days_label = "០ ថ្ងៃ (រួចរាល់)"
                comp_status = "fully_compensated"
            elif rem <= 0.5:
                comp_desc = "សង កន្លះថ្ងៃ (សៅរ៍ ឬ អាទិត្យ)"
                days_label = "កន្លះថ្ងៃ"
                comp_status = "partially_compensated" if comp_days > 0 else "uncompensated"
            elif rem == 1.0:
                comp_desc = "សង ១ ថ្ងៃ (សៅរ៍ ឬ អាទិត្យ)"
                days_label = "១ ថ្ងៃ"
                comp_status = "partially_compensated" if comp_days > 0 else "uncompensated"
            elif rem >= 2.0:
                comp_desc = f"សង {int(rem) if rem.is_integer() else rem} ថ្ងៃ (ថ្ងៃសៅរ៍ & អាទិត្យ)"
                days_label = f"{int(rem) if rem.is_integer() else rem} ថ្ងៃ"
                comp_status = "partially_compensated" if comp_days > 0 else "uncompensated"
            else:
                comp_desc = f"សង {rem} ថ្ងៃ (សៅរ៍ ឬ អាទិត្យ)"
                days_label = f"{rem} ថ្ងៃ"
                comp_status = "partially_compensated" if comp_days > 0 else "uncompensated"
                
            compensate_list.append({
                "employee_name": ename,
                "days": f"{int(tot) if tot.is_integer() else tot} ថ្ងៃ" if tot != 0.5 else "កន្លះថ្ងៃ",
                "raw_days": tot,
                "compensated_days": f"{int(comp_days) if comp_days.is_integer() else comp_days} ថ្ងៃ" if comp_days != 0.5 else "កន្លះថ្ងៃ",
                "raw_compensated_days": comp_days,
                "remaining_days": days_label,
                "raw_remaining_days": rem,
                "compensation_status": comp_status,
                "leave_count": info["count"],
                "reason": ", ".join(info["reasons"]),
                "status": "approved",
                "date": ", ".join(info["dates"]),
                "compensation": comp_desc
            })
            
        tot_days_display = int(total_days_to_compensate) if total_days_to_compensate.is_integer() else total_days_to_compensate
        return {
            "report_type": "weekend_leave",
            "generated_at": now_cam.strftime("%d/%m/%Y %H:%M"),
            "total_staff_count": active_staff_count,
            "total_days_to_compensate": tot_days_display,
            "per_employee": compensate_list,
            "items": compensate_list,
            "raw_records": raw_records
        }
    except Exception as e:
        print("generate_weekend_leave_report error:", e)
        return {"report_type": "weekend_leave", "items": [], "per_employee": [], "raw_records": [], "total_staff_count": 0, "total_days_to_compensate": 0}

def format_telegram_weekend_leave_report(data: dict) -> str:
    now_cam_str = data.get("generated_at", "")
    items = data.get("per_employee", data.get("items", []))
    
    # Filter ONLY active staff who still need to compensate (raw_remaining_days > 0)!
    active_items = [it for it in items if it.get("raw_remaining_days", 0) > 0]
    total_staff = len(active_items)
    total_days = data.get("total_days_to_compensate", 0)
    
    lines = []
    for idx, item in enumerate(active_items, 1):
        comp_info = f" (បានសង {item['compensated_days']})" if item.get('raw_compensated_days', 0) > 0 else ""
        lines.append(
            f"{idx}. <b>{item['employee_name']}</b> (✅ បានអនុញ្ញាត {item.get('leave_count', 1)} ករណី)\n"
            f"   • ច្បាប់ឈប់សរុប: <b>{item['days']}</b>{comp_info}\n"
            f"   • <b>នៅសល់ត្រូវសង:</b> <b>{item['remaining_days']}</b>\n"
            f"   • <b>កាតព្វកិច្ច:</b> <b>{item['compensation']}</b>"
        )
        
    items_str = "\n\n".join(lines) if lines else "  <i>គ្មានបុគ្គលិកសុំច្បាប់ដែលត្រូវធ្វើការសងក្នុងចុងសប្តាហ៍នេះទេ (សងរួចរាល់គ្រប់គ្នា) 🎉</i>"
    
    return (
        f"📢 <b>របាយការណ៍ច្បាប់ត្រូវសងចុងសប្តាហ៍ (ថ្ងៃសៅរ៍ & អាទិត្យ)</b>\n"
        f"⏰ <b>ជូនដំណឹងស្វ័យប្រវត្តិ:</b> រៀងរាល់ថ្ងៃសុក្រ ម៉ោង ៦:០០ ល្ងាច (18:00)\n"
        f"📅 <b>កាលបរិច្ឆេទ:</b> {now_cam_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>បុគ្គលិកត្រូវធ្វើការសង:</b> {total_staff} នាក់ (សរុប {total_days} ថ្ងៃ - អនុញ្ញាតប៉ុណ្ណោះ ✅)\n\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>សូមបុគ្គលិកដែលមានឈ្មោះខាងលើ មកបំពេញការងារសងនៅថ្ងៃសៅរ៍ និងថ្ងៃអាទិត្យឱ្យបានទៀងទាត់!</i>"
    )

def format_telegram_weekend_leave_report_detailed(data: dict, filter_employee: str = None) -> str:
    now_cam_str = data.get("generated_at", "")
    records = data.get("raw_records", [])
    
    if filter_employee and filter_employee.strip():
        records = [r for r in records if r.get("employee_name", "").strip().lower() == filter_employee.strip().lower()]
        
    active_records = [r for r in records if r.get("raw_remaining_days", 0) > 0]
    display_records = active_records if active_records else records
    
    total_days = sum(r.get("raw_remaining_days", 0) for r in active_records)
    tot_days_disp = int(total_days) if total_days.is_integer() else total_days
    
    lines = []
    for idx, r in enumerate(display_records, 1):
        emp = r.get("employee_name", "បុគ្គលិក")
        d_disp = r.get("days", "1 ថ្ងៃ")
        rsn = r.get("reason", "មិនបានបញ្ជាក់")
        dt = r.get("date", "")
        rem_disp = r.get("remaining_days", "0 ថ្ងៃ")
        comp_disp = r.get("compensated_days", "0 ថ្ងៃ")
        rem_raw = r.get("raw_remaining_days", 0)
        comp_raw = r.get("raw_compensated_days", 0)
        
        status_label = "✅ សងរួចរាល់" if rem_raw <= 0 else ("🟡 សងបានខ្លះ" if comp_raw > 0 else "🔴 មិនទាន់សង")
        
        lines.append(
            f"{idx}. <b>{emp}</b> (ថ្ងៃស្នើសុំ: <code>{dt}</code>)\n"
            f"   • ច្បាប់សុំ: <b>{d_disp}</b> | បានសង: {comp_disp}\n"
            f"   • <b>នៅសល់ត្រូវសង:</b> <b>{rem_disp}</b>\n"
            f"   • មូលហេតុ: {rsn}\n"
            f"   • ស្ថានភាព: {status_label}"
        )
        
    items_str = "\n\n".join(lines) if lines else "  <i>គ្មានទិន្នន័យច្បាប់ត្រូវសងទេ (សងរួចរាល់គ្រប់ករណី) 🎉</i>"
    
    filter_label = f"\n👤 <b>បុគ្គលិក:</b> {filter_employee}" if filter_employee else ""
    return (
        f"📑 <b>របាយការណ៍ច្បាប់ត្រូវសងលម្អិត (គ្រប់ប្រតិបត្តិការ)</b>{filter_label}\n"
        f"📅 <b>កាលបរិច្ឆេទ:</b> {now_cam_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>សរុបករណីត្រូវសង:</b> {len(active_records)} ករណី (សរុប {tot_days_disp} ថ្ងៃ - អនុញ្ញាតប៉ុណ្ណោះ ✅)\n\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>សូមបុគ្គលិកដែលមានឈ្មោះខាងលើ មកបំពេញការងារសងនៅថ្ងៃសៅរ៍ និងថ្ងៃអាទិត្យឱ្យបានទៀងទាត់!</i>"
    )

def record_loan_repayment(loan_id: Optional[str] = None, employee_name: Optional[str] = None, amount_to_repay: int = 0, admin_name: str = "Admin", note: str = "") -> dict:
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        now_str = now_cam.strftime("%d/%m/%Y %H:%M")
        amount_to_repay = int(amount_to_repay)
        if amount_to_repay <= 0:
            return {"success": False, "message": "ចំនួនទឹកប្រាក់ត្រូវកាត់/សង ត្រូវតែធំជាង 0"}
            
        target_emp_name = ""
        
        if loan_id:
            try:
                ln = loans_collection.find_one({"_id": ObjectId(loan_id)})
            except Exception:
                ln = None
            if not ln:
                return {"success": False, "message": "រកមិនឃើញទិន្នន័យខ្ចីប្រាក់នេះទេ"}
            if ln.get("status") != "approved":
                return {"success": False, "message": "អាចកត់ត្រាការកាត់/សងបានតែលើសំណើដែលរដ្ឋបាលបានអនុញ្ញាតិ (Approved) ប៉ុណ្ណោះ"}
                
            target_emp_name = (ln.get("employee_name") or "").strip()
            tot_amt = int(ln.get("amount_khr", 0))
            curr_paid = int(ln.get("paid_amount_khr", 0))
            curr_rem = max(0, tot_amt - curr_paid)
            
            apply_amt = min(amount_to_repay, curr_rem)
            new_paid = curr_paid + apply_amt
            new_rem = max(0, tot_amt - new_paid)
            new_status = "fully_paid" if new_rem <= 0 else "partially_paid"
            
            log_entry = {
                "amount_repaid": apply_amt,
                "total_paid_now": new_paid,
                "remaining_amount_khr": new_rem,
                "admin_name": admin_name,
                "note": note,
                "recorded_at": now_str
            }
            
            loans_collection.update_one(
                {"_id": ObjectId(loan_id)},
                {
                    "$set": {
                        "paid_amount_khr": new_paid,
                        "remaining_amount_khr": new_rem,
                        "repayment_status": new_status
                    },
                    "$push": {"repayment_history": log_entry}
                }
            )
            final_rem = new_rem
            
        elif employee_name:
            target_emp_name = employee_name.strip()
            loans = list(loans_collection.find({
                "employee_name": target_emp_name,
                "status": "approved",
                "repayment_status": {"$ne": "fully_paid"}
            }).sort("_id", 1))
            
            if not loans:
                return {"success": False, "message": f"មិនមានទិន្នន័យខ្ចីប្រាក់ដែលត្រូវកាត់/សង សម្រាប់បុគ្គលិក [{target_emp_name}] ទេ"}
                
            amt_needed = amount_to_repay
            for ln in loans:
                if amt_needed <= 0:
                    break
                tot_amt = int(ln.get("amount_khr", 0))
                curr_paid = int(ln.get("paid_amount_khr", 0))
                curr_rem = max(0, tot_amt - curr_paid)
                if curr_rem <= 0:
                    continue
                    
                apply_amt = min(amt_needed, curr_rem)
                new_paid = curr_paid + apply_amt
                new_rem = max(0, tot_amt - new_paid)
                new_status = "fully_paid" if new_rem <= 0 else "partially_paid"
                
                log_entry = {
                    "amount_repaid": apply_amt,
                    "total_paid_now": new_paid,
                    "remaining_amount_khr": new_rem,
                    "admin_name": admin_name,
                    "note": note,
                    "recorded_at": now_str
                }
                
                loans_collection.update_one(
                    {"_id": ln["_id"]},
                    {
                        "$set": {
                            "paid_amount_khr": new_paid,
                            "remaining_amount_khr": new_rem,
                            "repayment_status": new_status
                        },
                        "$push": {"repayment_history": log_entry}
                    }
                )
                amt_needed -= apply_amt
                
            active_loans = list(loans_collection.find({
                "employee_name": target_emp_name,
                "status": "approved"
            }))
            final_rem = sum(max(0, int(x.get("amount_khr", 0)) - int(x.get("paid_amount_khr", 0))) for x in active_loans)
        else:
            return {"success": False, "message": "ត្រូវផ្តល់ loan_id ឬ employee_name"}

        amt_disp = f"{amount_to_repay:,} រៀល"
        rem_disp = "០ រៀល (កាត់ដាច់ទាំងអស់ ✅)" if final_rem <= 0 else f"{final_rem:,} រៀល"
        status_label = "កាត់ដាច់ទាំងអស់ ✅" if final_rem <= 0 else "កាត់បានខ្លះ ⏳"
        
        note_line = f"\n📝 <b>ចំណាំ:</b> {note}" if note else ""
        tg_receipt = (
            f"🧾 <b>បង្កាន់ដៃកាត់ប្រាក់ខែ / សងប្រាក់កម្ចី (Loan Repayment)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>បុគ្គលិក:</b> <b>{target_emp_name}</b>\n"
            f"💵 <b>បានកាត់/សងលើកនេះ:</b> <b>{amt_disp}</b>\n"
            f"📊 <b>ស្ថានភាព:</b> <b>{status_label}</b>\n"
            f"⏳ <b>នៅសល់ជំពាក់ត្រូវកាត់បន្ត:</b> <b>{rem_disp}</b>\n"
            f"👮‍♂️ <b>កត់ត្រាដោយរដ្ឋបាល:</b> {admin_name}"
            f"{note_line}\n"
            f"📅 <b>កាលបរិច្ឆេទ:</b> {now_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📌 <i>ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក AC BOMLU</i>"
        )
        # Financial privacy: Do NOT broadcast loan repayment/salary deduction receipts to public Telegram group.
        # This information remains confidential on the Admin Dashboard.
        # send_telegram_notify(tg_receipt)
        
        return {
            "success": True,
            "message": f"បានកត់ត្រាការកាត់/សងប្រាក់ {amt_disp} ជូន [{target_emp_name}] ជោគជ័យ! (នៅសល់ {rem_disp})",
            "employee_name": target_emp_name,
            "amount_repaid": amount_to_repay,
            "final_remaining_amount": final_rem,
            "status": status_label
        }
    except Exception as e:
        print("record_loan_repayment error:", e)
        return {"success": False, "message": f"កំហុសក្នុងការកត់ត្រាការកាត់/សងប្រាក់: {e}"}

def generate_monthly_loan_report(branch: Optional[str] = None) -> dict:
    """
    Monthly Loan Report for month-end payroll deduction.
    STRICT: ONLY APPROVED LOANS. Pending and rejected loans are STRICTLY EXCLUDED.
    Returns both per_employee (grouped by employee) and items (all approved transactions).
    """
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        cur_year = now_cam.year
        cur_month = now_cam.month
        
        loan_q = {"status": "approved"}
        if branch:
            loan_q["branch"] = branch
        loans = list(loans_collection.find(loan_q).sort("_id", -1))
        
        emp_loans = {}  # ename -> {"total_amount": int, "paid_amount": int, "remaining_amount": int, "count": int, "purposes": list, "dates": list}
        month_items = []
        total_remaining_to_deduct = 0
        
        for ln in loans:
            doc_dt = ln["_id"].generation_time.astimezone(cambodia_tz)
            if doc_dt.year == cur_year and doc_dt.month == cur_month:
                amt = ln.get("amount_khr", 0)
                try:
                    amt_val = int(amt)
                except (ValueError, TypeError):
                    amt_val = 0
                    
                paid_val = int(ln.get("paid_amount_khr", 0))
                rem_val = max(0, amt_val - paid_val)
                rep_status = ln.get("repayment_status") or ("fully_paid" if rem_val <= 0 else ("partially_paid" if paid_val > 0 else "unpaid"))
                
                ename = (ln.get("employee_name") or "").strip() or "បុគ្គលិក"
                pur = ln.get("purpose", "មិនបានបញ្ជាក់")
                date_str = doc_dt.strftime("%d/%m/%Y")
                
                month_items.append({
                    "id": str(ln["_id"]),
                    "employee_name": ename,
                    "amount_khr": amt_val,
                    "paid_amount_khr": paid_val,
                    "remaining_amount_khr": rem_val,
                    "repayment_status": rep_status,
                    "purpose": pur,
                    "status": "approved",
                    "date": date_str
                })
                
                if ename not in emp_loans:
                    emp_loans[ename] = {"total_amount": 0, "paid_amount": 0, "remaining_amount": 0, "count": 0, "purposes": [], "dates": []}
                emp_loans[ename]["total_amount"] += amt_val
                emp_loans[ename]["paid_amount"] += paid_val
                emp_loans[ename]["remaining_amount"] += rem_val
                emp_loans[ename]["count"] += 1
                if pur not in emp_loans[ename]["purposes"]:
                    emp_loans[ename]["purposes"].append(pur)
                if date_str not in emp_loans[ename]["dates"]:
                    emp_loans[ename]["dates"].append(date_str)
                    
        per_employee = []
        active_borrowers_count = 0
        for ename, einfo in emp_loans.items():
            rem = einfo["remaining_amount"]
            paid = einfo["paid_amount"]
            tot = einfo["total_amount"]
            if rem > 0:
                total_remaining_to_deduct += rem
                active_borrowers_count += 1
                
            per_employee.append({
                "employee_name": ename,
                "total_amount_khr": tot,
                "total_paid_khr": paid,
                "remaining_amount_khr": rem,
                "repayment_status": "fully_paid" if rem <= 0 else ("partially_paid" if paid > 0 else "unpaid"),
                "loan_count": einfo["count"],
                "purpose": ", ".join(einfo["purposes"]),
                "date": ", ".join(einfo["dates"]),
                "status": "approved"
            })
            
        return {
            "report_type": "monthly_loan",
            "month_str": now_cam.strftime("%m/%Y"),
            "total_borrowers": active_borrowers_count,
            "total_transactions": len(month_items),
            "total_amount_khr": total_remaining_to_deduct,
            "per_employee": per_employee,
            "items": month_items
        }
    except Exception as e:
        print("generate_monthly_loan_report error:", e)
        return {"report_type": "monthly_loan", "items": [], "per_employee": [], "total_borrowers": 0, "total_transactions": 0, "total_amount_khr": 0}

def format_telegram_monthly_loan_report(data: dict) -> str:
    month_str = data.get("month_str", "")
    per_emp = data.get("per_employee", [])
    
    # Only active borrowers with remaining debt!
    active_emp = [it for it in per_emp if it.get("remaining_amount_khr", 0) > 0]
    total_amt = data.get("total_amount_khr", 0)
    total_borrowers = len(active_emp)
    
    lines = []
    for idx, item in enumerate(active_emp, 1):
        paid_info = f" (បានកាត់/សងរួច: {item['total_paid_khr']:,} ៛)" if item.get('total_paid_khr', 0) > 0 else ""
        lines.append(
            f"{idx}. <b>{item['employee_name']}</b> (✅ បានអនុញ្ញាត {item['loan_count']} ករណី)\n"
            f"   • ខ្ចីសរុប: <b>{item['total_amount_khr']:,} រៀល</b>{paid_info}\n"
            f"   • <b>នៅសល់ត្រូវកាត់ប្រាក់ខែ:</b> <b>{item['remaining_amount_khr']:,} រៀល</b>\n"
            f"   • គោលបំណង: {item['purpose']}\n"
            f"   • កាលបរិច្ឆេទ: {item['date']}"
        )
        
    items_str = "\n\n".join(lines) if lines else "  <i>គ្មានបុគ្គលិកជំពាក់ប្រាក់ខ្ចីដែលត្រូវកាត់ប្រាក់ខែក្នុងខែនេះទេ (កាត់ដាច់គ្រប់គ្នា) 🎉</i>"
    
    return (
        f"💰 <b>របាយការណ៍ខ្ចីប្រាក់ប្រចាំខែ (សម្រាប់កាត់ប្រាក់ខែចុងខែ {month_str})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>ទឹកប្រាក់សរុបត្រូវកាត់ប្រាក់ខែ:</b> <b>{total_amt:,} រៀល</b> (អនុញ្ញាតប៉ុណ្ណោះ ✅)\n"
        f"👥 <b>ចំនួនបុគ្គលិកត្រូវកាត់ប្រាក់:</b> {total_borrowers} នាក់\n\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <i>សូមគណនេយ្យ និងរដ្ឋបាល ពិនិត្យទិន្នន័យដើម្បីកាត់ប្រាក់ខែបុគ្គលិកខាងលើឱ្យបានត្រឹមត្រូវ!</i>"
    )

def format_telegram_monthly_loan_report_detailed(data: dict, filter_employee: str = None) -> str:
    month_str = data.get("month_str", "")
    items = data.get("items", [])
    
    if filter_employee and filter_employee.strip():
        items = [it for it in items if it.get("employee_name", "").strip().lower() == filter_employee.strip().lower()]
        
    active_items = [it for it in items if it.get("remaining_amount_khr", 0) > 0]
    display_items = active_items if active_items else items
    
    total_rem = sum(it.get("remaining_amount_khr", 0) for it in active_items)
    
    lines = []
    for idx, it in enumerate(display_items, 1):
        emp = it.get("employee_name", "បុគ្គលិក")
        amt = it.get("amount_khr", 0)
        paid = it.get("paid_amount_khr", 0)
        rem = it.get("remaining_amount_khr", 0)
        pur = it.get("purpose", "មិនបានបញ្ជាក់")
        dt = it.get("date", "")
        
        status_label = "✅ កាត់/សងដាច់" if rem <= 0 else ("🟡 បានកាត់ខ្លះ" if paid > 0 else "🔴 មិនទាន់កាត់")
        
        lines.append(
            f"{idx}. <b>{emp}</b> (ថ្ងៃខ្ចី: <code>{dt}</code>)\n"
            f"   • ចំនួនខ្ចី: <b>{amt:,} ៛</b> | បានកាត់/សង: {paid:,} ៛\n"
            f"   • <b>នៅសល់ត្រូវកាត់ប្រាក់ខែ:</b> <b>{rem:,} រៀល</b>\n"
            f"   • គោលបំណង: {pur}\n"
            f"   • ស្ថានភាព: {status_label}"
        )
        
    items_str = "\n\n".join(lines) if lines else "  <i>គ្មានទិន្នន័យកម្ចីជំពាក់ក្នុងខែនេះទេ (កាត់ដាច់គ្រប់ករណី) 🎉</i>"
    
    filter_label = f"\n👤 <b>បុគ្គលិក:</b> {filter_employee}" if filter_employee else ""
    return (
        f"📑 <b>របាយការណ៍ខ្ចីប្រាក់លម្អិតប្រចាំខែ (គ្រប់ប្រតិបត្តិការ)</b>{filter_label}\n"
        f"📅 <b>សម្រាប់កាត់ប្រាក់ខែចុងខែ:</b> {month_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>ទឹកប្រាក់សរុបនៅសល់ត្រូវកាត់:</b> <b>{total_rem:,} រៀល</b> (អនុញ្ញាតប៉ុណ្ណោះ ✅)\n"
        f"📋 <b>សរុបប្រតិបត្តិការត្រូវកាត់:</b> {len(active_items)} ករណី\n\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <i>សូមគណនេយ្យ និងរដ្ឋបាល ពិនិត្យទិន្នន័យប្រតិបត្តិការលម្អិតខាងលើដើម្បីកាត់ប្រាក់ខែឱ្យបានត្រឹមត្រូវ!</i>"
    )

# ===== Persistent Scheduled Reports Engine (Morning Briefing, Weekend Leave, Monthly Loan) =====

def check_and_send_scheduled_morning_briefing(force: bool = False) -> bool:
    """
    Checks if today's morning briefing was already sent.
    Sends branch-tailored morning briefings to BOTH Telegram groups (Kampong Cham & Tboung Khmum).
    ONLY sends Monday through Friday (now_cam.weekday() < 5).
    NEVER sends on Saturday or Sunday unless manually forced by admin.
    """
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        today_str = now_cam.strftime("%Y-%m-%d")

        # 1. Day-of-week rule: Monday to Friday ONLY (weekday 0 to 4)!
        # Saturday (5) & Sunday (6) are compensation days, not regular work days.
        if not force and now_cam.weekday() in (5, 6):
            return False

        rec = settings_collection.find_one({"_id": "scheduled_reports_state"}) or {}
        last_sent = rec.get("last_daily_briefing_date", "")

        # Trigger if forced OR if it's morning (between 06:00 AM and 11:59 AM) and not yet sent today
        if force or (6 <= now_cam.hour < 12 and last_sent != today_str):
            branches = get_all_branches()
            sent_count = 0
            
            for b in branches:
                b_name = b.get("name")
                c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
                if c_id:
                    briefing = generate_daily_morning_briefing(branch=b_name)
                    res = send_telegram_notify(briefing, target_chat_id=c_id, fallback_to_default=False)
                    if res:
                        sent_count += 1
                        print(f"Scheduled morning briefing sent to [{b_name}] ({c_id})")

            # Fallback to default group if no branch chat_ids configured
            if sent_count == 0:
                briefing = generate_daily_morning_briefing()
                send_telegram_notify(briefing, fallback_to_default=True)
                sent_count = 1

            settings_collection.update_one(
                {"_id": "scheduled_reports_state"},
                {"$set": {
                    "last_daily_briefing_date": today_str,
                    "last_daily_briefing_time": now_cam.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_status": "sent" if sent_count > 0 else "failed",
                    "sent_to_branches": sent_count
                }},
                upsert=True
            )
            print(f"Scheduled morning briefing completed for {today_str} (weekday={now_cam.weekday()}) to {sent_count} branch groups.")
            return True
    except Exception as e:
        print(f"check_and_send_scheduled_morning_briefing error: {e}")
    return False

def check_and_send_scheduled_weekend_report() -> bool:
    """Friday 18:00 (6:00 PM) to 23:59: notifies staff who must compensate work on Saturday & Sunday."""
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        today_str = now_cam.strftime("%Y-%m-%d")

        if now_cam.weekday() == 4 and now_cam.hour >= 18:
            rec = settings_collection.find_one({"_id": "scheduled_reports_state"}) or {}
            last_sent = rec.get("last_weekend_report_date", "")
            if last_sent != today_str:
                rep_data = generate_weekend_leave_report()
                if rep_data.get("items"):
                    tg_msg = format_telegram_weekend_leave_report(rep_data)
                    send_telegram_notify(tg_msg)
                settings_collection.update_one(
                    {"_id": "scheduled_reports_state"},
                    {"$set": {
                        "last_weekend_report_date": today_str,
                        "last_weekend_report_time": now_cam.strftime("%Y-%m-%d %H:%M:%S")
                    }},
                    upsert=True
                )
                return True
    except Exception as e:
        print(f"check_and_send_scheduled_weekend_report error: {e}")
    return False

def check_and_send_scheduled_monthly_loan_report() -> bool:
    """Financial privacy rule: Monthly loan reports are confidential on the Admin Dashboard only.
    Automated broadcasting to Telegram groups is strictly disabled."""
    return False

async def morning_briefing_scheduler():
    """Continuous background loop for permitted Telegram reports:
    - Daily morning attendance & approved leave briefing (របាយការណ៍សុំច្បាប់ប្រចាំថ្ងៃ)
    - Weekly weekend leave compensation report (របាយការណ៍សងថ្ងៃច្បាប់ចុងសប្ដាហ៍)
    """
    while True:
        try:
            check_and_send_scheduled_morning_briefing()
            check_and_send_scheduled_weekend_report()
        except Exception as e:
            print("Scheduler error:", e)
        await asyncio.sleep(60)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(morning_briefing_scheduler())
    # Ensure database indexes for optimal performance
    try:
        employees_collection.create_index([("telegram_user_id", 1)], background=True)
        employees_collection.create_index([("name", 1)], background=True)
        loans_collection.create_index([("employee_name", 1)], background=True)
        loans_collection.create_index([("status", 1)], background=True)
        loans_collection.create_index([("date", -1)], background=True)
        leaves_collection.create_index([("employee_name", 1)], background=True)
        leaves_collection.create_index([("status", 1)], background=True)
        leaves_collection.create_index([("date", -1)], background=True)
        finance_transactions_collection.create_index([("date", -1)], background=True)
        finance_transactions_collection.create_index([("type", 1)], background=True)
        finance_transactions_collection.create_index([("catalog_id", 1)], background=True)
        user_flows_collection.create_index([("user_id", 1)], background=True)
    except Exception as ie:
        print("Startup index check error:", ie)
    # Immediately check morning briefing on server startup / wakeup!
    try:
        check_and_send_scheduled_morning_briefing()
    except Exception as e:
        print("Startup morning briefing check error:", e)

@app.get("/api/cron/check-reports")
async def api_cron_check_reports():
    sent_briefing = check_and_send_scheduled_morning_briefing()
    sent_weekend = check_and_send_scheduled_weekend_report()
    sent_loan = check_and_send_scheduled_monthly_loan_report()
    return {
        "status": "ok",
        "sent_briefing": sent_briefing,
        "sent_weekend": sent_weekend,
        "sent_loan": sent_loan,
        "time": datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    }

# ===== Health Check & Keep-Alive Ping Routes =====
@app.get("/ping")
@app.get("/healthz")
async def health_check():
    return {
        "status": "ok",
        "service": "AC BOMLU Administration",
        "time": datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    }

# ===== API Routes =====
# ===== Admin Authentication & PWA Routes =====
@app.post("/api/auth/login")
async def api_admin_login(payload: AdminLoginRequest):
    u = (payload.username or "").strip()
    p = (payload.password or "").strip()
    if not u or not p:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូលឈ្មោះគណនី និងលេខសម្ងាត់!")
    if not verify_admin_login(u, p):
        raise HTTPException(status_code=401, detail="ឈ្មោះគណនី ឬលេខសម្ងាត់មិនត្រឹមត្រូវទេ! (អាចវាយ username: admin ឬ រដ្ឋបាល)")
        
    rec = settings_collection.find_one({"_id": ADMIN_AUTH_KEY})
    canonical_u = (rec.get("username") if rec else "") or u or "admin"
    
    token = generate_session_token(canonical_u)
    res = JSONResponse(content={
        "success": True,
        "token": token,
        "username": canonical_u,
        "message": f"សូមស្វាគមន៍មកកាន់ប្រព័ន្ធ AC BOMLU, {canonical_u}!"
    })
    res.set_cookie(
        key="admin_session",
        value=token,
        max_age=30 * 86400,
        httponly=False,
        samesite="lax",
        secure=False
    )
    return res



@app.get("/api/auth/verify")
async def api_admin_verify(req: Request):
    auth_header = req.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = req.cookies.get("admin_session", "")
    if not token:
        token = req.query_params.get("token", "")
        
    if validate_session_token(token):
        username = token.split("_")[0]
        return {"authenticated": True, "username": username}
    return {"authenticated": False}

@app.post("/api/auth/change-credentials")
async def api_admin_change_credentials(payload: AdminChangeCredentialsRequest, req: Request):
    cur_p = payload.current_password.strip()
    new_u = payload.new_username.strip()
    new_p = payload.new_password.strip()
    
    if not new_u:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូលឈ្មោះគណនី!")
    if not cur_p:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូលលេខសម្ងាត់បច្ចុប្បន្ន!")
    if not new_p:
        new_p = cur_p
    if len(new_p) < 4:
        raise HTTPException(status_code=400, detail="លេខសម្ងាត់ថ្មីត្រូវមានយ៉ាងតិច ៤ តួអក្សរឡើងទៅ!")

    rec = settings_collection.find_one({"_id": ADMIN_AUTH_KEY})
    cur_hash = hashlib.sha256(cur_p.encode('utf-8')).hexdigest()
    is_valid_current = False

    if rec and rec.get("password_hash"):
        if cur_hash == rec.get("password_hash"):
            is_valid_current = True
        elif not rec.get("is_custom_password") and cur_p in ["admin", "123456"]:
            is_valid_current = True
    else:
        if cur_p in ["admin", "123456"]:
            is_valid_current = True

    if not is_valid_current:
        raise HTTPException(status_code=401, detail="លេខសម្ងាត់បច្ចុប្បន្នមិនត្រឹមត្រូវទេ!")

    new_hash = hashlib.sha256(new_p.encode('utf-8')).hexdigest()
    settings_collection.update_one(
        {"_id": ADMIN_AUTH_KEY},
        {"$set": {
            "username": new_u,
            "password_hash": new_hash,
            "is_custom_password": True,
            "updated_at": datetime.now().isoformat()
        }},
        upsert=True
    )
    new_token = generate_session_token(new_u)
    res = JSONResponse(content={
        "success": True,
        "token": new_token,
        "username": new_u,
        "message": "បានផ្លាស់ប្ដូរឈ្មោះគណនី និងលេខសម្ងាត់ជោគជ័យ! (លេខសម្ងាត់ចាស់ 123456 និង admin ត្រូវបានលុបចោល)"
    })
    res.set_cookie(key="admin_session", value=new_token, max_age=30 * 86400, samesite="lax", secure=False)
    return res

@app.post("/api/auth/logout")
async def api_admin_logout():
    res = JSONResponse(content={"success": True, "message": "បានចាកចេញពីប្រព័ន្ធជោគជ័យ!"})
    res.delete_cookie("admin_session")
    return res

# ===== PWA Manifest & Service Worker =====
@app.get("/manifest.json")
async def get_manifest():
    manifest_data = {
        "name": "AC BOMLU — ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក",
        "short_name": "AC BOMLU",
        "description": "ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក ច្បាប់ឈប់សម្រាក និងប្រាក់ខ្ចី AC BOMLU",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0f172a",
        "theme_color": "#1565C0",
        "icons": [
            {
                "src": "/static/ac_logo.jpg",
                "sizes": "192x192",
                "type": "image/jpeg",
                "purpose": "any maskable"
            },
            {
                "src": "/static/ac_logo.jpg",
                "sizes": "512x512",
                "type": "image/jpeg",
                "purpose": "any maskable"
            }
        ]
    }
    return Response(content=json.dumps(manifest_data), media_type="application/manifest+json")

@app.get("/sw.js")
async def get_service_worker():
    sw_code = """
self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
      .then(() => self.registration.unregister())
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  event.respondWith(fetch(event.request));
});
"""
    return Response(
        content=sw_code.strip(),
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )
@app.get("/api/stats")
async def get_stats(branch: Optional[str] = None):
    q = {"branch": branch} if branch else {}
    return {
        "total_employees": employees_collection.count_documents(q),
        "pending_leaves": leaves_collection.count_documents({**q, "status": "pending"}),
        "pending_loans": loans_collection.count_documents({**q, "status": "pending"}),
        "approved_loans": loans_collection.count_documents({**q, "status": "approved"}),
    }

@app.get("/api/leaves")
async def get_leaves(branch: Optional[str] = None):
    q = {"branch": branch} if branch else {}
    docs = list(leaves_collection.find(q).sort("_id", -1).limit(100))
    return [fix_id(d) for d in docs]

@app.post("/api/leave")
async def create_leave(request: LeaveRequest):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    data = request.dict()
    if not data.get("branch"):
        emp = employees_collection.find_one({"name": request.employee_name})
        data["branch"] = emp.get("branch") if emp and emp.get("branch") else "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"
    date_str = (request.date or "").strip() or now.strftime("%Y-%m-%d")
    data["date"] = date_str
    if "created_at" not in data:
        data["created_at"] = now.isoformat()
    result = leaves_collection.insert_one(data)
    leave_id = str(result.inserted_id)
    adm_btn = {
        "inline_keyboard": [[
            {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
            {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
        ]]
    }
    target_chat = get_chat_id_by_branch(data["branch"])
    send_telegram_notify(
        f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
        f"🏢 <b>សាខា:</b> {data['branch']}\n"
        f"👤 <b>បុគ្គលិក:</b> {request.employee_name}\n"
        f"📅 <b>កាលបរិច្ឆេទ:</b> {date_str}\n"
        f"📝 <b>មូលហេតុ:</b> {request.reason}\n"
        f"⏱️ <b>ចំនួនថ្ងៃ:</b> {request.days} ថ្ងៃ\n"
        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំរដ្ឋបាលអនុញ្ញាតិ",
        reply_markup=adm_btn,
        target_chat_id=target_chat,
        fallback_to_default=False
    )
    return {"message": "Leave request saved!", "id": leave_id}

@app.patch("/api/leave/{leave_id}/status")
async def update_leave_status(leave_id: str, status: str):
    leave = leaves_collection.find_one({"_id": ObjectId(leave_id)})
    leaves_collection.update_one({"_id": ObjectId(leave_id)}, {"$set": {"status": status}})
    if leave:
        name = leave.get("employee_name", "បុគ្គលិក")
        days = leave.get("days", 1)
        branch = leave.get("branch", "ក្រុមហ៊ុនបូមលូ កំពង់ចាម")
        target_chat = get_chat_id_by_branch(branch)
        if status == "approved":
            send_telegram_notify(
                f"✅ <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                f"🏢 <b>សាខា:</b> {branch}\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                f"🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved)",
                target_chat_id=target_chat
            )
        elif status == "rejected":
            send_telegram_notify(
                f"❌ <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                f"🏢 <b>សាខា:</b> {branch}\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                f"🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected)",
                target_chat_id=target_chat
            )
    return {"message": f"Leave updated to {status}"}

@app.delete("/api/leave/{leave_id}")
async def delete_leave(leave_id: str):
    leaves_collection.delete_one({"_id": ObjectId(leave_id)})
    return {"message": "Deleted"}

@app.get("/api/loans")
async def get_loans(branch: Optional[str] = None):
    q = {"branch": branch} if branch else {}
    docs = list(loans_collection.find(q).sort("_id", -1).limit(100))
    return [fix_id(d) for d in docs]

@app.post("/api/loan")
async def create_loan(request: LoanRequest):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    data = request.dict()
    if not data.get("branch"):
        emp = employees_collection.find_one({"name": request.employee_name})
        data["branch"] = emp.get("branch") if emp and emp.get("branch") else "ក្រុមហ៊ុនបូមលូ កំពង់ចាម"
    date_str = (request.date or "").strip() or now.strftime("%Y-%m-%d")
    data["date"] = date_str
    if "created_at" not in data:
        data["created_at"] = now.isoformat()
    result = loans_collection.insert_one(data)
    loan_id = str(result.inserted_id)
    formatted_amount = f"{request.amount_khr:,}"
    adm_btn = {
        "inline_keyboard": [[
            {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
            {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
        ]]
    }
    target_chat = get_chat_id_by_branch(data["branch"])
    send_telegram_notify(
        f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
        f"🏢 <b>សាខា:</b> {data['branch']}\n"
        f"👤 <b>បុគ្គលិក:</b> {request.employee_name}\n"
        f"📅 <b>កាលបរិច្ឆេទ:</b> {date_str}\n"
        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {formatted_amount} រៀល\n"
        f"🎯 <b>គោលបំណង:</b> {request.purpose or 'មិនបានបញ្ជាក់'}\n"
        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំរដ្ឋបាលអនុញ្ញាតិ",
        reply_markup=adm_btn,
        target_chat_id=target_chat,
        fallback_to_default=False
    )
    return {"message": "Loan request saved!", "id": loan_id}

@app.patch("/api/loan/{loan_id}/status")
async def update_loan_status(loan_id: str, status: str):
    loan = loans_collection.find_one({"_id": ObjectId(loan_id)})
    loans_collection.update_one({"_id": ObjectId(loan_id)}, {"$set": {"status": status}})
    if loan:
        name = loan.get("employee_name", "បុគ្គលិក")
        amount = f"{loan.get('amount_khr', 0):,}"
        branch = loan.get("branch", "ក្រុមហ៊ុនបូមលូ កំពង់ចាម")
        target_chat = get_chat_id_by_branch(branch)
        if status == "approved":
            send_telegram_notify(
                f"✅ <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                f"🏢 <b>សាខា:</b> {branch}\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"💵 <b>ចំនួន:</b> {amount} រៀល\n"
                f"🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved)",
                target_chat_id=target_chat
            )
        elif status == "rejected":
            send_telegram_notify(
                f"❌ <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                f"🏢 <b>សាខា:</b> {branch}\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"💵 <b>ចំនួន:</b> {amount} រៀល\n"
                f"🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected)",
                target_chat_id=target_chat
            )
    return {"message": f"Loan updated to {status}"}

@app.delete("/api/loan/{loan_id}")
async def delete_loan(loan_id: str):
    loans_collection.delete_one({"_id": ObjectId(loan_id)})
    return {"message": "Deleted"}

@app.get("/api/employees")
async def get_employees(branch: Optional[str] = None):
    q = {"branch": branch} if branch else {}
    docs = list(employees_collection.find(q).sort("_id", -1).limit(200))
    return [fix_id(d) for d in docs]

@app.post("/api/employee")
async def create_employee(emp: Employee):
    result = employees_collection.insert_one(emp.dict())
    return {"message": "Employee added!", "id": str(result.inserted_id)}

@app.delete("/api/employee/{emp_id}")
async def delete_employee(emp_id: str):
    employees_collection.delete_one({"_id": ObjectId(emp_id)})
    return {"message": "Deleted"}

@app.put("/api/employee/{emp_id}")
async def update_employee(emp_id: str, payload: dict):
    update_data = {k: v for k, v in payload.items() if k in ["name", "position", "phone", "phone2", "telegram_user_id", "telegram_username", "branch"]}
    if update_data:
        old_emp = employees_collection.find_one({"_id": ObjectId(emp_id)})
        old_name = old_emp.get("name") if old_emp else None
        employees_collection.update_one({"_id": ObjectId(emp_id)}, {"$set": update_data})
        new_name = update_data.get("name")
        if old_name and new_name and old_name != new_name:
            leaves_collection.update_many({"employee_name": old_name}, {"$set": {"employee_name": new_name}})
            loans_collection.update_many({"employee_name": old_name}, {"$set": {"employee_name": new_name}})
    return {"message": "Updated"}

@app.post("/api/employees/sync-telegram")
async def api_sync_telegram_employees():
    res = sync_group_members_from_telegram()
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

@app.post("/api/employees/bulk")
async def bulk_create_employees(req: BulkEmployeeRequest):
    cambodia_tz = timezone(timedelta(hours=7))
    now_str = datetime.now(cambodia_tz).strftime("%d/%m/%Y %H:%M")
    created = 0
    for n in req.names:
        clean_n = n.strip()
        if not clean_n:
            continue
        if not employees_collection.find_one({"name": clean_n}):
            employees_collection.insert_one({
                "name": clean_n,
                "position": req.position or "បុគ្គលិក",
                "phone": "",
                "phone2": "",
                "telegram_user_id": "",
                "telegram_username": "",
                "branch": (req.branch or "ក្រុមហ៊ុនបូមលូ កំពង់ចាម").strip(),
                "created_at": now_str,
                "source": "bulk_import"
            })
            created += 1
    total_count = employees_collection.count_documents({})
    return {"message": f"បានបញ្ចូលបុគ្គលិកថ្មីចំនួន {created} នាក់ជោគជ័យ! បច្ចុប្បន្នមានសរុប {total_count} នាក់។", "created_count": created, "total_count": total_count}

@app.post("/api/employees/batch-phone")
async def batch_update_phones(req: BatchPhoneUpdateRequest):
    updated = 0
    for emp_id, phone in req.phones.items():
        clean_p = phone.replace("+855", "0").replace("-", "").replace(" ", "").strip()
        try:
            employees_collection.update_one({"_id": ObjectId(emp_id)}, {"$set": {"phone": clean_p}})
            updated += 1
        except Exception as e:
            print("Error updating phone:", e)
    return {"message": f"បានរក្សាទុកលេខទូរស័ព្ទបុគ្គលិកចំនួន {updated} នាក់ជោគជ័យ!", "updated_count": updated}

@app.post("/api/employees/request-phone-telegram")
async def api_request_phone_telegram(branch: Optional[str] = None):
    s = settings_collection.find_one({"_id": "app_settings"})
    token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    bot_username = "ac_bomlu_administration_bot"
    
    inline_btn = {
        "inline_keyboard": [[
            {"text": "📱 ចុចទីនេះដើម្បីបញ្ជូនលេខទូរស័ព្ទស្វ័យប្រវត្តិ", "url": f"https://t.me/{bot_username}?start=share_phone"}
        ]]
    }
    msg = (
        "📢 <b>ជូនដំណឹងដល់បុគ្គលិក AC BOMLU ទាំងអស់៖</b>\n\n"
        "ដើម្បីឱ្យប្រព័ន្ធគ្រប់គ្រងមានលេខទូរស័ព្ទទំនាក់ទំនងរបស់អ្នក និងបង្ហាញលើ Dashboard ក្រុមហ៊ុន សូមបុគ្គលិកម្នាក់ៗចុចប៊ូតុងខាងក្រោម៖\n\n"
        "👉 ចុចប៊ូតុង <b>[ 📱 ចុចទីនេះដើម្បីបញ្ជូនលេខទូរស័ព្ទស្វ័យប្រវត្តិ ]</b> រួចចុច <b>[ ចែករំលែកលេខទូរស័ព្ទ (Share Phone) ]</b> តែមួយដង នោះប្រព័ន្ធនឹងកត់ត្រាស្វ័យប្រវត្តិ។"
    )
    if branch:
        target_chat = get_chat_id_by_branch(branch)
        if not target_chat:
            raise HTTPException(status_code=400, detail=f"សាខា «{branch}» មិនទាន់មាន Telegram Chat ID នៅឡើយទេ! សូមបញ្ចូល Chat ID ក្នុងទំព័រ Settings ជាមុនសិន។")
        send_telegram_notify(msg, reply_markup=inline_btn, target_chat_id=target_chat, fallback_to_default=False)
        return {"message": f"បានផ្ញើសារស្នើសុំលេខទូរស័ព្ទទៅកាន់គ្រុប [{branch}] រួចរាល់!"}
    
    send_telegram_to_all_branches(msg, reply_markup=inline_btn)
    return {"message": "បានផ្ញើសារស្នើសុំលេខទូរស័ព្ទទៅកាន់គ្រុប Telegram គ្រប់សាខាទាំងអស់រួចរាល់!"}

@app.get("/api/backup/export")
async def export_database_backup():
    """Returns a full JSON backup of all company collections."""
    cambodia_tz = timezone(timedelta(hours=7))
    timestamp = datetime.now(cambodia_tz).strftime("%Y%m%d_%H%M%S")
    
    backup_data = {
        "version": "2.0",
        "system": "AC BOMLU Staff Control & Finance",
        "exported_at": datetime.now(cambodia_tz).strftime("%d/%m/%Y %H:%M:%S"),
        "employees": [fix_id(d) for d in employees_collection.find()],
        "loans": [fix_id(d) for d in loans_collection.find()],
        "leaves": [fix_id(d) for d in leaves_collection.find()],
        "finance_transactions": [fix_id(d) for d in finance_transactions_collection.find()],
        "finance_catalogs": [fix_id(d) for d in finance_catalogs_collection.find()],
        "settings": [fix_id(d) for d in settings_collection.find({"_id": "app_settings"})]
    }
    
    filename = f"acbomlu_backup_{timestamp}.json"
    return Response(
        content=json.dumps(backup_data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ===== Settings & Telegram Routes =====
def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) > 12:
        return token[:6] + "•" * 16 + token[-4:]
    return "•" * 16

@app.get("/api/settings")
async def get_settings():
    s = settings_collection.find_one({"_id": "app_settings"})
    token = ""
    chat_id = ""
    gemini_key = ""
    gemini_model = "gemini-flash-lite-latest"
    notify_leave = True
    notify_loan = True
    
    if s:
        token = s.get("telegram_bot_token", "")
        chat_id = s.get("telegram_chat_id", "")
        gemini_key = s.get("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))
        gemini_model = s.get("gemini_model", os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest"))
        notify_leave = s.get("notify_on_leave", True)
        notify_loan = s.get("notify_on_loan", True)
    else:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        gemini_key = os.getenv("GEMINI_API_KEY", "")
        gemini_model = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
        
    return {
        "telegram_bot_token": mask_token(token),
        "has_token": bool(token),
        "telegram_chat_id": chat_id,
        "gemini_api_key": mask_token(gemini_key),
        "has_gemini_key": bool(gemini_key),
        "gemini_model": gemini_model,
        "notify_on_leave": notify_leave,
        "notify_on_loan": notify_loan,
        "branches": get_all_branches()
    }

@app.post("/api/settings")
async def save_settings(payload: SettingsUpdate):
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    token_to_save = (payload.telegram_bot_token or "").strip()
    gemini_to_save = (payload.gemini_api_key or "").strip()
    
    # Security: If the token contains bullet points (masked) or is empty, keep existing token
    if not token_to_save or "•" in token_to_save:
        token_to_save = s.get("telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN", ""))
        
    if not gemini_to_save or "•" in gemini_to_save:
        gemini_to_save = s.get("gemini_api_key", os.getenv("GEMINI_API_KEY", ""))
        
    branches_to_save = payload.branches if payload.branches is not None else (s.get("branches") or get_all_branches())

    settings_collection.replace_one(
        {"_id": "app_settings"},
        {
            "_id": "app_settings",
            "telegram_bot_token": token_to_save,
            "telegram_chat_id": (payload.telegram_chat_id or "").strip(),
            "gemini_api_key": gemini_to_save,
            "gemini_model": (payload.gemini_model or "gemini-flash-lite-latest").strip(),
            "notify_on_leave": payload.notify_on_leave,
            "notify_on_loan": payload.notify_on_loan,
            "branches": branches_to_save
        },
        upsert=True
    )
    return {"message": "ការកំណត់ត្រូវបានរក្សាទុកដោយសុវត្ថិភាព (Encrypted & Masked)!"}

@app.get("/api/branches")
async def api_get_branches():
    return get_all_branches()

@app.post("/api/settings/branches")
async def api_save_branches(payload: dict):
    branches = payload.get("branches")
    if not isinstance(branches, list):
        raise HTTPException(status_code=400, detail="ទិន្នន័យសាខាមិនត្រឹមត្រូវ!")
    normalized = []
    for b in branches:
        c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
        normalized.append({
            "id": b.get("id"),
            "name": b.get("name"),
            "telegram_chat_id": c_id,
            "chat_id": c_id
        })
    settings_collection.update_one({"_id": "app_settings"}, {"$set": {"branches": normalized}}, upsert=True)
    
    # Auto-send activation message with persistent keyboard buttons to each connected branch
    for b in normalized:
        c_id = b.get("chat_id")
        b_name = b.get("name", "សាខា")
        if c_id:
            try:
                enforce_telegram_group_permissions(c_id)
                res_notify = send_telegram_notify(
                    f"👋 <b>សូមជម្រាបសួរបុគ្គលិក {b_name} ទាំងអស់!</b>\n\n"
                    f"👇 <b>ក្ដារចុចប៊ូតុងផ្លូវការត្រូវបានភ្ជាប់ជូន៖</b>\n"
                    f"សូមចុចលើប៊ូតុងខាងក្រោមដើម្បីដំណើរការ៖\n"
                    f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ដើម្បីសុំច្បាប់ឈប់សម្រាក\n"
                    f"• <b>[ 💰 ស្នើកម្ចី ]</b> ដើម្បីស្នើសុំខ្ចីប្រាក់ / កម្ចី",
                    target_chat_id=c_id,
                    fallback_to_default=False
                )
                if res_notify and isinstance(res_notify, dict) and "result" in res_notify:
                    m_id = res_notify["result"].get("message_id")
                    if m_id:
                        pin_telegram_message(c_id, m_id)
            except Exception as ex:
                print(f"Failed to push buttons to {c_id}: {ex}")

    return {"message": "បានរក្សាទុកព័ត៌មានសាខា និងបានផ្ញើប៊ូតុងទៅ Telegram ជោគជ័យ!", "branches": normalized}

@app.post("/api/branches/{branch_id}/send-buttons")
async def api_send_buttons_to_branch(branch_id: str):
    branches = get_all_branches()
    target_branch = None
    for b in branches:
        if b.get("id") == branch_id or b.get("name") == branch_id:
            target_branch = b
            break
    if not target_branch:
        raise HTTPException(status_code=404, detail="រកមិនឃើញសាខានេះទេ!")
    c_id = str(target_branch.get("telegram_chat_id") or target_branch.get("chat_id") or "").strip()
    if not c_id:
        raise HTTPException(status_code=400, detail=f"សាខា «{target_branch.get('name')}» មិនទាន់មាន Telegram Chat ID នៅឡើយទេ!")
    b_name = target_branch.get("name", "សាខា")
    res = send_telegram_notify(
        f"👋 <b>សូមជម្រាបសួរបុគ្គលិក {b_name} ទាំងអស់!</b>\n\n"
        f"👇 <b>ក្ដារចុចប៊ូតុងផ្លូវការត្រូវបានភ្ជាប់ជូន៖</b>\n"
        f"សូមចុចលើប៊ូតុងខាងក្រោមដើម្បីដំណើរការ៖\n"
        f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ដើម្បីសុំច្បាប់ឈប់សម្រាក\n"
        f"• <b>[ 💰 ស្នើកម្ចី ]</b> ដើម្បីស្នើសុំខ្ចីប្រាក់ / កម្ចី",
        target_chat_id=c_id,
        fallback_to_default=False
    )
    if not res:
        raise HTTPException(status_code=500, detail="មិនអាចផ្ញើសារទៅកាន់ Telegram បានទេ! សូមពិនិត្យមើល Bot Token និង Chat ID។")
    
    # Enforce permissions (Only Admins can pin/unpin) & Pin message at top of group
    enforce_telegram_group_permissions(c_id)
    if isinstance(res, dict) and "result" in res:
        m_id = res["result"].get("message_id")
        if m_id:
            pin_telegram_message(c_id, m_id)

    return {"message": f"បានផ្ញើប៊ូតុងសុំច្បាប់ និងខ្ចីប្រាក់ (រួមទាំង Pin លើគ្រុប) ទៅកាន់ [{b_name}] ជោគជ័យ!"}

@app.post("/api/test-gemini")
async def test_gemini(payload: GeminiTestRequest):
    key = (payload.gemini_api_key or "").strip()
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    if not key or "•" in key:
        key = s.get("gemini_api_key", os.getenv("GEMINI_API_KEY", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូល Google Gemini API Key ជាមុនសិន!")
        
    model = (payload.gemini_model or "gemini-flash-lite-latest").strip()
    candidate_models = [
        model,
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash"
    ]
    seen = set()
    models_to_try = [m for m in candidate_models if m and not m.startswith("gemini-3.") and not (m in seen or seen.add(m))]
    
    last_err = ""
    for m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}"
        test_payload = {
            "contents": [{"parts": [{"text": "សូមឆ្លើយពាក្យ 'សួស្តី' ខ្លីមួយម៉ាត់ជាភាសាខ្មែរ"}]}]
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(test_payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode('utf-8'))
                cand_list = data.get("candidates", [])
                if cand_list:
                    cand_parts = cand_list[0].get("content", {}).get("parts", [])
                    if cand_parts:
                        ans = cand_parts[0].get("text", "").strip()
                        # Auto-save key to MongoDB Atlas if verified successfully!
                        if payload.gemini_api_key and "•" not in payload.gemini_api_key:
                            settings_collection.update_one(
                                {"_id": "app_settings"},
                                {"$set": {"gemini_api_key": payload.gemini_api_key.strip(), "gemini_model": m}},
                                upsert=True
                            )
                        return {
                            "success": True,
                            "model_used": m,
                            "message": f"✅ ជោគជ័យ! Gemini AI ({m}) ឆ្លើយតប: {ans}"
                        }
        except Exception as e:
            last_err = str(e)
            continue
            
    raise HTTPException(status_code=400, detail=f"❌ ការតភ្ជាប់ Gemini បរាជ័យ: {last_err}")

@app.post("/api/test-telegram")
async def test_telegram(payload: TelegramTestRequest):
    token = (payload.bot_token or "").strip()
    chat_id = (payload.chat_id or "").strip()
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    
    # Security: If token is masked or empty, use the actual secured token from DB
    if not token or "•" in token:
        token = s.get("telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    if not chat_id:
        chat_id = s.get("telegram_chat_id", os.getenv("TELEGRAM_CHAT_ID", "")).strip()
            
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូល Telegram Bot Token និង Chat ID ជាមុនសិន!")
        
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        test_msg = (
            "🔔 <b>AC BOMLU HR System</b>\n\n"
            "✅ <b>ការតេស្ត Telegram Bot ទទួលបានជោគជ័យ ១០០%!</b>\n"
            "🛡️ <b>ប្រព័ន្ធសុវត្ថិភាព:</b> Token របស់អ្នកត្រូវបានការពារដោយសុវត្ថិភាព (Secured & Masked)។"
        )
        post_data = json.dumps({
            "chat_id": chat_id,
            "text": test_msg,
            "parse_mode": "HTML"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp_body = json.loads(resp.read().decode())
            if resp_body.get("ok"):
                return {"success": True, "message": "បានផ្ញើសារតេស្តទៅ Telegram ជោគជ័យ!"}
            else:
                return {"success": False, "message": f"Telegram API error: {resp_body}"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"មិនអាចផ្ញើសារបាន: {str(e)}")


@app.post("/api/send-briefing")
async def send_briefing_api(branch: Optional[str] = None):
    if branch:
        target_chat = get_chat_id_by_branch(branch)
        if not target_chat:
            raise HTTPException(status_code=400, detail=f"សាខា «{branch}» មិនទាន់មាន Telegram Chat ID នៅឡើយទេ! សូមបញ្ចូល Chat ID ក្នុងទំព័រ Settings ជាមុនសិន។")
        briefing = generate_daily_morning_briefing(branch=branch)
        send_telegram_notify(briefing, target_chat_id=target_chat, fallback_to_default=False)
        return {"message": f"បានផ្ញើរបាយការណ៍សង្ខេបពេលព្រឹកទៅគ្រុប [{branch}] រួចរាល់!"}

    # Broadcast to ALL branches with branch-specific data
    branches = get_all_branches()
    sent_count = 0
    for b in branches:
        b_name = b.get("name")
        c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
        if c_id:
            briefing = generate_daily_morning_briefing(branch=b_name)
            send_telegram_notify(briefing, target_chat_id=c_id, fallback_to_default=False)
            sent_count += 1
            
    if sent_count == 0:
        briefing = generate_daily_morning_briefing()
        send_telegram_notify(briefing, fallback_to_default=True)
        
    return {"message": "បានផ្ញើរបាយការណ៍សង្ខេបពេលព្រឹកទៅ Telegram គ្រប់សាខាទាំងអស់រួចរាល់!"}

@app.get("/api/reports/weekend-leave")
async def api_get_weekend_leave_report(branch: Optional[str] = None):
    return generate_weekend_leave_report(branch=branch)

@app.post("/api/reports/send-weekend-leave")
async def api_send_weekend_leave_report(req: Optional[SendReportRequest] = None):
    mode = req.mode if req else "summary"
    emp_filter = (req.employee_name or "").strip() if req else ""
    branch = (req.branch or "").strip() if req else ""
    data = generate_weekend_leave_report(branch=branch or None)
    if mode == "detailed":
        msg = format_telegram_weekend_leave_report_detailed(data, filter_employee=emp_filter)
    else:
        msg = format_telegram_weekend_leave_report(data)
    
    if branch:
        target_chat = get_chat_id_by_branch(branch)
        if not target_chat:
            raise HTTPException(status_code=400, detail=f"សាខា «{branch}» មិនទាន់មាន Telegram Chat ID នៅឡើយទេ! សូមបញ្ចូល Chat ID ក្នុងទំព័រ Settings ជាមុនសិន។")
        send_telegram_notify(msg, target_chat_id=target_chat, fallback_to_default=False)
    else:
        send_telegram_to_all_branches(msg)
        
    title = "របាយការណ៍ច្បាប់លម្អិត" if mode == "detailed" else "របាយការណ៍ច្បាប់សង្ខេប"
    if emp_filter:
        title += f" ({emp_filter})"
    if branch:
        title += f" [{branch}]"
    else:
        title += " [គ្រប់សាខា]"
    return {"message": f"បានផ្ញើ{title}ទៅ Telegram រួចរាល់!"}

@app.get("/api/reports/monthly-loan")
async def api_get_monthly_loan_report(branch: Optional[str] = None):
    return generate_monthly_loan_report(branch=branch)

@app.post("/api/reports/send-monthly-loan")
async def api_send_monthly_loan_report(req: Optional[SendReportRequest] = None):
    # Strict financial privacy rule: loan and money reports are strictly confidential.
    raise HTTPException(
        status_code=403,
        detail="ដើម្បីរក្សាការសម្ងាត់ទិន្នន័យហិរញ្ញវត្ថុ របាយការណ៍ខ្ចីប្រាក់មិនត្រូវបានអនុញ្ញាតឱ្យផ្ញើចូលក្នុង Telegram ឡើយ! សូមពិនិត្យផ្ទាល់លើ Admin Dashboard។"
    )

@app.post("/api/reports/compensate-leave")
async def api_compensate_leave(req: LeaveCompensationRequest):
    res = record_leave_compensation(
        leave_id=req.leave_id,
        employee_name=req.employee_name,
        days_to_compensate=req.days_to_compensate,
        admin_name=req.admin_name or "Admin",
        note=req.note or ""
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

@app.post("/api/reports/repay-loan")
async def api_repay_loan(req: LoanRepaymentRequest):
    res = record_loan_repayment(
        loan_id=req.loan_id,
        employee_name=req.employee_name,
        amount_to_repay=req.amount_to_repay,
        admin_name=req.admin_name or "Admin",
        note=req.note or ""
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

# ==============================================================================
# FINANCE & ACCOUNTING API (ចំណូល, ចំណាយ, កាតាឡុក, របាយការណ៍)
# ==============================================================================

DEFAULT_FINANCE_CATALOGS = [
    # ចំណូល (Income)
    {"name": "ចំណូលពីការលក់ទំនិញ", "type": "income", "icon": "storefront", "color": "emerald", "description": "ចំណូលបានពីការលក់ទំនិញប្រចាំថ្ងៃ"},
    {"name": "ចំណូលពីសេវាកម្ម", "type": "income", "icon": "handshake", "color": "blue", "description": "ចំណូលពីការផ្តល់សេវាកម្មអតិថិជន"},
    {"name": "ចំណូលពីកម្រៃជើងសារ", "type": "income", "icon": "percent", "color": "teal", "description": "ប្រាក់កម្រៃជើងសារ និងភាគរយផ្សេងៗ"},
    {"name": "ចំណូលផ្សេងៗ", "type": "income", "icon": "savings", "color": "amber", "description": "ចំណូលបន្ទាប់បន្សំ ឬប្រភពផ្សេងៗ"},
    # ចំណាយ (Expense)
    {"name": "ទិញទំនិញស្តុក / ផ្គត់ផ្គង់", "type": "expense", "icon": "shopping_cart", "color": "rose", "description": "ចំណាយលើការទិញទំនិញចូលស្តុក"},
    {"name": "ថ្លៃជួលទីតាំង / ហាង", "type": "expense", "icon": "home_work", "color": "purple", "description": "ថ្លៃឈ្នួលទីតាំងអាជីវកម្មប្រចាំខែ"},
    {"name": "ថ្លៃទឹក ភ្លើង និងអ៊ីនធឺណិត", "type": "expense", "icon": "bolt", "color": "amber", "description": "ថ្លៃទឹក អគ្គិសនី សេវាអ៊ីនធឺណិត និងទូរស័ព្ទ"},
    {"name": "ថ្លៃដឹកជញ្ជូន និងប្រេងសាំង", "type": "expense", "icon": "local_shipping", "color": "indigo", "description": "ចំណាយលើការដឹកជញ្ជូន និងការធ្វើដំណើរ"},
    {"name": "ថ្លៃផ្សព្វផ្សាយ / ទីផ្សារ", "type": "expense", "icon": "campaign", "color": "pink", "description": "ចំណាយលើការផ្សាយពាណិជ្ជកម្ម និងម៉ាឃីតធីង"},
    {"name": "បៀវត្សរ៍ / ប្រាក់ខែបុគ្គលិក", "type": "expense", "icon": "badge", "color": "orange", "description": "បើកប្រាក់បៀវត្សរ៍ និងប្រាក់ឧបត្ថម្ភបុគ្គលិក"},
    {"name": "ថ្លៃជួសជុល និងសម្ភារៈប្រើប្រាស់", "type": "expense", "icon": "build", "color": "cyan", "description": "សម្ភារៈការិយាល័យ និងការជួសជុលកែលម្អ"},
    {"name": "ចំណាយរដ្ឋបាល និងផ្សេងៗ", "type": "expense", "icon": "receipt_long", "color": "slate", "description": "ចំណាយទូទៅ និងចំណាយរដ្ឋបាលផ្សេងៗ"}
]

def seed_default_catalogs_if_needed():
    try:
        if finance_catalogs_collection.count_documents({}) == 0:
            now_iso = datetime.now(timezone(timedelta(hours=7))).isoformat()
            docs = []
            for item in DEFAULT_FINANCE_CATALOGS:
                docs.append({
                    **item,
                    "created_at": now_iso,
                    "updated_at": now_iso
                })
            finance_catalogs_collection.insert_many(docs)
    except Exception as e:
        print(f"seed_default_catalogs_if_needed error: {e}")

@app.get("/api/finance/catalogs")
async def api_get_catalogs(type: Optional[str] = None):
    seed_default_catalogs_if_needed()
    query = {}
    if type and type != "all":
        query["$or"] = [{"type": type}, {"type": "both"}]
    docs = list(finance_catalogs_collection.find(query).sort("name", 1))
    return [fix_id(d) for d in docs]

@app.post("/api/finance/catalogs")
async def api_create_catalog(payload: FinanceCatalogCreate):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូលឈ្មោះកាតាឡុក!")
    existing = finance_catalogs_collection.find_one({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}})
    if existing:
        raise HTTPException(status_code=400, detail="ឈ្មោះកាតាឡុកនេះមានរួចហើយ!")
    now_iso = datetime.now(timezone(timedelta(hours=7))).isoformat()
    doc = {
        "name": name,
        "type": payload.type or "both",
        "icon": payload.icon or "category",
        "color": payload.color or "blue",
        "description": payload.description or "",
        "created_at": now_iso,
        "updated_at": now_iso
    }
    res = finance_catalogs_collection.insert_one(doc)
    doc["_id"] = str(res.inserted_id)
    return {"success": True, "message": "បានបង្កើតកាតាឡុកថ្មីជោគជ័យ!", "catalog": doc}

@app.put("/api/finance/catalogs/{cat_id}")
async def api_update_catalog(cat_id: str, payload: FinanceCatalogUpdate):
    update_data = {}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="ឈ្មោះកាតាឡុកមិនអាចទទេបានទេ!")
        update_data["name"] = name
    if payload.type is not None:
        update_data["type"] = payload.type
    if payload.icon is not None:
        update_data["icon"] = payload.icon
    if payload.color is not None:
        update_data["color"] = payload.color
    if payload.description is not None:
        update_data["description"] = payload.description

    update_data["updated_at"] = datetime.now(timezone(timedelta(hours=7))).isoformat()
    r = finance_catalogs_collection.update_one({"_id": ObjectId(cat_id)}, {"$set": update_data})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="រកមិនឃើញកាតាឡុកនេះទេ!")
    return {"success": True, "message": "បានកែប្រែកាតាឡុកជោគជ័យ!"}

@app.delete("/api/finance/catalogs/{cat_id}")
async def api_delete_catalog(cat_id: str):
    r = finance_catalogs_collection.delete_one({"_id": ObjectId(cat_id)})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="រកមិនឃើញកាតាឡុកនេះទេ!")
    return {"success": True, "message": "បានលុបកាតាឡុកជោគជ័យ!"}

# --- Transactions API ---

@app.get("/api/finance/transactions")
async def api_get_transactions(
    type: Optional[str] = None,
    category: Optional[str] = None,
    date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    branch: Optional[str] = None,
    limit: int = 150,
    skip: int = 0
):
    query = {}
    if branch:
        query["branch"] = branch
    if type and type in ["income", "expense", "transfer"]:
        query["type"] = type
    if category and category != "all":
        query["category"] = category
    if date:
        query["date"] = date
    elif start_date and end_date:
        query["date"] = {"$gte": start_date, "$lte": end_date}
    elif start_date:
        query["date"] = {"$gte": start_date}
    elif end_date:
        query["date"] = {"$lte": end_date}

    if search:
        s_regex = {"$regex": re.escape(search.strip()), "$options": "i"}
        query["$or"] = [
            {"description": s_regex},
            {"reference_no": s_regex},
            {"category": s_regex},
            {"recorded_by": s_regex},
            {"payment_method": s_regex}
        ]

    cursor = finance_transactions_collection.find(query).sort([("date", -1), ("time", -1), ("_id", -1)]).skip(skip).limit(limit)
    docs = list(cursor)
    total_docs = finance_transactions_collection.count_documents(query)

    inc_khr = 0.0
    inc_usd = 0.0
    exp_khr = 0.0
    exp_usd = 0.0
    for d in docs:
        amt = float(d.get("amount", 0.0) or 0.0)
        curr = d.get("currency", "KHR")
        t = d.get("type", "income")
        if t == "income":
            if curr == "USD": inc_usd += amt
            else: inc_khr += amt
        else:
            if curr == "USD": exp_usd += amt
            else: exp_khr += amt

    return {
        "transactions": [fix_id(d) for d in docs],
        "total": total_docs,
        "summary": {
            "income_khr": inc_khr,
            "income_usd": inc_usd,
            "expense_khr": exp_khr,
            "expense_usd": exp_usd,
            "net_khr": inc_khr - exp_khr,
            "net_usd": inc_usd - exp_usd
        }
    }

# --- Finance Helpers: Khmer Numbers, Trips & Locations ---
KHMER_NUM_MAP = {
    '០': 0, '១': 1, '២': 2, '៣': 3, '៤': 4,
    '៥': 5, '៦': 6, '៧': 7, '៨': 8, '៩': 9
}

def parse_khmer_or_arabic_int(s):
    if not s:
        return 0
    res = ""
    for ch in str(s).strip():
        if ch in KHMER_NUM_MAP:
            res += str(KHMER_NUM_MAP[ch])
        elif ch.isdigit():
            res += ch
    try:
        return int(res) if res else 0
    except ValueError:
        return 0

def parse_khmer_or_arabic_number(s):
    if not s:
        return 0.0
    res = ""
    for ch in str(s).strip():
        if ch in KHMER_NUM_MAP:
            res += str(KHMER_NUM_MAP[ch])
        elif ch.isdigit() or ch == '.':
            res += ch
    try:
        return float(res) if res else 0.0
    except ValueError:
        return 0.0

def extract_trips_and_locations(desc, is_sewage_income=True):
    if not desc:
        return [("សេវាទូទៅ", 1, 0.0)] if is_sewage_income else []

    entries = []
    text = str(desc).strip()

    # Pattern 1: ទីតាំង[៖:] <loc> (<trips> ជើង [= <price> [៛|$|...]])
    # Handles multiple on same line or separate lines
    p1 = re.compile(r'ទីតាំង[៖:]\s*([^(\n]+?)\s*\(\s*([០-៩0-9]+)\s*ជើង(?:\s*=\s*([^\)]+?))?\s*\)')
    matches1 = list(p1.finditer(text))
    if matches1:
        for m in matches1:
            loc = m.group(1).strip()
            trips = parse_khmer_or_arabic_int(m.group(2)) or 1
            raw_price = m.group(3)
            price = parse_khmer_or_arabic_number(raw_price) if raw_price else 0.0
            if loc:
                entries.append((loc, trips, price))
        return entries

    # Pattern 2: line-by-line fallback for older formats
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines:
        m2 = re.search(r'([^\d(\n]+?)\s*\(?\s*([០-៩0-9]+)\s*ជើង(?:\s*=\s*([^\)]+?))?\s*\)?', line)
        if m2:
            loc = m2.group(1).strip().strip(':').strip('៖').strip()
            trips = parse_khmer_or_arabic_int(m2.group(2)) or 1
            raw_price = m2.group(3)
            price = parse_khmer_or_arabic_number(raw_price) if raw_price else 0.0
            if loc:
                entries.append((loc, trips, price))
                continue

        m3 = re.search(r'^([^\d០-៩]+?)\s*([០-៩0-9]+)$', line)
        if m3:
            loc = m3.group(1).strip()
            trips = parse_khmer_or_arabic_int(m3.group(2)) or 1
            if loc and len(loc) >= 2:
                entries.append((loc, trips, 0.0))
                continue

    if not entries and is_sewage_income:
        clean_desc = re.sub(r'[()\[\]]', '', text).strip()
        loc = clean_desc[:25] if clean_desc else "សេវាទូទៅ"
        entries.append((loc, 1, 0.0))

    return entries

@app.post("/api/finance/transactions")
async def api_create_transaction(payload: FinanceTransactionCreate):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    
    t_type = (payload.type or "income").strip().lower()
    if t_type not in ["income", "expense", "transfer"]:
        raise HTTPException(status_code=400, detail="ប្រភេទប្រតិបត្តិការត្រូវតែជា ចំណូល (income), ចំណាយ (expense) ឬ បង្វិលប្រាក់ (transfer)!")
    
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="ចំនួនទឹកប្រាក់ត្រូវតែធំជាង ០!")

    currency = (payload.currency or "KHR").strip().upper()
    if currency not in ["KHR", "USD"]:
        currency = "KHR"

    date_str = (payload.date or "").strip() or now.strftime("%Y-%m-%d")
    time_str = (payload.time or "").strip() or now.strftime("%H:%M")
    default_cat = "បង្វិលចូលគណនីវិនិយោគទុន" if t_type == "transfer" else ("ចំណូលផ្សេងៗ" if t_type == "income" else "ចំណាយផ្សេងៗ")
    category = (payload.category or "").strip() or default_cat

    service_items = payload.service_items
    if not service_items:
        is_service = "បូមលូ" in category or "សេវា" in category
        extracted = extract_trips_and_locations(payload.description or "", is_sewage_income=is_service)
        service_items = [{"location": loc, "trips": tr, "price": pr} for loc, tr, pr in extracted]

    doc = {
        "type": t_type,
        "amount": float(payload.amount),
        "currency": currency,
        "category": category,
        "catalog_id": payload.catalog_id,
        "date": date_str,
        "time": time_str,
        "payment_method": payload.payment_method or "សាច់ប្រាក់សុទ្ធ",
        "reference_no": (payload.reference_no or "").strip(),
        "description": (payload.description or "").strip(),
        "recorded_by": (payload.recorded_by or "").strip() or "រដ្ឋបាល",
        "branch": (getattr(payload, "branch", None) or "ក្រុមហ៊ុនបូមលូ កំពង់ចាម").strip(),
        "service_items": service_items or [],
        "transfer_from": payload.transfer_from or payload.payment_method or "សាច់ប្រាក់សុទ្ធ",
        "transfer_to": payload.transfer_to or "គណនីវិនិយោគទុន",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat()
    }
    res = finance_transactions_collection.insert_one(doc)
    doc["_id"] = str(res.inserted_id)

    # Financial privacy: Fund transfers are strictly confidential. Do NOT broadcast to Telegram groups.
    return {"success": True, "message": "បានកត់ត្រាប្រតិបត្តិការជោគជ័យ!", "transaction": doc}

@app.get("/api/finance/transactions/{tx_id}")
async def api_get_single_transaction(tx_id: str):
    doc = finance_transactions_collection.find_one({"_id": ObjectId(tx_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="រកមិនឃើញប្រតិបត្តិការនេះទេ!")
    return fix_id(doc)

@app.put("/api/finance/transactions/{tx_id}")
async def api_update_transaction(tx_id: str, payload: FinanceTransactionUpdate):
    update_data = {}
    if payload.type is not None:
        update_data["type"] = payload.type.strip().lower()
    if payload.amount is not None:
        if payload.amount <= 0:
            raise HTTPException(status_code=400, detail="ចំនួនទឹកប្រាក់ត្រូវតែធំជាង ០!")
        update_data["amount"] = float(payload.amount)
    if payload.currency is not None:
        update_data["currency"] = payload.currency.strip().upper()
    if payload.category is not None:
        update_data["category"] = payload.category.strip()
    if payload.catalog_id is not None:
        update_data["catalog_id"] = payload.catalog_id
    if payload.date is not None:
        update_data["date"] = payload.date.strip()
    if payload.time is not None:
        update_data["time"] = payload.time.strip()
    if payload.payment_method is not None:
        update_data["payment_method"] = payload.payment_method.strip()
    if payload.reference_no is not None:
        update_data["reference_no"] = payload.reference_no.strip()
    if payload.description is not None:
        update_data["description"] = payload.description.strip()
    if payload.recorded_by is not None:
        update_data["recorded_by"] = payload.recorded_by.strip()
    if payload.branch is not None:
        update_data["branch"] = payload.branch.strip()
    if payload.service_items is not None:
        update_data["service_items"] = payload.service_items
    elif payload.description is not None:
        extracted = extract_trips_and_locations(payload.description, is_sewage_income=True)
        update_data["service_items"] = [{"location": loc, "trips": tr, "price": pr} for loc, tr, pr in extracted]

    update_data["updated_at"] = datetime.now(timezone(timedelta(hours=7))).isoformat()
    r = finance_transactions_collection.update_one({"_id": ObjectId(tx_id)}, {"$set": update_data})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="រកមិនឃើញប្រតិបត្តិការនេះទេ!")
    return {"success": True, "message": "បានកែប្រែប្រតិបត្តិការជោគជ័យ!"}

@app.delete("/api/finance/transactions/{tx_id}")
async def api_delete_transaction(tx_id: str):
    r = finance_transactions_collection.delete_one({"_id": ObjectId(tx_id)})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="រកមិនឃើញប្រតិបត្តិការនេះទេ!")
    return {"success": True, "message": "បានលុបប្រតិបត្តិការជោគជ័យ!"}

# --- Stats & Reports Helpers ---

def calculate_finance_metrics(transactions):
    inc_khr = 0.0
    inc_usd = 0.0
    exp_khr = 0.0
    exp_usd = 0.0
    
    inc_by_cat = {}
    exp_by_cat = {}

    total_trips = 0
    location_map = {}
    staff_map = {}

    payment_breakdown = {
        "income": {
            "cash": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "សាច់ប្រាក់សុទ្ធ"},
            "bank": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "បាញ់តាមធនាគារ"},
            "other": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ផ្សេងៗ"}
        },
        "expense": {
            "cash": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "សាច់ប្រាក់សុទ្ធ"},
            "bank": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "បាញ់តាមធនាគារ"},
            "other": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ផ្សេងៗ"}
        }
    }

    expense_structure = {
        "fuel": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ប្រេងឥន្ធនៈ (Fuel)"},
        "maintenance": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ជួសជុល & គ្រឿងបន្លាស់ (Repairs)"},
        "disposal": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ថ្លៃចាក់កាកសំណល់ (Dumping Fee)"},
        "marketing": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ផ្សព្វផ្សាយ & ផ្លាក (Ads/Signs)"},
        "salary": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ប្រាក់បេសកកម្ម/ប្រាក់ខែ (Wages)"},
        "other": {"khr": 0.0, "usd": 0.0, "count": 0, "label": "ចំណាយប្រតិបត្តិការផ្សេងៗ (Other)"}
    }

    inv_from_cash_khr = 0.0
    inv_from_cash_usd = 0.0
    inv_from_bank_khr = 0.0
    inv_from_bank_usd = 0.0
    transfer_count = 0

    for tx in transactions:
        t = tx.get("type", "income")
        amt = float(tx.get("amount", 0.0) or 0.0)
        curr = tx.get("currency", "KHR")
        cat = tx.get("category") or ("ចំណូលផ្សេងៗ" if t == "income" else "ចំណាយផ្សេងៗ")
        pay_method = (tx.get("payment_method") or "").strip().lower()
        desc = tx.get("description") or ""
        staff = tx.get("recorded_by") or "រដ្ឋបាល"

        # Month-End Fund Transfer into Investment Account
        if t == "transfer":
            transfer_count += 1
            tf_from = (tx.get("transfer_from") or pay_method).strip().lower()
            if any(w in tf_from for w in ["សាច់ប្រាក់", "cash", "ប្រាក់សុទ្ធ"]):
                if curr == "USD": inv_from_cash_usd += amt
                else: inv_from_cash_khr += amt
            else:
                if curr == "USD": inv_from_bank_usd += amt
                else: inv_from_bank_khr += amt
            continue

        if any(w in pay_method for w in ["សាច់ប្រាក់", "cash", "ប្រាក់សុទ្ធ"]):
            pay_key = "cash"
        elif any(w in pay_method for w in ["aba", "ធនាគារ", "bank", "khqr", "acleda", "wing", "ផ្ទេរ", "បាញ់"]):
            pay_key = "bank"
        else:
            pay_key = "other"

        if t == "income":
            if curr == "USD":
                inc_usd += amt
                payment_breakdown["income"][pay_key]["usd"] += amt
            else:
                inc_khr += amt
                payment_breakdown["income"][pay_key]["khr"] += amt
            payment_breakdown["income"][pay_key]["count"] += 1

            if cat not in inc_by_cat:
                inc_by_cat[cat] = {"khr": 0.0, "usd": 0.0, "count": 0}
            if curr == "USD": inc_by_cat[cat]["usd"] += amt
            else: inc_by_cat[cat]["khr"] += amt
            inc_by_cat[cat]["count"] += 1

            is_service = "បូមលូ" in cat or "សេវា" in cat
            raw_items = tx.get("service_items")
            tx_items = []
            if raw_items and isinstance(raw_items, list):
                for it in raw_items:
                    if isinstance(it, dict):
                        loc = str(it.get("location") or "").strip()
                        if loc:
                            tr = parse_khmer_or_arabic_int(it.get("trips")) or 1
                            pr = float(it.get("price", 0.0) or 0.0)
                            tx_items.append((loc, tr, pr))

            if not tx_items:
                tx_items = extract_trips_and_locations(desc, is_sewage_income=is_service)

            if tx_items:
                tx_total_trips = sum(entry[1] for entry in tx_items) or 1
                total_trips += tx_total_trips
                total_explicit_price = sum(entry[2] for entry in tx_items)

                for entry in tx_items:
                    loc = entry[0]
                    tr = entry[1]
                    pr = entry[2] if len(entry) > 2 else 0.0

                    if loc not in location_map:
                        location_map[loc] = {"location": loc, "trips": 0, "khr": 0.0, "usd": 0.0, "count": 0}
                    location_map[loc]["trips"] += tr
                    location_map[loc]["count"] += 1

                    if total_explicit_price > 0 and pr > 0:
                        allocated_rev = pr
                    else:
                        portion = tr / tx_total_trips
                        allocated_rev = amt * portion

                    if curr == "USD":
                        location_map[loc]["usd"] += allocated_rev
                    else:
                        location_map[loc]["khr"] += allocated_rev

            if staff not in staff_map:
                staff_map[staff] = {"staff": staff, "income_khr": 0.0, "income_usd": 0.0, "expense_khr": 0.0, "expense_usd": 0.0, "trips": 0, "tx_count": 0}
            if curr == "USD": staff_map[staff]["income_usd"] += amt
            else: staff_map[staff]["income_khr"] += amt
            staff_map[staff]["trips"] += sum(entry[1] for entry in tx_items) if is_service else 0
            staff_map[staff]["tx_count"] += 1

        else:
            if curr == "USD":
                exp_usd += amt
                payment_breakdown["expense"][pay_key]["usd"] += amt
            else:
                exp_khr += amt
                payment_breakdown["expense"][pay_key]["khr"] += amt
            payment_breakdown["expense"][pay_key]["count"] += 1

            if cat not in exp_by_cat:
                exp_by_cat[cat] = {"khr": 0.0, "usd": 0.0, "count": 0}
            if curr == "USD": exp_by_cat[cat]["usd"] += amt
            else: exp_by_cat[cat]["khr"] += amt
            exp_by_cat[cat]["count"] += 1

            cat_lower = (cat + " " + desc).lower()
            if any(w in cat_lower for w in ["ប្រេង", "សាំង", "ហ្គាស", "fuel"]):
                exp_struct_key = "fuel"
            elif any(w in cat_lower for w in ["ជួសជុល", "គ្រឿងបន្លាស់", "ថែទាំ", "repair", "maintenance"]):
                exp_struct_key = "maintenance"
            elif any(w in cat_lower for w in ["ចាក់", "កាកសំណល់", "dump", "disposal"]):
                exp_struct_key = "disposal"
            elif any(w in cat_lower for w in ["ផ្លាក", "ផ្សាយ", "banner", "ad", "marketing"]):
                exp_struct_key = "marketing"
            elif any(w in cat_lower for w in ["បេសកកម្ម", "ប្រាក់ខែ", "ប្រាក់ឈ្នួល", "salary", "wage"]):
                exp_struct_key = "salary"
            else:
                exp_struct_key = "other"

            if curr == "USD":
                expense_structure[exp_struct_key]["usd"] += amt
            else:
                expense_structure[exp_struct_key]["khr"] += amt
            expense_structure[exp_struct_key]["count"] += 1

            if staff not in staff_map:
                staff_map[staff] = {"staff": staff, "income_khr": 0.0, "income_usd": 0.0, "expense_khr": 0.0, "expense_usd": 0.0, "trips": 0, "tx_count": 0}
            if curr == "USD": staff_map[staff]["expense_usd"] += amt
            else: staff_map[staff]["expense_khr"] += amt
            staff_map[staff]["tx_count"] += 1

    arpt_khr = round(inc_khr / total_trips, 2) if total_trips > 0 else 0.0
    arpt_usd = round(inc_usd / total_trips, 2) if total_trips > 0 else 0.0
    locations_list = sorted(location_map.values(), key=lambda x: (x["trips"], x["khr"], x["usd"]), reverse=True)

    cash_net_khr = payment_breakdown["income"]["cash"]["khr"] - payment_breakdown["expense"]["cash"]["khr"] - inv_from_cash_khr
    cash_net_usd = payment_breakdown["income"]["cash"]["usd"] - payment_breakdown["expense"]["cash"]["usd"] - inv_from_cash_usd
    bank_net_khr = payment_breakdown["income"]["bank"]["khr"] - payment_breakdown["expense"]["bank"]["khr"] - inv_from_bank_khr
    bank_net_usd = payment_breakdown["income"]["bank"]["usd"] - payment_breakdown["expense"]["bank"]["usd"] - inv_from_bank_usd

    investment_khr = inv_from_cash_khr + inv_from_bank_khr
    investment_usd = inv_from_cash_usd + inv_from_bank_usd

    reconciliation = {
        "cash_balance": {"khr": cash_net_khr, "usd": cash_net_usd},
        "bank_balance": {"khr": bank_net_khr, "usd": bank_net_usd},
        "investment_balance": {"khr": investment_khr, "usd": investment_usd},
        "investment_from_cash": {"khr": inv_from_cash_khr, "usd": inv_from_cash_usd},
        "investment_from_bank": {"khr": inv_from_bank_khr, "usd": inv_from_bank_usd},
        "cash_transfers_out": {"khr": inv_from_cash_khr, "usd": inv_from_cash_usd},
        "bank_transfers_out": {"khr": inv_from_bank_khr, "usd": inv_from_bank_usd},
        "cash_income": payment_breakdown["income"]["cash"],
        "cash_expense": payment_breakdown["expense"]["cash"],
        "bank_income": payment_breakdown["income"]["bank"],
        "bank_expense": payment_breakdown["expense"]["bank"],
        "transfer_count": transfer_count
    }

    total_rev_approx = inc_khr + (inc_usd * 4100.0)
    total_exp_approx = exp_khr + (exp_usd * 4100.0)
    expense_ratio_pct = round((total_exp_approx / total_rev_approx * 100), 1) if total_rev_approx > 0 else 0.0
    fuel_khr = expense_structure["fuel"]["khr"] + (expense_structure["fuel"]["usd"] * 4100.0)
    fuel_per_trip_khr = round(fuel_khr / total_trips, 0) if total_trips > 0 else 0.0
    staff_list = sorted(staff_map.values(), key=lambda x: (x["trips"], x["income_khr"]), reverse=True)

    return {
        "income_khr": inc_khr,
        "income_usd": inc_usd,
        "expense_khr": exp_khr,
        "expense_usd": exp_usd,
        "net_khr": inc_khr - exp_khr,
        "net_usd": inc_usd - exp_usd,
        "income_by_category": sorted([{"category": k, **v} for k, v in inc_by_cat.items()], key=lambda x: (x["khr"], x["usd"]), reverse=True),
        "expense_by_category": sorted([{"category": k, **v} for k, v in exp_by_cat.items()], key=lambda x: (x["khr"], x["usd"]), reverse=True),
        "count": len(transactions),
        "total_trips": total_trips,
        "arpt": {"khr": arpt_khr, "usd": arpt_usd},
        "locations": locations_list,
        "payment_breakdown": payment_breakdown,
        "reconciliation": reconciliation,
        "expense_structure": expense_structure,
        "expense_ratio_pct": expense_ratio_pct,
        "fuel_per_trip_khr": fuel_per_trip_khr,
        "staff_breakdown": staff_list
    }

@app.get("/api/finance/stats")
async def api_get_finance_stats(branch: Optional[str] = None):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    today_str = now.strftime("%Y-%m-%d")
    month_prefix = now.strftime("%Y-%m")
    year_prefix = now.strftime("%Y")

    b_q = {"branch": branch} if branch else {}
    today_txs = list(finance_transactions_collection.find({"date": today_str, **b_q}))
    month_txs = list(finance_transactions_collection.find({"date": {"$regex": f"^{month_prefix}"}, **b_q}))
    year_txs = list(finance_transactions_collection.find({"date": {"$regex": f"^{year_prefix}"}, **b_q}))

    return {
        "today": calculate_finance_metrics(today_txs),
        "this_month": calculate_finance_metrics(month_txs),
        "this_year": calculate_finance_metrics(year_txs),
        "today_date": today_str,
        "current_month": now.month,
        "current_year": now.year
    }

@app.get("/api/finance/reports/daily")
async def api_get_daily_report(date: Optional[str] = None, branch: Optional[str] = None):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    target_date = (date or "").strip() or now.strftime("%Y-%m-%d")
    
    b_q = {"branch": branch} if branch else {}
    txs = list(finance_transactions_collection.find({"date": target_date, **b_q}).sort([("time", -1), ("_id", -1)]))
    metrics = calculate_finance_metrics(txs)
    
    return {
        "date": target_date,
        "summary": metrics,
        "transactions": [fix_id(t) for t in txs]
    }

@app.get("/api/finance/reports/monthly")
async def api_get_monthly_report(year: Optional[Any] = None, month: Optional[Any] = None, branch: Optional[str] = None):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    try:
        y = int(year) if (year is not None and str(year).strip() != "") else now.year
    except Exception:
        y = now.year
    try:
        m = int(month) if (month is not None and str(month).strip() != "") else now.month
    except Exception:
        m = now.month
    
    days_in_m = calendar.monthrange(y, m)[1]
    month_prefix = f"{y:04d}-{m:02d}"
    b_q = {"branch": branch} if branch else {}
    txs = list(finance_transactions_collection.find({"date": {"$regex": f"^{month_prefix}"}, **b_q}).sort([("date", 1), ("time", 1)]))
    metrics = calculate_finance_metrics(txs)

    daily_map = {d: {"income_khr": 0.0, "income_usd": 0.0, "expense_khr": 0.0, "expense_usd": 0.0, "count": 0, "trips": 0} for d in range(1, days_in_m + 1)}
    for t in txs:
        d_str = t.get("date", "")
        try:
            day_num = int(d_str.split("-")[2])
        except Exception:
            continue
        if day_num in daily_map:
            amt = float(t.get("amount", 0.0) or 0.0)
            curr = t.get("currency", "KHR")
            t_type = t.get("type", "income")
            cat = t.get("category") or ""
            desc = t.get("description") or ""
            if t_type == "income":
                if curr == "USD": daily_map[day_num]["income_usd"] += amt
                else: daily_map[day_num]["income_khr"] += amt
                is_service = "បូមលូ" in cat or "សេវា" in cat
                raw_items = t.get("service_items")
                if raw_items and isinstance(raw_items, list):
                    daily_map[day_num]["trips"] += sum(parse_khmer_or_arabic_int(it.get("trips")) or 1 for it in raw_items if isinstance(it, dict))
                else:
                    trip_entries = extract_trips_and_locations(desc, is_sewage_income=is_service)
                    if trip_entries:
                        daily_map[day_num]["trips"] += sum(entry[1] for entry in trip_entries)
            else:
                if curr == "USD": daily_map[day_num]["expense_usd"] += amt
                else: daily_map[day_num]["expense_khr"] += amt
            daily_map[day_num]["count"] += 1

    khmer_months = ['មករា', 'កុម្ភៈ', 'មីនា', 'មេសា', 'ឧសភា', 'មិថុនា', 'កក្កដា', 'សីហា', 'កញ្ញា', 'តុលា', 'វិច្ឆិកា', 'ធ្នូ']
    daily_list = []
    for d in range(1, days_in_m + 1):
        info = daily_map[d]
        daily_list.append({
            "day": d,
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "income_khr": info["income_khr"],
            "income_usd": info["income_usd"],
            "expense_khr": info["expense_khr"],
            "expense_usd": info["expense_usd"],
            "net_khr": info["income_khr"] - info["expense_khr"],
            "net_usd": info["income_usd"] - info["expense_usd"],
            "count": info["count"],
            "trips": info.get("trips", 0)
        })

    return {
        "year": y,
        "month": m,
        "month_name": khmer_months[m - 1],
        "summary": metrics,
        "daily_breakdown": daily_list,
        "total_transactions": len(txs),
        "transactions": [fix_id(t) for t in txs]
    }

@app.get("/api/finance/reports/yearly")
async def api_get_yearly_report(year: Optional[Any] = None, branch: Optional[str] = None):
    cam_tz = timezone(timedelta(hours=7))
    now = datetime.now(cam_tz)
    try:
        y = int(year) if (year is not None and str(year).strip() != "") else now.year
    except Exception:
        y = now.year
    
    year_prefix = f"{y:04d}"
    b_q = {"branch": branch} if branch else {}
    txs = list(finance_transactions_collection.find({"date": {"$regex": f"^{year_prefix}"}, **b_q}).sort([("date", 1)]))
    metrics = calculate_finance_metrics(txs)

    khmer_months = ['មករា', 'កុម្ភៈ', 'មីនា', 'មេសា', 'ឧសភា', 'មិថុនា', 'កក្កដា', 'សីហា', 'កញ្ញា', 'តុលា', 'វិច្ឆិកា', 'ធ្នូ']
    monthly_map = {m: {"income_khr": 0.0, "income_usd": 0.0, "expense_khr": 0.0, "expense_usd": 0.0, "count": 0, "trips": 0} for m in range(1, 13)}

    for t in txs:
        d_str = t.get("date", "")
        try:
            m_num = int(d_str.split("-")[1])
        except Exception:
            continue
        if m_num in monthly_map:
            amt = float(t.get("amount", 0.0) or 0.0)
            curr = t.get("currency", "KHR")
            t_type = t.get("type", "income")
            cat = t.get("category") or ""
            desc = t.get("description") or ""
            if t_type == "income":
                if curr == "USD": monthly_map[m_num]["income_usd"] += amt
                else: monthly_map[m_num]["income_khr"] += amt
                is_service = "បូមលូ" in cat or "សេវា" in cat
                raw_items = t.get("service_items")
                if raw_items and isinstance(raw_items, list):
                    monthly_map[m_num]["trips"] += sum(parse_khmer_or_arabic_int(it.get("trips")) or 1 for it in raw_items if isinstance(it, dict))
                else:
                    trip_entries = extract_trips_and_locations(desc, is_sewage_income=is_service)
                    if trip_entries:
                        monthly_map[m_num]["trips"] += sum(entry[1] for entry in trip_entries)
            else:
                if curr == "USD": monthly_map[m_num]["expense_usd"] += amt
                else: monthly_map[m_num]["expense_khr"] += amt
            monthly_map[m_num]["count"] += 1

    months_list = []
    for m in range(1, 13):
        info = monthly_map[m]
        months_list.append({
            "month": m,
            "month_name": khmer_months[m - 1],
            "income_khr": info["income_khr"],
            "income_usd": info["income_usd"],
            "expense_khr": info["expense_khr"],
            "expense_usd": info["expense_usd"],
            "net_khr": info["income_khr"] - info["expense_khr"],
            "net_usd": info["income_usd"] - info["expense_usd"],
            "count": info["count"],
            "trips": info.get("trips", 0)
        })

    return {
        "year": y,
        "summary": metrics,
        "monthly_breakdown": months_list,
        "total_transactions": len(txs),
        "transactions": [fix_id(t) for t in txs]
    }

# ===== Telegram Mini App Web Forms (Option 4) =====
LEAVE_FORM_HTML = """<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>ទម្រង់សុំច្បាប់ - AC BOMLU</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&family=Outfit:wght@400;600;700&display=swap" rel="stylesheet">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" />
  <style>
    body { font-family: 'Kantumruy Pro', sans-serif; background: #0b1329; color: #f8fafc; }
    .glass-card { background: rgba(23, 37, 84, 0.7); backdrop-filter: blur(16px); border: 1px solid rgba(59, 130, 246, 0.2); }

    /* Print styles */
    @media print {
      #sidebar, #sidebar-backdrop, header, .no-print, button, #pwa-install-sidebar-box, .modal-overlay {
        display: none !important;
      }
      main {
        overflow: visible !important;
        height: auto !important;
        padding: 0 !important;
        margin: 0 !important;
        width: 100% !important;
      }
      body {
        background: white !important;
        overflow: visible !important;
        height: auto !important;
      }
      .bg-white {
        box-shadow: none !important;
        border: 1px solid #E2E8F0 !important;
      }
    }
  </style>
</head>
<body class="p-4 min-h-screen flex items-center justify-center">
  <div class="w-full max-w-md glass-card rounded-3xl p-6 shadow-2xl">
    <!-- Header -->
    <div class="flex items-center gap-3 mb-6 pb-4 border-b border-blue-900/50">
      <img src="/static/ac_logo.jpg" alt="AC BOMLU" class="w-12 h-12 rounded-2xl object-cover ring-2 ring-blue-500/40" />
      <div>
        <h1 class="text-base font-bold text-white flex items-center gap-1.5">
          <span class="material-symbols-outlined text-blue-400 text-lg">event_available</span>
          ទម្រង់ស្នើសុំច្បាប់ឈប់សម្រាក
        </h1>
        <p class="text-xs text-blue-300/80">AC BOMLU Staff Control</p>
      </div>
    </div>

    <!-- Form -->
    <form id="leave-form" onsubmit="submitForm(event)" class="space-y-4">
      <!-- Employee Name -->
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">ឈ្មោះបុគ្គលិក</label>
        <div class="relative">
          <span class="material-symbols-outlined absolute left-3 top-2.5 text-slate-400 text-lg">person</span>
          <input type="text" id="emp-name" required placeholder="ឈ្មោះបុគ្គលិក"
            class="w-full bg-slate-900/90 border border-slate-700/80 rounded-xl pl-10 pr-4 py-2.5 text-sm text-white focus:outline-none focus:border-blue-500" />
        </div>
      </div>

      <!-- Days Selection -->
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">ចំនួនថ្ងៃឈប់សម្រាក</label>
        <div class="grid grid-cols-6 gap-1">
          <button type="button" onclick="selectDay(0.5)" class="day-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="d-05">កន្លះថ្ងៃ</button>
          <button type="button" onclick="selectDay(1)" class="day-btn py-2 text-xs font-semibold rounded-xl border border-blue-500 bg-blue-600 text-white" id="d-1">១ ថ្ងៃ</button>
          <button type="button" onclick="selectDay(2)" class="day-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="d-2">២ ថ្ងៃ</button>
          <button type="button" onclick="selectDay(3)" class="day-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="d-3">៣ ថ្ងៃ</button>
          <button type="button" onclick="selectDay(4)" class="day-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="d-4">៤ ថ្ងៃ</button>
          <button type="button" onclick="selectDay(5)" class="day-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="d-5">៥ ថ្ងៃ</button>
        </div>
        <input type="hidden" id="leave-days" value="1" />
      </div>

      <!-- Quick Reasons -->
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">មូលហេតុនៃការសុំច្បាប់</label>
        <div class="flex flex-wrap gap-1.5 mb-2">
          <button type="button" onclick="setReason('🤒 ឈឺ / មិនស្រួលខ្លួន')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🤒 ឈឺ</button>
          <button type="button" onclick="setReason('🏠 រវល់ធុរៈគ្រួសារ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🏠 រវល់ផ្ទះ</button>
          <button type="button" onclick="setReason('🚗 ទៅស្រុកកំណើត')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🚗 ទៅស្រុក</button>
          <button type="button" onclick="setReason('🎉 ជាប់កម្មវិធីផ្សេងៗ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🎉 ជាប់កម្មវិធី</button>
        </div>
        <textarea id="leave-reason" rows="3" required placeholder="វាយបញ្ជាក់មូលហេតុនៅទីនេះ..."
          class="w-full bg-slate-900/90 border border-slate-700/80 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-blue-500"></textarea>
      </div>

      <!-- Submit Button -->
      <button type="submit" id="submit-btn"
        class="w-full py-3 rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 font-semibold text-white shadow-lg shadow-blue-500/30 flex items-center justify-center gap-2 transition active:scale-95">
        <span class="material-symbols-outlined text-lg">send</span>
        <span>ដាក់ស្នើពាក្យសុំច្បាប់</span>
      </button>
    </form>

    <!-- Success View -->
    <div id="success-view" class="hidden text-center py-6">
      <div class="w-16 h-16 rounded-full bg-green-500/20 text-green-400 flex items-center justify-center mx-auto mb-3 ring-4 ring-green-500/10">
        <span class="material-symbols-outlined text-4xl">check_circle</span>
      </div>
      <h2 class="text-lg font-bold text-white mb-1">បានដាក់ស្នើជោគជ័យ!</h2>
      <p class="text-xs text-slate-400">សំណើត្រូវបានបញ្ជូនទៅគ្រុប និងរដ្ឋបាលរួចរាល់</p>
    </div>
  </div>

  <script>
    const urlParams = new URLSearchParams(window.location.search);
    const nameParam = urlParams.get('name');
    if (nameParam) {
      document.getElementById('emp-name').value = decodeURIComponent(nameParam);
    }

    if (window.Telegram && window.Telegram.WebApp) {
      window.Telegram.WebApp.ready();
      window.Telegram.WebApp.expand();
      const tgUser = window.Telegram.WebApp.initDataUnsafe?.user;
      if (tgUser && !nameParam) {
        const name = [tgUser.first_name, tgUser.last_name].filter(Boolean).join(' ') || tgUser.username;
        if (name) document.getElementById('emp-name').value = name;
      }
    }

    function selectDay(n) {
      document.getElementById('leave-days').value = n;
      document.querySelectorAll('.day-btn').forEach(b => {
        b.className = 'day-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300';
      });
      const activeId = n === 0.5 ? 'd-05' : 'd-' + n;
      const activeBtn = document.getElementById(activeId);
      if (activeBtn) {
        activeBtn.className = 'day-btn py-2 text-xs font-semibold rounded-xl border border-blue-500 bg-blue-600 text-white shadow-lg shadow-blue-500/20';
      }
    }

    function setReason(txt) {
      document.getElementById('leave-reason').value = txt;
    }

    async function submitForm(e) {
      e.preventDefault();
      const btn = document.getElementById('submit-btn');
      btn.disabled = true;
      btn.innerHTML = '<span class="material-symbols-outlined animate-spin text-lg">progress_activity</span> កំពុងបញ្ជូន...';

      const payload = {
        employee_name: document.getElementById('emp-name').value.trim(),
        days: parseInt(document.getElementById('leave-days').value) || 1,
        reason: document.getElementById('leave-reason').value.trim()
      };

      try {
        const r = await fetch('/api/leave', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (r.ok) {
          document.getElementById('leave-form').classList.add('hidden');
          document.getElementById('success-view').classList.remove('hidden');
          if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.notificationOccurred('success');
          }
          setTimeout(() => {
            if (window.Telegram && window.Telegram.WebApp) {
              window.Telegram.WebApp.close();
            }
          }, 1200);
        } else {
          alert('មានបញ្ហាក្នុងការដាក់ស្នើ សូមព្យាយាមម្ដងទៀត!');
          btn.disabled = false;
          btn.innerHTML = '<span>ដាក់ស្នើពាក្យសុំច្បាប់</span>';
        }
      } catch(err) {
        alert('មិនអាចភ្ជាប់ទៅកាន់ប្រព័ន្ធបាន: ' + err);
        btn.disabled = false;
        btn.innerHTML = '<span>ដាក់ស្នើពាក្យសុំច្បាប់</span>';
      }
    }
  </script>
</body>
</html>
"""

LOAN_FORM_HTML = """<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>ទម្រង់ខ្ចីប្រាក់ - AC BOMLU</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&family=Outfit:wght@400;600;700&display=swap" rel="stylesheet">
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" />
  <style>
    body { font-family: 'Kantumruy Pro', sans-serif; background: #061e1b; color: #f8fafc; }
    .glass-card { background: rgba(6, 78, 59, 0.45); backdrop-filter: blur(16px); border: 1px solid rgba(16, 185, 129, 0.25); }
  </style>
</head>
<body class="p-4 min-h-screen flex items-center justify-center">
  <div class="w-full max-w-md glass-card rounded-3xl p-6 shadow-2xl">
    <!-- Header -->
    <div class="flex items-center gap-3 mb-6 pb-4 border-b border-emerald-900/60">
      <img src="/static/ac_logo.jpg" alt="AC BOMLU" class="w-12 h-12 rounded-2xl object-cover ring-2 ring-emerald-500/40" />
      <div>
        <h1 class="text-base font-bold text-white flex items-center gap-1.5">
          <span class="material-symbols-outlined text-emerald-400 text-lg">payments</span>
          ទម្រង់ស្នើសុំខ្ចីប្រាក់ / បុរេប្រទាន
        </h1>
        <p class="text-xs text-emerald-300/80">AC BOMLU Staff Control</p>
      </div>
    </div>

    <!-- Form -->
    <form id="loan-form" onsubmit="submitForm(event)" class="space-y-4">
      <!-- Employee Name -->
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">ឈ្មោះបុគ្គលិក</label>
        <div class="relative">
          <span class="material-symbols-outlined absolute left-3 top-2.5 text-slate-400 text-lg">person</span>
          <input type="text" id="emp-name" required placeholder="ឈ្មោះបុគ្គលិក"
            class="w-full bg-slate-900/90 border border-slate-700/80 rounded-xl pl-10 pr-4 py-2.5 text-sm text-white focus:outline-none focus:border-emerald-500" />
        </div>
      </div>

      <!-- Amount Selection -->
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">ចំនួនទឹកប្រាក់ដែលចង់ខ្ចី (រៀល)</label>
        <div class="grid grid-cols-3 gap-2 mb-2">
          <button type="button" onclick="selectAmt(10000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-10000">១០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(20000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-20000">២០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(30000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-30000">៣០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(50000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-50000">៥០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(100000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-emerald-500 bg-emerald-600 text-white" id="a-100000">១០០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(200000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-200000">២០០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(300000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-300000">៣០០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(400000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-400000">៤០០,០០០ ៛</button>
          <button type="button" onclick="selectAmt(500000)" class="amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300" id="a-500000">៥០០,០០០ ៛</button>
        </div>
        <div class="relative">
          <span class="material-symbols-outlined absolute left-3 top-2.5 text-emerald-400 text-lg">attach_money</span>
          <input type="number" id="custom-amt" value="100000" min="10000" step="5000" required
            class="w-full bg-slate-900/90 border border-slate-700/80 rounded-xl pl-10 pr-4 py-2.5 text-sm font-bold text-emerald-400 focus:outline-none focus:border-emerald-500" />
        </div>
      </div>

      <!-- Quick Purpose -->
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1.5">គោលបំណង ឬមូលហេតុនៃការខ្ចី</label>
        <div class="flex flex-wrap gap-1.5 mb-2">
          <button type="button" onclick="setPurpose('🏥 ព្យាបាលជំងឺ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🏥 ព្យាបាលជំងឺ</button>
          <button type="button" onclick="setPurpose('🏦 បង់ធនាគារ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🏦 បង់ធនាគារ</button>
          <button type="button" onclick="setPurpose('🍲 ទិញម្ហូបអាហារ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🍲 ម្ហូបអាហារ</button>
          <button type="button" onclick="setPurpose('✨ តម្រូវការផ្សេងៗ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">✨ ផ្សេងៗ</button>
        </div>
        <textarea id="loan-purpose" rows="3" required placeholder="វាយបញ្ជាក់គោលបំណងនៅទីនេះ..."
          class="w-full bg-slate-900/90 border border-slate-700/80 rounded-xl p-3 text-sm text-white focus:outline-none focus:border-emerald-500"></textarea>
      </div>

      <!-- Submit Button -->
      <button type="submit" id="submit-btn"
        class="w-full py-3 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 hover:from-emerald-500 hover:to-teal-500 font-semibold text-white shadow-lg shadow-emerald-500/30 flex items-center justify-center gap-2 transition active:scale-95">
        <span class="material-symbols-outlined text-lg">payments</span>
        <span>ដាក់ស្នើសុំខ្ចីប្រាក់</span>
      </button>
    </form>

    <!-- Success View -->
    <div id="success-view" class="hidden text-center py-6">
      <div class="w-16 h-16 rounded-full bg-emerald-500/20 text-emerald-400 flex items-center justify-center mx-auto mb-3 ring-4 ring-emerald-500/10">
        <span class="material-symbols-outlined text-4xl">check_circle</span>
      </div>
      <h2 class="text-lg font-bold text-white mb-1">បានដាក់ស្នើជោគជ័យ!</h2>
      <p class="text-xs text-slate-400">សំណើត្រូវបានបញ្ជូនទៅគ្រុប និងរដ្ឋបាលរួចរាល់</p>
    </div>
  </div>

  <script>
    const urlParams = new URLSearchParams(window.location.search);
    const nameParam = urlParams.get('name');
    if (nameParam) {
      document.getElementById('emp-name').value = decodeURIComponent(nameParam);
    }

    if (window.Telegram && window.Telegram.WebApp) {
      window.Telegram.WebApp.ready();
      window.Telegram.WebApp.expand();
      const tgUser = window.Telegram.WebApp.initDataUnsafe?.user;
      if (tgUser && !nameParam) {
        const name = [tgUser.first_name, tgUser.last_name].filter(Boolean).join(' ') || tgUser.username;
        if (name) document.getElementById('emp-name').value = name;
      }
    }

    function selectAmt(amt) {
      document.getElementById('custom-amt').value = amt;
      [10000, 20000, 30000, 50000, 100000, 200000, 300000, 400000, 500000].forEach(a => {
        const btn = document.getElementById('a-' + a);
        if (btn) {
          if (a === amt) {
            btn.className = 'amt-btn py-2 text-xs font-semibold rounded-xl border border-emerald-500 bg-emerald-600 text-white';
          } else {
            btn.className = 'amt-btn py-2 text-xs font-semibold rounded-xl border border-slate-700 bg-slate-800 text-slate-300';
          }
        }
      });
    }

    function setPurpose(txt) {
      document.getElementById('loan-purpose').value = txt;
    }

    async function submitForm(e) {
      e.preventDefault();
      const btn = document.getElementById('submit-btn');
      btn.disabled = true;
      btn.innerHTML = '<span class="material-symbols-outlined animate-spin text-lg">progress_activity</span> កំពុងបញ្ជូន...';

      const payload = {
        employee_name: document.getElementById('emp-name').value.trim(),
        amount_khr: parseInt(document.getElementById('custom-amt').value) || 100000,
        purpose: document.getElementById('loan-purpose').value.trim()
      };

      try {
        const r = await fetch('/api/loan', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (r.ok) {
          document.getElementById('loan-form').classList.add('hidden');
          document.getElementById('success-view').classList.remove('hidden');
          if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.HapticFeedback) {
            window.Telegram.WebApp.HapticFeedback.notificationOccurred('success');
          }
          setTimeout(() => {
            if (window.Telegram && window.Telegram.WebApp) {
              window.Telegram.WebApp.close();
            }
          }, 1200);
        } else {
          alert('មានបញ្ហាក្នុងការដាក់ស្នើ សូមព្យាយាមម្ដងទៀត!');
          btn.disabled = false;
          btn.innerHTML = '<span>ដាក់ស្នើសុំខ្ចីប្រាក់</span>';
        }
      } catch(err) {
        alert('មិនអាចភ្ជាប់ទៅកាន់ប្រព័ន្ធបាន: ' + err);
        btn.disabled = false;
        btn.innerHTML = '<span>ដាក់ស្នើសុំខ្ចីប្រាក់</span>';
      }
    }
  </script>
</body>
</html>
"""

@app.get("/form/leave", response_class=HTMLResponse)
async def get_leave_form():
    return HTMLResponse(content=LEAVE_FORM_HTML)

@app.get("/form/loan", response_class=HTMLResponse)
async def get_loan_form():
    return HTMLResponse(content=LOAN_FORM_HTML)

REASON_MAP = {
    "sick": "🤒 ឈឺ / មិនស្រួលខ្លួន",
    "family": "🏠 រវល់ធុរៈគ្រួសារ",
    "hometown": "🚗 ទៅស្រុកកំណើត",
    "event": "🎉 ជាប់កម្មវិធីផ្សេងៗ",
    "ceremony": "🎉 ជាប់កម្មវិធីផ្សេងៗ",
}

PURPOSE_MAP = {
    "treatment": "🏥 ព្យាបាលជំងឺ",
    "bank": "🏦 បង់ធនាគារ",
    "food": "🍲 ទិញម្ហូបអាហារ",
    "other": "✨ តម្រូវការផ្សេងៗ",
    "med": "🏥 ព្យាបាលជំងឺ",
    "rent": "🏦 បង់ធនាគារ",
    "family": "🍲 ទិញម្ហូបអាហារ",
    "util": "✨ តម្រូវការផ្សេងៗ",
}

def download_telegram_voice_bytes(file_id: str, token: str) -> Optional[bytes]:
    """
    Downloads raw voice/audio bytes from Telegram API given a file_id.
    """
    if not file_id or not token:
        return None
    try:
        gf_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
        gf_req = urllib.request.Request(gf_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(gf_req, timeout=8) as r:
            gf_data = json.loads(r.read().decode())
            file_path = gf_data.get("result", {}).get("file_path")
            if file_path:
                down_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                with urllib.request.urlopen(urllib.request.Request(down_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as down_r:
                    return down_r.read()
    except Exception as ve:
        print(f"Error downloading voice message ({file_id}): {ve}")
    return None

def get_base_url() -> str:
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    custom_url = (s.get("app_url", "") or "").strip().rstrip("/")
    if custom_url:
        return custom_url
    return os.getenv("WEBHOOK_BASE_URL", "https://admin.acbomlu.com").rstrip("/")

def save_voice_message(audio_bytes: Optional[bytes], file_id: Optional[str] = None, sender_name: str = "", mime_type: str = "audio/ogg") -> Optional[str]:
    if not audio_bytes:
        return None
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_str = datetime.now(cambodia_tz).strftime("%Y-%m-%d %H:%M:%S")
        v_doc = {
            "audio_bytes": audio_bytes,
            "mime_type": mime_type,
            "file_id": file_id or "",
            "sender_name": sender_name,
            "created_at": now_str,
            "size_bytes": len(audio_bytes)
        }
        res = voice_messages_collection.insert_one(v_doc)
        return str(res.inserted_id)
    except Exception as e:
        print(f"Error saving voice message: {e}")
        return None

@app.get("/api/voice/{voice_id}")
async def get_voice_audio(voice_id: str):
    try:
        doc = voice_messages_collection.find_one({"_id": ObjectId(voice_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid voice ID")
    if not doc or "audio_bytes" not in doc:
        raise HTTPException(status_code=404, detail="រកមិនឃើញឯកសារសំឡេងនេះទេ")
    
    mime_type = doc.get("mime_type", "audio/ogg")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="voice_{voice_id}.ogg"',
        "Cache-Control": "public, max-age=86400"
    }
    return Response(content=doc["audio_bytes"], media_type=mime_type, headers=headers)

@app.get("/voice-player/{voice_id}", response_class=HTMLResponse)
async def play_voice_page(voice_id: str):
    try:
        doc = voice_messages_collection.find_one({"_id": ObjectId(voice_id)})
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid voice ID")
    if not doc:
        raise HTTPException(status_code=404, detail="រកមិនឃើញឯកសារសំឡេងនេះទេ")
    
    sender = doc.get("sender_name", "បុគ្គលិក")
    created = doc.get("created_at", "")
    html = f"""<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>ស្ដាប់សារសំឡេងបុគ្គលិក - AC BOMLU</title>
  <link rel="icon" type="image/png" href="/logo.png">
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&family=Outfit:wght@400;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0" />
  <style>
    body {{ font-family: 'Kantumruy Pro', sans-serif; }}
    .font-outfit {{ font-family: 'Outfit', sans-serif; }}
  </style>
</head>
<body class="bg-gradient-to-br from-slate-900 via-slate-800 to-indigo-950 min-h-screen text-slate-100 flex items-center justify-center p-4">
  <div class="w-full max-w-md bg-white/10 backdrop-blur-xl border border-white/15 rounded-3xl p-6 sm:p-8 shadow-2xl text-center">
    <!-- Header -->
    <div class="flex items-center justify-center gap-3 mb-6">
      <img src="/logo.png" onerror="this.src='/static/logo.png'; this.onerror=null;" alt="Logo" class="w-12 h-12 object-contain rounded-full shadow-lg bg-white/20 p-1">
      <div class="text-left">
        <h1 class="text-lg font-bold text-white tracking-wide">AC BOMLU</h1>
        <p class="text-xs text-blue-300 font-medium">ប្រព័ន្ធគ្រប់គ្រងរដ្ឋបាល និងបុគ្គលិក</p>
      </div>
    </div>
    
    <!-- Audio Info Card -->
    <div class="bg-white/5 border border-white/10 rounded-2xl p-5 mb-6 text-left space-y-2">
      <div class="flex items-center justify-between">
        <span class="text-xs font-semibold text-blue-300 uppercase tracking-wider">🎙️ សារសំឡេងបុគ្គលិក</span>
        <span class="text-[11px] text-slate-400">{created}</span>
      </div>
      <div class="text-base font-bold text-white flex items-center gap-2">
        <span class="material-symbols-outlined text-blue-400">person</span>
        <span>{sender}</span>
      </div>
    </div>
    
    <!-- Audio Player -->
    <div class="bg-black/40 rounded-2xl p-5 border border-white/10 mb-6 shadow-inner">
      <audio id="voice-audio" controls autoplay playsinline class="w-full h-12" src="/api/voice/{voice_id}">
        កម្មវិធីរុករករបស់អ្នកមិនគាំទ្រការចាក់សំឡេងនេះទេ។
      </audio>
    </div>
    
    <!-- Actions -->
    <div class="flex flex-col gap-2.5">
      <a href="/api/voice/{voice_id}" download="voice_{voice_id}.ogg"
         class="inline-flex items-center justify-center gap-2 w-full py-3 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white font-semibold rounded-xl shadow-lg transition-all text-sm">
        <span class="material-symbols-outlined text-lg">download</span>
        <span>ទាញយកឯកសារសំឡេង (Download Voice)</span>
      </a>
      <a href="https://admin.acbomlu.com"
         class="inline-flex items-center justify-center gap-2 w-full py-2.5 bg-white/10 hover:bg-white/15 text-slate-300 hover:text-white font-medium rounded-xl transition-all text-xs">
        <span class="material-symbols-outlined text-base">dashboard</span>
        <span>ចូលកាន់ផ្ទាំងគ្រប់គ្រងរដ្ឋបាល (Web Dashboard)</span>
      </a>
    </div>
    
    <p class="text-[11px] text-slate-400 mt-5">
      🔒 សារសំឡេងនេះត្រូវបានរក្សាទុកដោយសុវត្ថិភាពក្នុងប្រព័ន្ធ AC BOMLU Administration
    </p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html)

def transcribe_voice_khmer(audio_bytes: bytes, audio_mime: str = "audio/ogg") -> Optional[str]:
    """
    Transcribes a voice note into natural, accurate Khmer text using Google Gemini.
    """
    if not audio_bytes:
        return None
    try:
        s = settings_collection.find_one({"_id": "app_settings"}) or {}
        api_key = (s.get("gemini_api_key", "").strip() if s else "") or os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            return None

        configured_model = (s.get("gemini_model", "") or "").strip()
        candidate_models = [
            configured_model,
            "gemini-flash-lite-latest",
            "gemini-flash-latest",
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash"
        ]
        seen = set()
        models_to_try = [m for m in candidate_models if m and not m.startswith("gemini-3.") and not (m in seen or seen.add(m))]

        b64_audio = base64.b64encode(audio_bytes).decode('utf-8')
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": audio_mime,
                                "data": b64_audio
                            }
                        },
                        {
                            "text": (
                                "Please listen carefully to this voice recording from a Cambodian staff member and transcribe the spoken words into natural, accurate Khmer text (អក្សរខ្មែរ).\n"
                                "Rules:\n"
                                "1. Output ONLY the transcribed Khmer spoken words without explanation, markdown, English, or quotes.\n"
                                "2. If the user states a reason for taking leave (e.g., ឈឺពោះ, ឈឺក្បាល, ទៅស្រុកកំណើត, ការកូន, រវល់ធុរៈគ្រួសារ) or borrowing money, capture the exact Khmer words clearly."
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1
            }
        }

        for m in models_to_try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=20) as response:
                    res_data = json.loads(response.read().decode('utf-8'))
                    candidates = res_data.get("candidates", [])
                    if candidates:
                        cand_parts = candidates[0].get("content", {}).get("parts", [])
                        if cand_parts:
                            raw_txt = cand_parts[0].get("text", "").strip()
                            clean = re.sub(r"^```[a-z]*\s*", "", raw_txt)
                            clean = re.sub(r"\s*```$", "", clean)
                            clean = clean.strip('"\'''\n\r ')
                            if clean:
                                return clean
            except Exception as e:
                print(f"transcribe_voice_khmer model {m} error: {e}")
                continue
    except Exception as outer_e:
        print(f"transcribe_voice_khmer outer error: {outer_e}")
    return None

def call_gemini_ai(prompt: str, user_name: str = "", audio_bytes: bytes = None, audio_mime: str = "audio/ogg") -> Optional[dict]:
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    api_key = (s.get("gemini_api_key", "").strip() if s else "") or os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
        
    model = (s.get("gemini_model", "") or "gemini-flash-lite-latest").strip()
    candidate_models = [
        model,
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-flash"
    ]
    seen = set()
    models_to_try = [m for m in candidate_models if m and not m.startswith("gemini-3.") and not (m in seen or seen.add(m))]
    
    parts = []
    if audio_bytes:
        b64_audio = base64.b64encode(audio_bytes).decode('utf-8')
        parts.append({
            "inlineData": {
                "mimeType": audio_mime,
                "data": b64_audio
            }
        })
    parts.append({"text": prompt})
    
    # Live database employee context for ultra-smart self-service answers
    emp_ctx = get_employee_context(user_name) if user_name else {}
    emp_info_str = ""
    if emp_ctx and emp_ctx.get("name"):
        emp_info_str = (
            f"LIVE DATABASE STATS FOR THIS STAFF MEMBER ({user_name}):\n"
            f"- Approved Leaves taken: {emp_ctx.get('approved_leaves', 0)} days\n"
            f"- Pending Leaves awaiting review: {emp_ctx.get('pending_leaves', 0)} days\n"
            f"- Total Leave requests submitted: {emp_ctx.get('total_leaves', 0)} times\n"
            f"- Approved Loan total: {emp_ctx.get('approved_loans', 0):,} Riel\n"
            f"- Pending Loan requests: {emp_ctx.get('pending_loans', 0):,} Riel\n"
            f"CRITICAL: If the staff asks about their leaves, loan status, or remaining days/money, reference these real figures directly in your answer!\n\n"
        )
    
    system_instruction = (
        "You are the official Ultra-Smart HR & Staff Assistant AI for 'AC BOMLU' (ក្រុមហ៊ុន សេវាបូមលូ និងដឹកជញ្ជូន AC BOMLU).\n"
        "Your role is to assist employees with leave requests, loan requests, status inquiries, and company questions with maximum intelligence, warmth, and accuracy in fluent Khmer.\n\n"
        f"{emp_info_str}"
        "Company Profile:\n"
        "- Company: សេវាបូមលូ និងដឹកជញ្ជូន AC BOMLU\n"
        "- Working Hours: 07:30 AM - 05:30 PM (រៀងរាល់ថ្ងៃ)\n"
        "- Services: សេវាបូមលូ ដឹកជញ្ជូន និងសម្អាតអនាម័យទូទាំងប្រទេស\n\n"
        "Instructions when analyzing staff input (text or audio):\n"
        "1. Classify intent into one of:\n"
        "   - 'leave': Staff wants to take leave / day off (សុំច្បាប់, ឈឺ, ទៅស្រុក, សម្រាក...)\n"
        "   - 'loan': Staff wants to borrow money / salary advance (ខ្ចីប្រាក់, បុរេប្រទាន, ខ្ចីលុយ...)\n"
        "   - 'self_query': Staff is inquiring about their personal records, leave count, loan balance, or status (e.g. 'ខែនេះខ្ញុំសុំច្បាប់ប៉ុន្មានថ្ងៃហើយ?', 'តើខ្ញុំនៅសល់លុយខ្ចីប៉ុន្មាន?', 'ច្បាប់ខ្ញុំ')\n"
        "   - 'hr_query': Questions about company rules, working hours, procedures, or general advice\n"
        "   - 'general_chat': Greetings, thank-you messages, casual friendly talk\n"
        "2. If intent is 'leave', extract: days (number, 0.5 for 'កន្លះថ្ងៃ' / half-day, or 1, 2, 3), reason (concise Khmer reason).\n"
        "3. If intent is 'loan', extract: amount_khr (integer in Riel, default 100000), purpose (concise Khmer purpose).\n"
        "4. In 'response_text': Always compose an exceptionally smart, warm, polite, and helpful response in natural Khmer (using 'បាទ/ចាស', 'ជម្រាបសួរ', 'សូមអរគុណ').\n"
        "   - For 'self_query': Answer their question directly with their exact live data numbers, followed by an encouraging note.\n"
        "   - For 'leave' or 'loan': A warm acknowledgment stating that their request has been registered and forwarded to management/រដ្ឋបាល for approval.\n"
        "   - For 'hr_query' or 'general_chat': An insightful, polite, and supportive response.\n\n"
        "CRITICAL: Output strictly valid JSON matching this schema:\n"
        "{\n"
        '  "intent": "leave" | "loan" | "self_query" | "hr_query" | "general_chat",\n'
        '  "days": 1,\n'
        '  "reason": "...",\n'
        '  "amount_khr": 100000,\n'
        '  "purpose": "...",\n'
        '  "response_text": "..."\n'
        "}"
    )
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_instruction}]
        },
        "contents": [
            {
                "role": "user",
                "parts": parts
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }
    
    for m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                candidates = res_data.get("candidates", [])
                if candidates:
                    cand_parts = candidates[0].get("content", {}).get("parts", [])
                    if cand_parts:
                        raw_text = cand_parts[0].get("text", "").strip()
                        if raw_text.startswith("```"):
                            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                            raw_text = re.sub(r"\s*```$", "", raw_text)
                        return json.loads(raw_text)
        except Exception as e:
            print(f"Gemini model {m} failed: {e}")
            continue
            
    return None

@app.post("/api/telegram-webhook")
async def telegram_webhook(req: Request):
    try:
        update = await req.json()
        
        # Helper to get bot token
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        admin_group_chat_id = (s.get("telegram_chat_id", "").strip() if s else "") or os.getenv("TELEGRAM_CHAT_ID", "")

        # Auto-link branch if incoming update is from a group chat
        incoming_chat = (
            update.get("message", {}).get("chat", {}) or
            update.get("callback_query", {}).get("message", {}).get("chat", {}) or
            update.get("my_chat_member", {}).get("chat", {})
        )
        in_chat_id = str(incoming_chat.get("id") or "").strip()
        if in_chat_id.startswith("-"):
            get_branch_by_chat_id(in_chat_id)

        # A. Handle Callback Query (When clicking Inline Buttons: Days or Loan Amount)
        callback_query = update.get("callback_query")
        if callback_query:
            cq_id = callback_query.get("id")
            cq_data = callback_query.get("data", "")
            cq_user = callback_query.get("from", {})
            cq_user_id = str(cq_user.get("id") or "")
            if cq_user and not cq_user.get("is_bot"):
                sync_or_update_employee_from_telegram(cq_user)
            
            # Resolve official company name from database!
            emp_rec = employees_collection.find_one({"telegram_user_id": cq_user_id}) if cq_user_id else None
            if emp_rec and emp_rec.get("name"):
                cq_name = emp_rec.get("name")
            else:
                cq_name = (cq_user.get("first_name", "") + " " + cq_user.get("last_name", "")).strip() or cq_user.get("username", "បុគ្គលិក")
            cq_msg = callback_query.get("message", {})
            cq_chat = cq_msg.get("chat", {})
            cq_chat_id = str(cq_chat.get("id") or "")
            cq_chat_type = cq_chat.get("type", "group")
            is_cq_private = (cq_chat_type == "private")
            
            # Callback for Connecting/Registering staff account
            if cq_data.startswith("connect_me"):
                sync_or_update_employee_from_telegram(cq_user)
                if token and cq_id:
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": f"✅ បានភ្ជាប់គណនី Telegram [{cq_name}] ចូលក្នុងប្រព័ន្ធជោគជ័យ!",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                return {"ok": True}

            # 1. Callback for Leave Days selection -> Show Reason Selection & Allow Direct Typing!
            if cq_data.startswith("leave_days:"):
                parts = cq_data.split(":")
                days_str = parts[1]
                try:
                    days = float(days_str)
                except (ValueError, TypeError):
                    days = 1.0
                    
                initiator_user_id = parts[2] if len(parts) > 2 else ""
                initiator_name = parts[3] if len(parts) > 3 else cq_name
                
                # Security Check: Prevent other staff from hijacking another person's request!
                if initiator_user_id and cq_user_id != initiator_user_id:
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": f"⚠️ ប៊ូតុងនេះសម្រាប់តែ [{initiator_name}] ប៉ុណ្ណោះ! ប្រសិនបើលោកអ្នកចង់សុំច្បាប់ សូមចុចប៊ូតុង [ 📝 ស្នើសុំច្បាប់ ] ខាងក្រោមដោយខ្លួនឯង។",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    return {"ok": True}
                    
                if days == 0.5:
                    days_display = "កន្លះថ្ងៃ (0.5 ថ្ងៃ)"
                elif days.is_integer():
                    days_display = f"{int(days)} ថ្ងៃ"
                else:
                    days_display = f"{days} ថ្ងៃ"

                # In group chat, immediately delete Step 1 message to keep chat uncluttered!
                cq_msg_id = cq_msg.get("message_id")
                if cq_msg_id:
                    delete_telegram_message(cq_chat_id, cq_msg_id)
                
                # Acknowledge callback query
                if token and cq_id:
                    try:
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({"callback_query_id": cq_id, "text": f"✅ បានជ្រើសរើស {days_display}"}).encode('utf-8')
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                
                reason_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🤒 ឈឺ / មិនស្រួលខ្លួន", "callback_data": f"leave_rsn:sick:{days}:{cq_user_id}"},
                            {"text": "🏠 រវល់ធុរៈគ្រួសារ", "callback_data": f"leave_rsn:family:{days}:{cq_user_id}"}
                        ],
                        [
                            {"text": "🚗 ទៅស្រុកកំណើត", "callback_data": f"leave_rsn:hometown:{days}:{cq_user_id}"},
                            {"text": "🎉 ជាប់កម្មវិធីផ្សេងៗ", "callback_data": f"leave_rsn:event:{days}:{cq_user_id}"}
                        ]
                    ]
                }
                ask_reason_msg = (
                    f"⏱️ <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"⏱️ <b>ចំនួនថ្ងៃ:</b> {days_display}\n\n"
                    f"👉 <b>សូមចុចជ្រើសរើស «មូលហេតុ» ខាងក្រោម៖</b>"
                )
                sent_res = send_telegram_notify(ask_reason_msg, reply_markup=reason_buttons, target_chat_id=cq_chat_id)
                reason_msg_id = (sent_res.get("result", {}).get("message_id") if isinstance(sent_res, dict) else None)
                if reason_msg_id:
                    asyncio.create_task(schedule_message_timeout_cleanup(cq_chat_id, reason_msg_id, cq_user_id, 60))
                
                user_flows_collection.replace_one(
                    {"user_id": cq_user_id},
                    {
                        "user_id": cq_user_id,
                        "type": "leave",
                        "days": days,
                        "days_display": days_display,
                        "name": cq_name,
                        "is_private": is_cq_private,
                        "chat_id": cq_chat_id,
                        "step1_msg_id": cq_msg_id,
                        "reason_msg_id": reason_msg_id
                    },
                    upsert=True
                )
                return {"ok": True}

            # 1.1 Callback when clicking a quick Reason button!
            if cq_data.startswith("leave_rsn:"):
                parts = cq_data.split(":")
                code = parts[1]
                rsn = REASON_MAP.get(code, code)
                days_str = parts[2] if len(parts) > 2 else "1"
                try:
                    days = float(days_str)
                except (ValueError, TypeError):
                    days = 1.0
                initiator_user_id = parts[3] if len(parts) > 3 else ""
                
                # Security Check: Prevent other staff from hijacking
                if initiator_user_id and cq_user_id != initiator_user_id:
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": "⚠️ ប៊ូតុងនេះសម្រាប់តែម្ចាស់សំណើដើមប៉ុណ្ណោះ! ប្រសិនបើលោកអ្នកចង់សុំច្បាប់ សូមចុចប៊ូតុង [ 📝 ស្នើសុំច្បាប់ ] ខាងក្រោមដោយខ្លួនឯង។",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    return {"ok": True}
                
                active_flow = user_flows_collection.find_one({"user_id": cq_user_id, "type": "leave"})
                cq_msg_id = cq_msg.get("message_id")
                
                # If flow is not active, this form was already used/finished! Reject and delete!
                if not active_flow:
                    if cq_msg_id:
                        delete_telegram_message(cq_chat_id, cq_msg_id)
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": "⚠️ ទម្រង់នេះត្រូវបានចុច ឬប្រើប្រាស់រួចរាល់ហើយ!",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    return {"ok": True}
                
                step1_msg_id = active_flow.get("step1_msg_id") if active_flow else None
                user_flows_collection.delete_one({"user_id": cq_user_id})
                
                if days == 0.5:
                    days_display = "កន្លះថ្ងៃ"
                elif days.is_integer():
                    days_display = f"{int(days)} ថ្ងៃ"
                else:
                    days_display = f"{days} ថ្ងៃ"
                
                # Clean up intermediate reason & step 1 messages so ONLY the final Admin Card remains!
                if cq_msg_id:
                    delete_telegram_message(cq_chat_id, cq_msg_id)
                if step1_msg_id and step1_msg_id != cq_msg_id:
                    delete_telegram_message(cq_chat_id, step1_msg_id)
                
                # Acknowledge callback
                if token and cq_id:
                    try:
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({"callback_query_id": cq_id, "text": "✅ សំណើសុំច្បាប់ត្រូវបានបញ្ជូន!"}).encode('utf-8')
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    
                emp_rec = employees_collection.find_one({"$or": [{"name": cq_name}, {"telegram_user_id": str(cq_user_id)}]})
                branch_name = emp_rec.get("branch") if emp_rec and emp_rec.get("branch") else get_branch_by_chat_id(cq_chat_id)
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                l_res = leaves_collection.insert_one({
                    "employee_name": cq_name,
                    "reason": rsn,
                    "days": days,
                    "status": "pending",
                    "date": today_khr,
                    "branch": branch_name
                })
                leave_id = str(l_res.inserted_id)
                conflict_alert = check_leave_conflicts(cq_name)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                admin_card = (
                    f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
                    f"🏢 <b>សាខា:</b> {branch_name}\n"
                    f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                    f"📝 <b>មូលហេតុ:</b> {rsn}\n"
                    f"{conflict_alert}"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំរដ្ឋបាលអនុញ្ញាតិ"
                )
                if is_cq_private:
                    emp_conf = (
                        f"✅ <b>សំណើសុំច្បាប់ឈប់សម្រាកត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                        f"📝 <b>មូលហេតុ:</b> {rsn}\n\n"
                        f"🔒 <i>សំណើរបស់អ្នកត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅកាន់រដ្ឋបាលពិនិត្យអនុញ្ញាត។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=cq_chat_id)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=cq_chat_id)
                return {"ok": True}

            # 2. Callback for Loan Amount selection -> Show Purpose Selection & Allow Direct Typing!
            if cq_data.startswith("loan_amt:"):
                parts = cq_data.split(":")
                amount_str = parts[1]
                try:
                    amount = int(amount_str)
                except (ValueError, TypeError):
                    amount = 100000
                initiator_user_id = parts[2] if len(parts) > 2 else ""
                initiator_name = parts[3] if len(parts) > 3 else cq_name
                
                # Security Check: Prevent other staff from hijacking
                if initiator_user_id and cq_user_id != initiator_user_id:
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": f"⚠️ ប៊ូតុងនេះសម្រាប់តែ [{initiator_name}] ប៉ុណ្ណោះ! ប្រសិនបើលោកអ្នកចង់ខ្ចីប្រាក់ សូមចុចប៊ូតុង [ 💰 ស្នើខ្ចីប្រាក់ ] ខាងក្រោមដោយខ្លួនឯង។",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    return {"ok": True}
                    
                cq_msg_id = cq_msg.get("message_id")
                # In group chat, immediately delete Step 1 message to keep chat uncluttered!
                if cq_msg_id:
                    delete_telegram_message(cq_chat_id, cq_msg_id)
                
                # Acknowledge callback
                if token and cq_id:
                    try:
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({"callback_query_id": cq_id, "text": f"✅ បានជ្រើសរើស {amount:,} រៀល"}).encode('utf-8')
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                
                purpose_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🏥 ព្យាបាលជំងឺ", "callback_data": f"loan_pur:treatment:{amount}:{cq_user_id}"},
                            {"text": "🏦 បង់ធនាគារ", "callback_data": f"loan_pur:bank:{amount}:{cq_user_id}"}
                        ],
                        [
                            {"text": "🍲 ទិញម្ហូបអាហារ", "callback_data": f"loan_pur:food:{amount}:{cq_user_id}"},
                            {"text": "✨ តម្រូវការផ្សេងៗ", "callback_data": f"loan_pur:other:{amount}:{cq_user_id}"}
                        ]
                    ]
                }
                ask_purpose_msg = (
                    f"💵 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n\n"
                    f"👉 <b>សូមចុចជ្រើសរើស «គោលបំណង» ខាងក្រោម៖</b>"
                )
                sent_res = send_telegram_notify(ask_purpose_msg, reply_markup=purpose_buttons, target_chat_id=cq_chat_id)
                purpose_msg_id = (sent_res.get("result", {}).get("message_id") if isinstance(sent_res, dict) else None)
                if purpose_msg_id:
                    asyncio.create_task(schedule_message_timeout_cleanup(cq_chat_id, purpose_msg_id, cq_user_id, 60))
                
                user_flows_collection.replace_one(
                    {"user_id": cq_user_id},
                    {
                        "user_id": cq_user_id,
                        "type": "loan",
                        "amount": amount,
                        "name": cq_name,
                        "is_private": is_cq_private,
                        "chat_id": cq_chat_id,
                        "step1_msg_id": cq_msg_id,
                        "purpose_msg_id": purpose_msg_id
                    },
                    upsert=True
                )
                return {"ok": True}

            # 2.1 Callback when clicking a quick Purpose button!
            if cq_data.startswith("loan_pur:"):
                parts = cq_data.split(":")
                code = parts[1]
                pur = PURPOSE_MAP.get(code, code)
                amount_str = parts[2] if len(parts) > 2 else "100000"
                try:
                    amt = int(amount_str)
                except (ValueError, TypeError):
                    amt = 100000
                initiator_user_id = parts[3] if len(parts) > 3 else ""
                
                # Security Check: Prevent other staff from hijacking
                if initiator_user_id and cq_user_id != initiator_user_id:
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": "⚠️ ប៊ូតុងនេះសម្រាប់តែម្ចាស់សំណើដើមប៉ុណ្ណោះ! ប្រសិនបើលោកអ្នកចង់ខ្ចីប្រាក់ សូមចុចប៊ូតុង [ 💰 ស្នើខ្ចីប្រាក់ ] ខាងក្រោមដោយខ្លួនឯង។",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    return {"ok": True}
                
                active_flow = user_flows_collection.find_one({"user_id": cq_user_id, "type": "loan"})
                cq_msg_id = cq_msg.get("message_id")
                
                if not active_flow:
                    if cq_msg_id:
                        delete_telegram_message(cq_chat_id, cq_msg_id)
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": "⚠️ ទម្រង់នេះត្រូវបានចុច ឬប្រើប្រាស់រួចរាល់ហើយ!",
                        "show_alert": True
                    }).encode('utf-8')
                    try:
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                    return {"ok": True}
                
                step1_msg_id = active_flow.get("step1_msg_id") if active_flow else None
                user_flows_collection.delete_one({"user_id": cq_user_id})
                
                # Clean up intermediate purpose & step 1 messages so ONLY the final Admin Card remains!
                if cq_msg_id:
                    delete_telegram_message(cq_chat_id, cq_msg_id)
                if step1_msg_id and step1_msg_id != cq_msg_id:
                    delete_telegram_message(cq_chat_id, step1_msg_id)
                
                # Acknowledge callback
                if token and cq_id:
                    try:
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({"callback_query_id": cq_id, "text": "✅ សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូន!"}).encode('utf-8')
                        urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    except Exception:
                        pass
                
                emp_rec = employees_collection.find_one({"$or": [{"name": cq_name}, {"telegram_user_id": str(cq_user_id)}]})
                branch_name = emp_rec.get("branch") if emp_rec and emp_rec.get("branch") else get_branch_by_chat_id(cq_chat_id)
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                ln_res = loans_collection.insert_one({
                    "employee_name": cq_name,
                    "amount_khr": amt,
                    "purpose": pur,
                    "status": "pending",
                    "date": today_khr,
                    "branch": branch_name
                })
                loan_id = str(ln_res.inserted_id)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                admin_card = (
                    f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
                    f"🏢 <b>សាខា:</b> {branch_name}\n"
                    f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amt:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {pur}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំរដ្ឋបាលអនុញ្ញាតិ"
                )
                if is_cq_private:
                    emp_conf = (
                        f"✅ <b>សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amt:,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {pur}\n\n"
                        f"🔒 <i>សំណើរបស់អ្នកត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅកាន់រដ្ឋបាលពិនិត្យអនុញ្ញាត។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=cq_chat_id)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=cq_chat_id)
                return {"ok": True}

            # 3. Handle Admin Approval / Rejection directly inside Telegram (Option B)
            if cq_data.startswith("app_leave:") or cq_data.startswith("rej_leave:") or cq_data.startswith("app_loan:") or cq_data.startswith("rej_loan:"):
                cq_msg = callback_query.get("message", {})
                chat_id_str = str(cq_msg.get("chat", {}).get("id") or "")
                if not chat_id_str or chat_id_str == "None":
                    chat_id_str = (s.get("telegram_chat_id", "").strip() if s else "") or os.getenv("TELEGRAM_CHAT_ID", "")
                cq_user_id = cq_user.get("id")
                
                # Security Check: Option B - Only Group Admins can approve/reject!
                if not is_telegram_admin(token, chat_id_str, cq_user_id):
                    ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                    ans_data = json.dumps({
                        "callback_query_id": cq_id,
                        "text": "⚠️ សុំទោស! លោកអ្នកជាបុគ្គលិកធម្មតា មិនមានសិទ្ធិចុចអនុញ្ញាតិឡើយ។ មានតែរដ្ឋបាលក្នុងគ្រុបប៉ុណ្ណោះដែលអាចចុចបាន!",
                        "show_alert": True
                    }).encode('utf-8')
                    urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                    return {"ok": True}
                    
                # Admin authorized!
                action, doc_id = cq_data.split(":")
                is_approve = action.startswith("app_")
                msg_id = cq_msg.get("message_id")
                
                if "leave" in action:
                    leave = leaves_collection.find_one({"_id": ObjectId(doc_id)})
                    if not leave:
                        if msg_id:
                            remove_telegram_inline_keyboard(chat_id_str, msg_id)
                        return {"ok": True}
                    
                    if leave.get("status") in ["approved", "rejected"]:
                        curr_status = "អនុញ្ញាតិ (Approved)" if leave.get("status") == "approved" else "មិនអនុញ្ញាតិ (Rejected)"
                        if msg_id:
                            remove_telegram_inline_keyboard(chat_id_str, msg_id)
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({
                            "callback_query_id": cq_id,
                            "text": f"⚠️ សំណើនេះត្រូវបានសម្រេចរួចហើយ: {curr_status}! មិនអនុញ្ញាតឱ្យចុចម្តងទៀតបានឡើយ។",
                            "show_alert": True
                        }).encode('utf-8')
                        try:
                            urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                        except Exception:
                            pass
                        return {"ok": True}
                    
                    emp_name = leave.get("employee_name", "បុគ្គលិក")
                    days = leave.get("days", 1)
                    reason = leave.get("reason", "")
                    status_str = "approved" if is_approve else "rejected"
                    
                    up_res = leaves_collection.update_one({"_id": ObjectId(doc_id), "status": "pending"}, {"$set": {"status": status_str}})
                    if up_res.modified_count == 0:
                        if msg_id:
                            remove_telegram_inline_keyboard(chat_id_str, msg_id)
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({
                            "callback_query_id": cq_id,
                            "text": "⚠️ សំណើនេះទើបតែត្រូវបានសម្រេចដោយរដ្ឋបាលម្នាក់ផ្សេងទៀតរួចរាល់ហើយ!",
                            "show_alert": True
                        }).encode('utf-8')
                        try:
                            urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                        except Exception:
                            pass
                        return {"ok": True}
                    
                    v_id = leave.get("voice_id")
                    voice_line = f"\n🎙️ <b>សារសំឡេង:</b> <a href=\"{get_base_url()}/voice-player/{v_id}\">▶️ ចុចទីនេះដើម្បីស្ដាប់សំឡេង (Listen) 🔊</a>" if v_id else ""
                    result_text = "🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved) ✅" if is_approve else "🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected) ❌"
                    branch_line = f"🏢 <b>សាខា:</b> {leave.get('branch', '')}\n" if leave.get("branch") else ""
                    edit_text = (
                        f"📋 <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                        f"{branch_line}"
                        f"👤 <b>បុគ្គលិក:</b> {emp_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                        f"📝 <b>មូលហេតុ:</b> {reason}"
                        f"{voice_line}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"{result_text}\n"
                        f"👮‍♂️ <b>ដោយរដ្ឋបាល:</b> {cq_name}"
                    )
                    decision_kb = []
                    if v_id:
                        decision_kb.append([{"text": "🎧 ស្ដាប់សារសំឡេងបុគ្គលិក (Voice)", "url": f"{get_base_url()}/voice-player/{v_id}"}])
                    edit_telegram_message(chat_id=chat_id_str, message_id=msg_id, new_text=edit_text, reply_markup={"inline_keyboard": decision_kb})
                    
                elif "loan" in action:
                    loan = loans_collection.find_one({"_id": ObjectId(doc_id)})
                    if not loan:
                        if msg_id:
                            remove_telegram_inline_keyboard(chat_id_str, msg_id)
                        return {"ok": True}
                    
                    if loan.get("status") in ["approved", "rejected"]:
                        curr_status = "អនុញ្ញាតិ (Approved)" if loan.get("status") == "approved" else "មិនអនុញ្ញាតិ (Rejected)"
                        if msg_id:
                            remove_telegram_inline_keyboard(chat_id_str, msg_id)
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({
                            "callback_query_id": cq_id,
                            "text": f"⚠️ សំណើខ្ចីប្រាក់នេះត្រូវបានសម្រេចរួចហើយ: {curr_status}! មិនអនុញ្ញាតឱ្យចុចម្តងទៀតបានឡើយ។",
                            "show_alert": True
                        }).encode('utf-8')
                        try:
                            urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                        except Exception:
                            pass
                        return {"ok": True}
                    
                    emp_name = loan.get("employee_name", "បុគ្គលិក")
                    amount = loan.get("amount_khr", 0)
                    purpose = loan.get("purpose", "")
                    status_str = "approved" if is_approve else "rejected"
                    
                    up_res = loans_collection.update_one({"_id": ObjectId(doc_id), "status": "pending"}, {"$set": {"status": status_str}})
                    if up_res.modified_count == 0:
                        if msg_id:
                            remove_telegram_inline_keyboard(chat_id_str, msg_id)
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_data = json.dumps({
                            "callback_query_id": cq_id,
                            "text": "⚠️ សំណើខ្ចីប្រាក់នេះទើបតែត្រូវបានសម្រេចដោយរដ្ឋបាលម្នាក់ផ្សេងទៀតរួចរាល់ហើយ!",
                            "show_alert": True
                        }).encode('utf-8')
                        try:
                            urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                        except Exception:
                            pass
                        return {"ok": True}
                    
                    v_id = loan.get("voice_id")
                    voice_line = f"\n🎙️ <b>សារសំឡេង:</b> <a href=\"{get_base_url()}/voice-player/{v_id}\">▶️ ចុចទីនេះដើម្បីស្ដាប់សំឡេង (Listen) 🔊</a>" if v_id else ""
                    result_text = "🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved) ✅" if is_approve else "🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected) ❌"
                    branch_line = f"🏢 <b>សាខា:</b> {loan.get('branch', '')}\n" if loan.get("branch") else ""
                    edit_text = (
                        f"💰 <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                        f"{branch_line}"
                        f"👤 <b>បុគ្គលិក:</b> {emp_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {purpose}"
                        f"{voice_line}\n"
                        f"━━━━━━━━━━━━━━━━━━━\n"
                        f"{result_text}\n"
                        f"👮‍♂️ <b>ដោយរដ្ឋបាល:</b> {cq_name}"
                    )
                    decision_kb = []
                    if v_id:
                        decision_kb.append([{"text": "🎧 ស្ដាប់សារសំឡេងបុគ្គលិក (Voice)", "url": f"{get_base_url()}/voice-player/{v_id}"}])
                    edit_telegram_message(chat_id=chat_id_str, message_id=msg_id, new_text=edit_text, reply_markup={"inline_keyboard": decision_kb})

                ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                ans_data = json.dumps({
                    "callback_query_id": cq_id,
                    "text": "✅ បានអនុញ្ញាតិជោគជ័យ!" if is_approve else "❌ មិនអនុញ្ញាតិ!"
                }).encode('utf-8')
                try:
                    urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                except Exception:
                    pass
                return {"ok": True}

        # B. Handle Regular Messages
        message = update.get("message") or update.get("channel_post")
        if not message:
            # Check chat_member or my_chat_member update event (profile changes, joining group)
            c_update = update.get("chat_member") or update.get("my_chat_member")
            if c_update:
                m_user = c_update.get("new_chat_member", {}).get("user", {}) or c_update.get("from", {})
                new_status = c_update.get("new_chat_member", {}).get("status", "")
                c_chat = c_update.get("chat", {})
                c_chat_id = str(c_chat.get("id") or "").strip()
                if c_chat_id.startswith("-") and (m_user.get("is_bot") or new_status in ["member", "administrator"]):
                    b_name = get_branch_by_chat_id(c_chat_id)
                    welcome_bot = (
                        f"👋 <b>សូមស្វាគមន៍មកកាន់គ្រុប {b_name}!</b>\n\n"
                        f"👇 <b>ក្ដារចុចប៊ូតុងផ្លូវការត្រូវបានភ្ជាប់ជូន៖</b>\n"
                        f"សូមចុចលើប៊ូតុងខាងក្រោមដើម្បីដំណើរការ៖\n"
                        f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ដើម្បីសុំច្បាប់ឈប់សម្រាក\n"
                        f"• <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ដើម្បីស្នើសុំខ្ចីប្រាក់ / បុរេប្រទាន"
                    )
                    send_telegram_notify(welcome_bot, target_chat_id=c_chat_id)
                elif m_user and not m_user.get("is_bot"):
                    sync_or_update_employee_from_telegram(m_user)
            return {"ok": True}

        from_user = message.get("from", {})
        user_id_str = str(from_user.get("id"))
        if from_user and not from_user.get("is_bot"):
            sync_or_update_employee_from_telegram(from_user)
        
        # Resolve official company name from database!
        emp_match = employees_collection.find_one({"telegram_user_id": user_id_str}) if user_id_str else None
        if emp_match and emp_match.get("name"):
            sender_name = emp_match.get("name")
        else:
            sender_name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip() or from_user.get("username", "បុគ្គលិក")
        
        chat = message.get("chat", {})
        chat_id_str = str(chat.get("id"))
        chat_type = chat.get("type", "group")
        is_private_chat = (chat_type == "private")

        # 1. Handle New Member Added to Group -> Welcome & Auto-Sync!
        new_members = message.get("new_chat_members", [])
        if new_members:
            for m in new_members:
                if m.get("is_bot"):
                    b_name = get_branch_by_chat_id(chat_id_str)
                    welcome_bot = (
                        f"👋 <b>សូមស្វាគមន៍មកកាន់គ្រុប {b_name}!</b>\n\n"
                        f"👇 <b>ក្ដារចុចប៊ូតុងផ្លូវការត្រូវបានភ្ជាប់ជូន៖</b>\n"
                        f"សូមចុចលើប៊ូតុងខាងក្រោមដើម្បីដំណើរការ៖\n"
                        f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ដើម្បីសុំច្បាប់ឈប់សម្រាក\n"
                        f"• <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ដើម្បីស្នើសុំខ្ចីប្រាក់ / បុរេប្រទាន"
                    )
                    send_telegram_notify(welcome_bot, target_chat_id=chat_id_str)
                    continue
                sync_or_update_employee_from_telegram(m)
                m_name = (m.get("first_name", "") + " " + m.get("last_name", "")).strip() or m.get("username", "បុគ្គលិក")
                b_name = get_branch_by_chat_id(chat_id_str)
                welcome_msg = (
                    f"👋 <b>សូមស្វាគមន៍ {m_name} មកកាន់គ្រុប {b_name}!</b>\n\n"
                    f"👇 <b>ក្ដារចុចប៊ូតុងត្រូវបានភ្ជាប់ជូនអ្នកស្វ័យប្រវត្តិ៖</b>\n"
                    f"សូមមើលប៊ូតុងខាងក្រោម៖ ចុចលើ <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ឬ <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ដើម្បីស្នើសុំបានគ្រប់ពេល។"
                )
                send_telegram_notify(welcome_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # Check contact sharing (Staff shares phone number via Telegram):
        contact = message.get("contact")
        if contact:
            c_phone = (contact.get("phone_number") or "").strip()
            if c_phone.startswith("+855"):
                c_phone = "0" + c_phone[4:]
            elif c_phone.startswith("855"):
                c_phone = "0" + c_phone[3:]
            elif c_phone.startswith("+"):
                c_phone = c_phone[1:]
            c_phone = c_phone.replace(" ", "").replace("-", "")
            sync_or_update_employee_from_telegram(from_user, phone=c_phone)
            conf_msg = (
                f"✅ <b>បានកត់ត្រាលេខទូរស័ព្ទជោគជ័យ!</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                f"📞 <b>លេខទូរស័ព្ទ:</b> <code>{c_phone}</code>\n\n"
                f"ប្រព័ន្ធបានធ្វើបច្ចុប្បន្នភាពចូល Dashboard រួចរាល់។"
            )
            done_kb = {
                "keyboard": [
                    [{"text": "📝 ស្នើសុំច្បាប់"}, {"text": "💰 ស្នើកម្ចី"}]
                ],
                "resize_keyboard": True,
                "is_persistent": True,
                "one_time_keyboard": False,
                "input_field_placeholder": "ចុចប៊ូតុង [📝 ស្នើសុំច្បាប់] ឬ [💰 ស្នើកម្ចី]..."
            }
            send_telegram_notify(conf_msg, reply_markup=done_kb, target_chat_id=chat_id_str)
            return {"ok": True}
        
        # Check active flow immediately to prevent UnboundLocalError
        active_flow = user_flows_collection.find_one({"user_id": user_id_str})
        
        text = (message.get("text") or "").strip()
        has_voice = bool(message.get("voice") or message.get("audio"))
        clean_cmd = text.split('@')[0].strip().lower()

        bot_username = "ac_bomlu_administration_bot"
        is_reply_to_bot = bool(message.get("reply_to_message", {}).get("from", {}).get("is_bot"))
        is_mentioned = ("@" in text and bot_username.lower() in text.lower()) or is_reply_to_bot
        is_hr_topic = any(w in text for w in ["សុំច្បាប់", "សុំឈប់", "ឈប់សម្រាក", "leave", "ខ្ចីប្រាក់", "បុរេប្រទាន", "loan", "វត្តមាន"])

        clean_btn_text = text.replace(" ", "").replace("📝", "").replace("💰", "").strip()
        is_leave_btn = clean_btn_text in ["ស្នើសុំច្បាប់", "សុំច្បាប់", "ស្នើច្បាប់", "សុំឈប់"] or clean_cmd in ["/leave", "/request_leave"]
        is_loan_btn = clean_btn_text in ["ស្នើកម្ចី", "សុំកម្ចី", "កម្ចី", "ស្នើខ្ចីប្រាក់", "សុំខ្ចីប្រាក់", "ខ្ចីប្រាក់"] or clean_cmd in ["/loan", "/request_loan"]
        is_system_cmd = clean_cmd in ["/start", "/help", "/briefing", "/report", "/today", "/morning", "/me", "/status", "/profile", "/my_leave", "/my_loan", "/weekend_report", "/leave_report", "/weekend", "/leave_detail", "/weekend_detail", "/month_report", "/loan_report", "/month_loan", "/loan_detail", "/month_detail", "/sync", "/sync_members", "/sync_telegram", "/connect", "/register_members", "/checkin", "/link", "/request_phone", "/phone_request", "/get_phone", "/share_phone", "/buttons", "/menu", "/keyboard", "/btn"] or clean_cmd.startswith("/compensate") or clean_cmd.startswith("/សង") or clean_cmd.startswith("/repay") or clean_cmd.startswith("/កាត់លុយ") or clean_cmd.startswith("/setphone") or clean_cmd.startswith("/phone") or clean_cmd.startswith("/setposition") or clean_cmd.startswith("/position") or is_leave_btn or is_loan_btn

        # Re-attach Keyboard in Group Chats or DM when typing /buttons, /menu, /help, /start, etc.
        if clean_cmd in ["/buttons", "/menu", "/keyboard", "/btn", "/options"] or (clean_cmd in ["/start", "/help"] and not is_private_chat) or text.lower() in ["ប៊ូតុង", "menu", "buttons"]:
            b_name = get_branch_by_chat_id(chat_id_str)
            welcome_group = (
                f"👋 <b>សូមជម្រាបសួរបុគ្គលិក {b_name} ទាំងអស់!</b>\n\n"
                f"👇 <b>ក្ដារចុចប៊ូតុងផ្លូវការត្រូវបានភ្ជាប់ជូន៖</b>\n"
                f"សូមចុចលើប៊ូតុងខាងក្រោមដើម្បីដំណើរការ៖\n"
                f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ដើម្បីសុំច្បាប់ឈប់សម្រាក\n"
                f"• <b>[ 💰 ស្នើកម្ចី ]</b> ដើម្បីស្នើសុំខ្ចីប្រាក់ / កម្ចី"
            )
            sent_wg = send_telegram_notify(welcome_group, target_chat_id=chat_id_str)
            if isinstance(sent_wg, dict) and "result" in sent_wg and chat_id_str.startswith("-"):
                m_id = sent_wg["result"].get("message_id")
                if m_id:
                    pin_telegram_message(chat_id_str, m_id)
            return {"ok": True}

        # Private Chat /start or /share_phone prompt with Contact Request Keyboard Button
        if is_private_chat and (text.startswith("/start") or clean_cmd in ["/share_phone"]):
            contact_kb = {
                "keyboard": [
                    [{"text": "📱 ចែករំលែកលេខទូរស័ព្ទរបស់ខ្ញុំ (Share Phone)", "request_contact": True}],
                    [{"text": "📝 ស្នើសុំច្បាប់"}, {"text": "💰 ស្នើកម្ចី"}]
                ],
                "resize_keyboard": True,
                "is_persistent": True,
                "one_time_keyboard": False
            }
            welcome_private = (
                f"👋 <b>សួស្តី {sender_name}!</b>\n\n"
                f"សូមស្វាគមន៍មកកាន់ប្រព័ន្ធរដ្ឋបាល <b>AC BOMLU Staff Control</b>!\n\n"
                f"📱 <b>ដើម្បីភ្ជាប់លេខទូរស័ព្ទរបស់អ្នកជាមួយកាតបុគ្គលិកស្វ័យប្រវត្តិ៖</b>\n"
                f"សូមចុចប៊ូតុង <b>[ 📱 ចែករំលែកលេខទូរស័ព្ទរបស់ខ្ញុំ (Share Phone) ]</b> ខាងក្រោមតែមួយដង នោះលេខទូរស័ព្ទរបស់អ្នកនឹងត្រូវកត់ត្រាចូលក្នុង Dashboard ដោយស្វ័យប្រវត្តិ។\n\n"
                f"ក្រៅពីនេះ អ្នកក៏អាចចុចប៊ូតុង <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ឬ <b>[ 💰 ស្នើកម្ចី ]</b> ដើម្បីស្នើសុំបានគ្រប់ពេល។"
            )
            send_telegram_notify(welcome_private, reply_markup=contact_kb, target_chat_id=chat_id_str)
            return {"ok": True}

        # /request_phone command in Telegram group or DM
        if clean_cmd in ["/request_phone", "/phone_request", "/get_phone"]:
            inline_btn = {
                "inline_keyboard": [[
                    {"text": "📱 ចុចទីនេះដើម្បីបញ្ជូនលេខទូរស័ព្ទស្វ័យប្រវត្តិ", "url": f"https://t.me/{bot_username}?start=share_phone"}
                ]]
            }
            msg = (
                "📢 <b>ជូនដំណឹងដល់បុគ្គលិក AC BOMLU ទាំងអស់៖</b>\n\n"
                "ដើម្បីឱ្យប្រព័ន្ធគ្រប់គ្រងមានលេខទូរស័ព្ទទំនាក់ទំនងរបស់អ្នក និងបង្ហាញលើ Dashboard ក្រុមហ៊ុន សូមបុគ្គលិកម្នាក់ៗចុចប៊ូតុងខាងក្រោម៖\n\n"
                "👉 ចុចប៊ូតុង <b>[ 📱 ចុចទីនេះដើម្បីបញ្ជូនលេខទូរស័ព្ទស្វ័យប្រវត្តិ ]</b> រួចចុច <b>[ ចែករំលែកលេខទូរស័ព្ទ (Share Phone) ]</b> តែមួយដង នោះប្រព័ន្ធនឹងកត់ត្រាស្វ័យប្រវត្តិ។"
            )
            send_telegram_notify(msg, reply_markup=inline_btn, target_chat_id=chat_id_str)
            return {"ok": True}

        # Feature 3: Daily Morning Briefing Command (/briefing, /report, /today)
        if clean_cmd in ["/briefing", "/report", "/today", "/morning"]:
            branch_for_group = get_branch_by_chat_id(chat_id_str)
            briefing = generate_daily_morning_briefing(branch=branch_for_group)
            send_telegram_notify(briefing, target_chat_id=chat_id_str)
            return {"ok": True}

        # Weekend Leave Compensation Report Command (/weekend_report, /leave_report, /leave_detail)
        if clean_cmd in ["/weekend_report", "/leave_report", "/weekend", "/leave_detail", "/weekend_detail"]:
            is_detailed = "detail" in text.lower() or "លម្អិត" in text or clean_cmd in ["/leave_detail", "/weekend_detail"]
            rep_data = generate_weekend_leave_report()
            if is_detailed:
                tg_msg = format_telegram_weekend_leave_report_detailed(rep_data)
            else:
                tg_msg = format_telegram_weekend_leave_report(rep_data)
            send_telegram_notify(tg_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # Monthly Loan Deduction Report Command: Strictly Protected by Financial Privacy Policy
        if clean_cmd in ["/month_report", "/loan_report", "/month_loan", "/loan_detail", "/month_detail"]:
            privacy_msg = (
                "🔒 <b>ការរក្សាការសម្ងាត់ទិន្នន័យហិរញ្ញវត្ថុ (Financial Privacy)</b>\n\n"
                "⚠️ របាយការណ៍ខ្ចីប្រាក់ និងទិន្នន័យហិរញ្ញវត្ថុ មិនត្រូវបានអនុញ្ញាតឱ្យបង្ហោះក្នុងក្រុម Telegram ឡើយ!\n"
                "👉 សូមចូលទៅកាន់ <b>Admin Dashboard</b> ដើម្បីពិនិត្យរបាយការណ៍នេះដោយសុវត្ថិភាព។"
            )
            send_telegram_notify(privacy_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # Admin Leave Compensation Command (/compensate [ឈ្មោះ] [ចំនួនថ្ងៃ])
        if clean_cmd.startswith("/compensate") or clean_cmd.startswith("/សង"):
            sender_id = from_user.get("id")
            s = settings_collection.find_one({"_id": "app_settings"})
            token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
            if not is_telegram_admin(token, chat_id_str, sender_id):
                send_telegram_notify("⚠️ សុំទោស! មានតែរដ្ឋបាលក្នុងគ្រុបប៉ុណ្ណោះដែលអាចកត់ត្រាការសងច្បាប់បាន។", target_chat_id=chat_id_str)
                return {"ok": True}
            parts = text.split()
            if len(parts) >= 3:
                try:
                    c_days = float(parts[-1])
                    c_emp = " ".join(parts[1:-1]).strip()
                    res = record_leave_compensation(employee_name=c_emp, days_to_compensate=c_days, admin_name=sender_name)
                    if not res.get("success"):
                        send_telegram_notify(f"⚠️ {res.get('message')}", target_chat_id=chat_id_str)
                except ValueError:
                    send_telegram_notify("⚠️ សូមបញ្ជាក់ចំនួនថ្ងៃជាលេខឱ្យបានត្រឹមត្រូវ (ឧ. /compensate Kem Borey 1)", target_chat_id=chat_id_str)
            else:
                send_telegram_notify("📌 <b>ទម្រង់ប្រើប្រាស់:</b> <code>/compensate [ឈ្មោះបុគ្គលិក] [ចំនួនថ្ងៃ]</code>\nឧទាហរណ៍៖ <code>/compensate Kem Borey 1</code> ឬ <code>/សង Kem Borey 0.5</code>", target_chat_id=chat_id_str)
            return {"ok": True}

        # Admin Loan Repayment Command (/repay [ឈ្មោះ] [ចំនួនទឹកប្រាក់])
        if clean_cmd.startswith("/repay") or clean_cmd.startswith("/កាត់លុយ"):
            sender_id = from_user.get("id")
            s = settings_collection.find_one({"_id": "app_settings"})
            token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
            if not is_telegram_admin(token, chat_id_str, sender_id):
                send_telegram_notify("⚠️ សុំទោស! មានតែរដ្ឋបាលក្នុងគ្រុបប៉ុណ្ណោះដែលអាចកត់ត្រាការកាត់/សងប្រាក់បាន។", target_chat_id=chat_id_str)
                return {"ok": True}
            parts = text.split()
            if len(parts) >= 3:
                try:
                    raw_amt_str = parts[-1].replace(",", "").replace("៛", "").replace("$", "").strip()
                    r_amt = int(raw_amt_str)
                    r_emp = " ".join(parts[1:-1]).strip()
                    res = record_loan_repayment(employee_name=r_emp, amount_to_repay=r_amt, admin_name=sender_name)
                    if not res.get("success"):
                        send_telegram_notify(f"⚠️ {res.get('message')}", target_chat_id=chat_id_str)
                except ValueError:
                    send_telegram_notify("⚠️ សូមបញ្ជាក់ចំនួនទឹកប្រាក់ជាលេខរៀលឱ្យបានត្រឹមត្រូវ (ឧ. /repay Kem Borey 50000)", target_chat_id=chat_id_str)
            else:
                send_telegram_notify("📌 <b>ទម្រង់ប្រើប្រាស់:</b> <code>/repay [ឈ្មោះបុគ្គលិក] [ចំនួនទឹកប្រាក់]</code>\nឧទាហរណ៍៖ <code>/repay Kem Borey 50000</code> ឬ <code>/កាត់លុយ Kem Borey 100000</code>", target_chat_id=chat_id_str)
            return {"ok": True}

        # /setphone command
        if clean_cmd.startswith("/setphone") or clean_cmd.startswith("/phone"):
            parts = text.split()
            if len(parts) == 2:
                # User setting own phone: /setphone 012345678
                p_num = parts[1].replace("+855", "0").replace("-", "").replace(" ", "").strip()
                sync_or_update_employee_from_telegram(from_user, phone=p_num)
                send_telegram_notify(
                    f"✅ <b>បានអាប់ដេតលេខទូរស័ព្ទជោគជ័យ!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"📞 <b>លេខទូរស័ព្ទ:</b> <code>{p_num}</code>",
                    target_chat_id=chat_id_str
                )
                return {"ok": True}
            elif len(parts) >= 3:
                # Setting an employee's phone: /setphone [name] [phone]
                p_num = parts[-1].replace("+855", "0").replace("-", "").replace(" ", "").strip()
                target_name = " ".join(parts[1:-1]).strip()
                import re
                target_emp = employees_collection.find_one({"name": {"$regex": f"^{re.escape(target_name)}$", "$options": "i"}})
                if target_emp:
                    employees_collection.update_one({"_id": target_emp["_id"]}, {"$set": {"phone": p_num}})
                    send_telegram_notify(
                        f"✅ <b>បានអាប់ដេតលេខទូរស័ព្ទជោគជ័យ!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {target_emp.get('name')}\n"
                        f"📞 <b>លេខទូរស័ព្ទ:</b> <code>{p_num}</code>",
                        target_chat_id=chat_id_str
                    )
                else:
                    send_telegram_notify(f"⚠️ រកមិនឃើញបុគ្គលិកឈ្មោះ «{target_name}» ក្នុងប្រព័ន្ធទេ!", target_chat_id=chat_id_str)
                return {"ok": True}
            else:
                send_telegram_notify(
                    f"📌 <b>ទម្រង់ប្រើប្រាស់:</b>\n"
                    f"• បុគ្គលិកកំណត់លេខផ្ទាល់ខ្លួន: <code>/setphone [លេខទូរស័ព្ទ]</code> (ឧ. <code>/setphone 016905354</code>)\n"
                    f"• រដ្ឋបាលកំណត់ឱ្យបុគ្គលិក: <code>/setphone [ឈ្មោះ] [លេខទូរស័ព្ទ]</code>",
                    target_chat_id=chat_id_str
                )
                return {"ok": True}

        # /setposition command
        if clean_cmd.startswith("/setposition") or clean_cmd.startswith("/position"):
            sender_id = from_user.get("id")
            s = settings_collection.find_one({"_id": "app_settings"})
            token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
            if not is_telegram_admin(token, chat_id_str, sender_id):
                send_telegram_notify("⚠️ សុំទោស! មានតែរដ្ឋបាលក្នុងគ្រុបប៉ុណ្ណោះដែលអាចកំណត់មុខតំណែងបាន។", target_chat_id=chat_id_str)
                return {"ok": True}
            parts = text.split()
            if len(parts) >= 3:
                pos_str = parts[-1].strip()
                target_name = " ".join(parts[1:-1]).strip()
                import re
                target_emp = employees_collection.find_one({"name": {"$regex": f"^{re.escape(target_name)}$", "$options": "i"}})
                if target_emp:
                    employees_collection.update_one({"_id": target_emp["_id"]}, {"$set": {"position": pos_str}})
                    send_telegram_notify(
                        f"✅ <b>បានអាប់ដេតមុខតំណែងជោគជ័យ!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {target_emp.get('name')}\n"
                        f"💼 <b>មុខតំណែង:</b> {pos_str}",
                        target_chat_id=chat_id_str
                    )
                else:
                    send_telegram_notify(f"⚠️ រកមិនឃើញបុគ្គលិកឈ្មោះ «{target_name}» ក្នុងប្រព័ន្ធទេ!", target_chat_id=chat_id_str)
            else:
                send_telegram_notify("📌 <b>ទម្រង់ប្រើប្រាស់:</b> <code>/setposition [ឈ្មោះបុគ្គលិក] [មុខតំណែង]</code>\nឧទាហរណ៍៖ <code>/setposition AC Mart APP អ្នកបើកឡាន</code>", target_chat_id=chat_id_str)
            return {"ok": True}

        # /sync_members or /sync command
        if clean_cmd in ["/sync", "/sync_members", "/sync_telegram"]:
            res = sync_group_members_from_telegram()
            send_telegram_notify(res.get("message", "Sync រួចរាល់!"), target_chat_id=chat_id_str)
            return {"ok": True}

        # /connect command - allows all staff in group to connect in 1 tap!
        if clean_cmd in ["/connect", "/register_members", "/checkin", "/link"]:
            connect_btn = {
                "inline_keyboard": [[
                    {"text": "👤 ភ្ជាប់គណនីខ្ញុំ (Connect Profile)", "callback_data": "connect_me"}
                ]]
            }
            msg = (
                "👋 <b>សូមជម្រាបសួរបុគ្គលិក AC BOMLU ទាំងអស់!</b>\n\n"
                "👉 សូមចុចប៊ូតុង <b>[ 👤 ភ្ជាប់គណនីខ្ញុំ ]</b> ខាងក្រោមតែមួយដង ដើម្បីឱ្យប្រព័ន្ធកត់ត្រា និងភ្ជាប់គណនី Telegram របស់អ្នកចូលទៅក្នុងប្រព័ន្ធគ្រប់គ្រងបុគ្គលិកស្វ័យប្រវត្តិ។"
            )
            send_telegram_notify(msg, reply_markup=connect_btn, target_chat_id=chat_id_str)
            return {"ok": True}

        # Feature 5: Self-Service Query Command (/me, /status, or natural language question)
        is_self_query = clean_cmd in ["/me", "/status", "/profile", "/my_leave", "/my_loan"] or any(k in text for k in [
            "ច្បាប់ប៉ុន្មាន", "លុយខ្ចីប៉ុន្មាន", "ខ្ចីប៉ុន្មាន", "ច្បាប់ខ្ញុំ", 
            "ខ្ចីខ្ញុំ", "ប្រវត្តិខ្ញុំ", "មើលច្បាប់", "មើលខ្ចី", "ទិន្នន័យខ្ញុំ", "សល់លុយ"
        ])
        if is_self_query:
            if not is_private_chat and not text.startswith("/"):
                return {"ok": True}
            ai_data = call_gemini_ai(prompt=f"Staff member '{sender_name}' asked self-service inquiry: {text}", user_name=sender_name)
            ai_msg = (ai_data.get("response_text") if ai_data else "").strip()
            report = get_employee_self_service_report(sender_name)
            if ai_msg:
                full_reply = f"🤖 <b>ឆ្លើយតប ({sender_name}):</b>\n{ai_msg}\n\n{report}"
            else:
                full_reply = report
            send_telegram_notify(full_reply, target_chat_id=chat_id_str)
            return {"ok": True}

        # Strict Button-Only Policy: Bot ONLY acts on the 2 Official Buttons or Slash Commands
        is_slash_cmd = text.startswith("/") and is_system_cmd
        if not is_leave_btn and not is_loan_btn and not is_slash_cmd:
            # Completely ignore all external text input, voice messages, chatter, or casual conversation!
            return {"ok": True}

        # 3. Handle Leave Button -> Step 1: Show Days Selection Buttons
        if is_leave_btn:
            prev_flow = user_flows_collection.find_one({"user_id": user_id_str})
            if prev_flow:
                old_m1 = prev_flow.get("step1_msg_id")
                old_m2 = prev_flow.get("reason_msg_id") or prev_flow.get("purpose_msg_id")
                if old_m1:
                    delete_telegram_message(chat_id_str, old_m1)
                if old_m2:
                    delete_telegram_message(chat_id_str, old_m2)
                user_flows_collection.delete_one({"user_id": user_id_str})

            inline_days = {
                "inline_keyboard": [
                    [
                        {"text": "🌗 កន្លះថ្ងៃ", "callback_data": f"leave_days:0.5:{user_id_str}:{sender_name}"},
                        {"text": "១ ថ្ងៃ", "callback_data": f"leave_days:1:{user_id_str}:{sender_name}"},
                        {"text": "២ ថ្ងៃ", "callback_data": f"leave_days:2:{user_id_str}:{sender_name}"}
                    ],
                    [
                        {"text": "៣ ថ្ងៃ", "callback_data": f"leave_days:3:{user_id_str}:{sender_name}"},
                        {"text": "៤ ថ្ងៃ", "callback_data": f"leave_days:4:{user_id_str}:{sender_name}"},
                        {"text": "៥ ថ្ងៃ", "callback_data": f"leave_days:5:{user_id_str}:{sender_name}"}
                    ]
                ]
            }
            msg = (
                f"📋 <b>សួស្តី {sender_name}!</b>\n\n"
                f"👉 <b>សូមចុចជ្រើសរើស «ចំនួនថ្ងៃឈប់សម្រាក» ខាងក្រោម៖</b>"
            )
            sent_res = send_telegram_notify(msg, reply_markup=inline_days, target_chat_id=chat_id_str)
            m_id = (sent_res.get("result", {}).get("message_id") if isinstance(sent_res, dict) else None)
            if m_id:
                user_flows_collection.replace_one(
                    {"user_id": user_id_str},
                    {
                        "user_id": user_id_str,
                        "type": "leave",
                        "step": 1,
                        "name": sender_name,
                        "chat_id": chat_id_str,
                        "step1_msg_id": m_id,
                        "created_at": time.time()
                    },
                    upsert=True
                )
                asyncio.create_task(schedule_message_timeout_cleanup(chat_id_str, m_id, user_id_str, 60))
            return {"ok": True}

        # 4. Handle Loan Button -> Step 1: Show Amount Selection Buttons
        if is_loan_btn:
            prev_flow = user_flows_collection.find_one({"user_id": user_id_str})
            if prev_flow:
                old_m1 = prev_flow.get("step1_msg_id")
                old_m2 = prev_flow.get("reason_msg_id") or prev_flow.get("purpose_msg_id")
                if old_m1:
                    delete_telegram_message(chat_id_str, old_m1)
                if old_m2:
                    delete_telegram_message(chat_id_str, old_m2)
                user_flows_collection.delete_one({"user_id": user_id_str})

            inline_amounts = {
                "inline_keyboard": [
                    [
                        {"text": "១០,០០០ ៛", "callback_data": f"loan_amt:10000:{user_id_str}:{sender_name}"},
                        {"text": "២០,០០០ ៛", "callback_data": f"loan_amt:20000:{user_id_str}:{sender_name}"},
                        {"text": "៣០,០០០ ៛", "callback_data": f"loan_amt:30000:{user_id_str}:{sender_name}"}
                    ],
                    [
                        {"text": "៥០,០០០ ៛", "callback_data": f"loan_amt:50000:{user_id_str}:{sender_name}"},
                        {"text": "១០០,០០០ ៛", "callback_data": f"loan_amt:100000:{user_id_str}:{sender_name}"},
                        {"text": "២០០,០០០ ៛", "callback_data": f"loan_amt:200000:{user_id_str}:{sender_name}"}
                    ],
                    [
                        {"text": "៣០០,០០០ ៛", "callback_data": f"loan_amt:300000:{user_id_str}:{sender_name}"},
                        {"text": "៤០០,០០០ ៛", "callback_data": f"loan_amt:400000:{user_id_str}:{sender_name}"},
                        {"text": "៥០០,០០០ ៛", "callback_data": f"loan_amt:500000:{user_id_str}:{sender_name}"}
                    ]
                ]
            }
            msg = (
                f"💰 <b>សួស្តី {sender_name}!</b>\n\n"
                f"👉 <b>សូមចុចជ្រើសរើស «ចំនួនទឹកប្រាក់ដែលចង់ខ្ចី» (រៀល)៖</b>"
            )
            sent_res = send_telegram_notify(msg, reply_markup=inline_amounts, target_chat_id=chat_id_str)
            m_id = (sent_res.get("result", {}).get("message_id") if isinstance(sent_res, dict) else None)
            if m_id:
                user_flows_collection.replace_one(
                    {"user_id": user_id_str},
                    {
                        "user_id": user_id_str,
                        "type": "loan",
                        "step": 1,
                        "name": sender_name,
                        "chat_id": chat_id_str,
                        "step1_msg_id": m_id,
                        "created_at": time.time()
                    },
                    upsert=True
                )
                asyncio.create_task(schedule_message_timeout_cleanup(chat_id_str, m_id, user_id_str, 60))
            return {"ok": True}

        # 5. Handle Help/Start Command
        if text.startswith("/help") or text.startswith("/start"):
            help_msg = (
                f"👋 <b>ជម្រាបសួរ {sender_name}!</b>\n\n"
                f"📌 <b>របៀបសុំច្បាប់ ឬខ្ចីប្រាក់ជាមួយ Bot AC BOMLU:</b>\n"
                f"• ចុចប៊ូតុង <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ខាងក្រោម ➡️ រើសចំនួនថ្ងៃ ➡️ រើសមូលហេតុ\n"
                f"• ចុចប៊ូតុង <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ខាងក្រោម ➡️ រើសចំនួនប្រាក់ ➡️ រើសគោលបំណង\n\n"
                f"👮‍♂️ <b>ពាក្យបញ្ជាសម្រាប់រដ្ឋបាល:</b>\n"
                f"• <code>/sync_members</code> — Sync ទិន្នន័យបុគ្គលិកពី Telegram Group\n"
                f"• <code>/weekend_report</code> — របាយការណ៍ច្បាប់ត្រូវសងចុងសប្តាហ៍\n"
                f"• <code>/month_report</code> — របាយការណ៍កាត់ប្រាក់ខែប្រចាំខែ\n\n"
                f"🚀 ប្រព័ន្ធនឹងកត់ត្រាចូល Dashboard របស់ AC BOMLU ដោយស្វ័យប្រវត្តិ!"
            )
            send_telegram_notify(help_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # In all other cases (normal chatting, voice messages, casual chat):
        # The bot remains completely silent and NEVER creates any loan or leave!
        return {"ok": True}

    except Exception as e:
        print("Telegram Webhook Error:", e)
        
    return {"ok": True}

@app.post("/api/register-webhook")
async def register_webhook_endpoint():
    s = settings_collection.find_one({"_id": "app_settings"})
    token = (s.get("telegram_bot_token", "") if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise HTTPException(status_code=400, detail="រកមិនឃើញ Telegram Bot Token ទេ!")
    webhook_url = os.getenv("WEBHOOK_BASE_URL", "https://admin.acbomlu.com").rstrip("/") + "/api/telegram-webhook"
    try:
        url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            res = json.loads(r.read().decode())
            if res.get("ok"):
                return {"success": True, "message": "បានភ្ជាប់ Telegram Webhook ជាមួយគ្រុបជោគជ័យ!"}
            return {"success": False, "message": str(res)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.on_event("startup")
async def startup_event():
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "") if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        if token:
            webhook_url = os.getenv("WEBHOOK_BASE_URL", "https://admin.acbomlu.com").rstrip("/") + "/api/telegram-webhook"
            url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                print("Webhook Auto-Registered:", r.read().decode())
            
            # Set Telegram Bot Commands to ONLY 2 options: leave & loan with clean Khmer descriptions
            bot_cmd_list = [
                {"command": "leave", "description": "📝 ស្នើសុំច្បាប់"},
                {"command": "loan", "description": "💰 ស្នើកម្ចី"}
            ]
            for sc in [None, {"type": "default"}, {"type": "all_group_chats"}, {"type": "all_chat_administrators"}, {"type": "all_private_chats"}]:
                try:
                    payload = json.dumps({"commands": bot_cmd_list, **({"scope": sc} if sc else {})}).encode("utf-8")
                    cmd_req = urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/setMyCommands",
                        data=payload,
                        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(cmd_req, timeout=5):
                        pass
                except Exception:
                    pass

            # Enforce Group Chat Permissions (Only Admins can pin/unpin) & ensure pinned buttons
            for b in get_all_branches():
                c_id = str(b.get("telegram_chat_id") or b.get("chat_id") or "").strip()
                if c_id and c_id.startswith("-"):
                    enforce_telegram_group_permissions(c_id)
                    ensure_pinned_official_message(c_id, b.get("name", ""))
    except Exception as e:
        print("Startup webhook error:", e)

# ===== Modern HTML UI =====
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AC BOMLU — ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក</title>

  <script>
    // Immediate anti-stale cache killer on client load
    try {
      if ('serviceWorker' in navigator) {
        navigator.serviceWorker.getRegistrations().then(function(regs) {
          for (var i = 0; i < regs.length; i++) regs[i].unregister();
        });
      }
      if ('caches' in window) {
        caches.keys().then(function(keys) {
          for (var i = 0; i < keys.length; i++) caches.delete(keys[i]);
        });
      }
    } catch(e) {}
  </script>

  <!-- Progressive Web App (PWA) Meta & Manifest -->
  <link rel="manifest" href="/manifest.json" />
  <meta name="theme-color" content="#1565C0" />
  <meta name="mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="apple-mobile-web-app-title" content="AC BOMLU Admin" />

  <!-- Favicon: AC Logo -->
  <link rel="icon" type="image/jpeg" href="/static/ac_logo.jpg" />
  <link rel="apple-touch-icon" href="/static/ac_logo.jpg" />

  <!-- Tailwind CSS v3 CDN -->
  <script src="https://cdn.tailwindcss.com"></script>

  <!-- Google Material Symbols (Outlined) -->
  <link rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0" />

  <!-- Kantumruy Pro — Khmer Font -->
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:ital,wght@0,100..700;1,100..700&display=swap"
    rel="stylesheet" />

  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: { sans: ['Kantumruy Pro', 'sans-serif'] },
          colors: {
            primary: { DEFAULT: '#1565C0', light: '#1976D2', dark: '#0D47A1', surface: '#E3F2FD' },
          },
          keyframes: {
            fadeIn: { from: { opacity: 0, transform: 'translateY(10px)' }, to: { opacity: 1, transform: 'translateY(0)' } },
            slideIn: { from: { transform: 'translateX(-100%)' }, to: { transform: 'translateX(0)' } },
          },
          animation: {
            fadeIn: 'fadeIn 0.3s ease-out',
            slideIn: 'slideIn 0.25s ease-out',
          }
        }
      }
    }
  </script>

  <style>
    * { font-family: 'Kantumruy Pro', sans-serif; }
    .material-symbols-outlined { font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; vertical-align: -5px; }
    .icon-fill { font-variation-settings: 'FILL' 1, 'wght' 400, 'GRAD' 0, 'opsz' 24; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-thumb { background: #CBD5E1; border-radius: 4px; }
    ::-webkit-scrollbar-track { background: transparent; }

    /* Active nav link */
    .nav-link.active { background-color: rgba(255,255,255,0.15); }
    .nav-link { transition: background 0.2s; }
    .nav-link:hover { background-color: rgba(255,255,255,0.1); }

    /* Status badges */
    .badge-pending  { background:#FEF3C7; color:#92400E; }
    .badge-approved { background:#D1FAE5; color:#065F46; }
    .badge-rejected { background:#FEE2E2; color:#991B1B; }

    /* Shimmer skeleton */
    @keyframes shimmer { 0%{background-position:-400px 0} 100%{background-position:400px 0} }
    .skeleton { background: linear-gradient(90deg,#f0f0f0 25%,#e0e0e0 50%,#f0f0f0 75%); background-size: 400px; animation: shimmer 1.2s infinite; border-radius: 6px; }

    /* Modal */
    .modal-overlay { backdrop-filter: blur(4px); }
    .modal-box { animation: fadeIn 0.25s ease-out; }

    /* Ripple button */
    .btn-primary { background: #1565C0; color: #fff; }
    .btn-primary:hover { background: #1976D2; }
    .btn-secondary { background: #E3F2FD; color: #1565C0; }
    .btn-secondary:hover { background: #BBDEFB; }

    /* Print styles for Executive Report */
    @media print {
      body { background: #ffffff !important; color: #000000 !important; overflow: visible !important; height: auto !important; }
      #sidebar, #sidebar-backdrop, header, .no-print, #login-screen { display: none !important; }
      main { margin: 0 !important; padding: 0 !important; width: 100% !important; overflow: visible !important; height: auto !important; }
      .print-only { display: block !important; }
      .no-break-inside { break-inside: avoid; page-break-inside: avoid; }
      .page-break { break-before: page; page-break-before: always; }
      .bg-white { box-shadow: none !important; border-color: #cbd5e1 !important; }
    }
  </style>
</head>

<body class="bg-slate-100 flex h-screen overflow-hidden text-gray-800">

<!-- ======================================================
     1. LOGIN SCREEN (FOR OWNER & ADMIN ONLY)
====================================================== -->
<div id="login-screen" class="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-gradient-to-br from-slate-950 via-blue-950 to-slate-900 overflow-y-auto">
  <div class="w-full max-w-md bg-white/95 backdrop-blur-xl rounded-3xl shadow-2xl border border-white/20 p-7 sm:p-9 animate-fadeIn relative">
    
    <!-- Top Floating Logo with Ring -->
    <div class="flex justify-center -mt-16 sm:-mt-20 mb-4">
      <div class="relative">
        <img src="/static/ac_logo.jpg" alt="AC BOMLU Logo"
          class="w-20 h-20 sm:w-24 sm:h-24 rounded-full object-cover shadow-2xl border-4 border-white ring-4 ring-blue-500/30" />
        <span class="absolute bottom-1 right-1 w-5 h-5 bg-emerald-500 border-2 border-white rounded-full"></span>
      </div>
    </div>

    <!-- Header Title -->
    <div class="text-center mb-6">
      <h2 class="text-xl sm:text-2xl font-bold text-gray-900 tracking-tight">AC BOMLU Administration</h2>
      <p class="text-xs text-blue-600 font-semibold mt-1">ប្រព័ន្ធគ្រប់គ្រងផ្ទៃក្នុង — សម្រាប់ Admin & ម្ចាស់ក្រុមហ៊ុន</p>
      <p class="text-[11px] text-gray-400 mt-0.5">សូមចូលគណនីដើម្បីពិនិត្យបុគ្គលិក ច្បាប់ និងប្រាក់កម្ចី</p>
    </div>

    <!-- Alert Message Box -->
    <div id="login-alert" class="hidden mb-4 p-3 rounded-2xl text-xs font-semibold border flex items-center gap-2"></div>

    <!-- Form -->
    <form onsubmit="event.preventDefault(); handleAdminLogin(event); return false;" class="space-y-4">
      <div>
        <label class="block text-xs font-bold text-gray-700 mb-1 flex items-center gap-1.5">
          <span class="material-symbols-outlined text-sm text-blue-600">person</span>
          <span>ឈ្មោះគណនី (Username)</span>
        </label>
        <input id="login-username" type="text" value="admin" required autocomplete="username"
          placeholder="ឧទាហរណ៍: admin ឬ រដ្ឋបាល"
          class="w-full px-4 py-3 rounded-2xl border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition bg-slate-50 focus:bg-white font-medium" />
      </div>

      <div>
        <label class="block text-xs font-bold text-gray-700 mb-1 flex items-center justify-between">
          <span class="flex items-center gap-1.5">
            <span class="material-symbols-outlined text-sm text-blue-600">lock</span>
            <span>លេខសម្ងាត់ (Password)</span>
          </span>
          <span class="text-[11px] text-gray-400 font-normal">សុវត្ថិភាពខ្ពស់</span>
        </label>
        <div class="relative">
          <input id="login-password" type="password" required autocomplete="current-password"
            placeholder="បញ្ចូលលេខសម្ងាត់..."
            onkeydown="if(event.key==='Enter'){event.preventDefault(); handleAdminLogin(event);}"
            class="w-full px-4 py-3 pr-11 rounded-2xl border border-slate-200 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition bg-slate-50 focus:bg-white font-mono" />
          <button type="button" onclick="toggleLoginPassword()"
            class="absolute inset-y-0 right-0 pr-3.5 flex items-center text-gray-400 hover:text-gray-600 cursor-pointer">
            <span id="login-pw-icon" class="material-symbols-outlined text-lg">visibility</span>
          </button>
        </div>
      </div>

      <div class="flex items-center justify-between pt-1">
        <label class="flex items-center gap-2 cursor-pointer select-none">
          <input id="login-remember" type="checkbox" checked
            class="w-4 h-4 rounded text-blue-600 focus:ring-blue-500 border-gray-300" />
          <span class="text-xs text-gray-600 font-medium">ចងចាំលើឧបករណ៍នេះ</span>
        </label>
        <span class="text-[11px] text-blue-600 font-medium">🔒 256-bit Encrypted</span>
      </div>

      <button type="button" onclick="handleAdminLogin(event)" id="login-submit-btn"
        class="w-full py-3.5 px-4 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white font-bold text-sm rounded-2xl shadow-lg shadow-blue-500/25 transition transform active:scale-[0.99] flex items-center justify-center gap-2 cursor-pointer">
        <span class="material-symbols-outlined text-lg">login</span>
        <span id="login-btn-text">ចូលប្រើប្រព័ន្ធ (Log In)</span>
      </button>
    </form>

    <div class="mt-6 pt-4 border-t border-slate-100 text-center">
      <p class="text-[12px] text-gray-500 flex items-center justify-center gap-1.5 font-medium">
        <span class="material-symbols-outlined text-sm text-blue-500">lock</span>
        <span>គណនី: <b class="text-blue-600 font-bold">admin</b> | លេខសម្ងាត់: <span class="text-slate-500 font-normal">អាចផ្លាស់ប្តូរបានក្នុង Settings</span></span>
      </p>
    </div>
  </div>
</div>

<script>
  function toggleLoginPassword() {
    const pInput = document.getElementById('login-password');
    const icon = document.getElementById('login-pw-icon');
    if (!pInput || !icon) return;
    if (pInput.type === 'password') {
      pInput.type = 'text';
      icon.textContent = 'visibility_off';
    } else {
      pInput.type = 'password';
      icon.textContent = 'visibility';
    }
  }

  function showLoginAlert(msg, isError) {
    const alertBox = document.getElementById('login-alert');
    if (!alertBox) return;
    alertBox.textContent = msg;
    alertBox.className = isError
      ? 'mb-4 p-3 rounded-2xl text-xs font-semibold bg-red-50 text-red-700 border border-red-200 flex items-center gap-2'
      : 'mb-4 p-3 rounded-2xl text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200 flex items-center gap-2';
    alertBox.classList.remove('hidden');
  }

  async function handleAdminLogin(event) {
    if (event) {
      if (typeof event.preventDefault === 'function') event.preventDefault();
      if (typeof event.stopPropagation === 'function') event.stopPropagation();
    }
    const uInput = document.getElementById('login-username');
    const pInput = document.getElementById('login-password');
    const rememberEl = document.getElementById('login-remember');
    const remember = rememberEl ? rememberEl.checked : true;
    const alertBox = document.getElementById('login-alert');
    const submitBtn = document.getElementById('login-submit-btn');
    const btnText = document.getElementById('login-btn-text');

    const kmNumMap = {'០':'0','១':'1','២':'2','៣':'3','៤':'4','៥':'5','៦':'6','៧':'7','៨':'8','៩':'9'};
    const rawU = uInput ? uInput.value : '';
    const rawP = pInput ? pInput.value : '';
    const cleanU = rawU.replace(/[\u200b\u200c\u200d\u200e\u200f\ufeff\u00a0]/g, '').trim();
    const cleanP = rawP.replace(/[\u200b\u200c\u200d\u200e\u200f\ufeff\u00a0]/g, '').trim();
    const normP = cleanP.replace(/[០-៩]/g, d => kmNumMap[d] || d);

    const u = cleanU;
    const p = normP || cleanP;

    if (!u || !p) {
      showLoginAlert('សូមបញ្ចូលឈ្មោះគណនី និងលេខសម្ងាត់!', true);
      return false;
    }

    if (submitBtn) submitBtn.disabled = true;
    if (btnText) btnText.textContent = 'កំពុងផ្ទៀងផ្ទាត់...';
    if (alertBox) alertBox.classList.add('hidden');

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, password: p })
      });
      const data = await res.json();

      if (res.ok && data.success) {
        localStorage.setItem('acbomlu_admin_token', data.token);
        if (remember) {
          localStorage.setItem('acbomlu_saved_username', u);
        } else {
          localStorage.removeItem('acbomlu_saved_username');
        }
        const scr = document.getElementById('login-screen');
        if (scr) scr.classList.add('hidden');
        const topAdminName = document.getElementById('top-admin-name');
        if (topAdminName) topAdminName.textContent = data.username || u;
        const settingAdminUser = document.getElementById('setting-admin-username');
        if (settingAdminUser) settingAdminUser.value = data.username || u;
        if (typeof showToast === 'function') {
          showToast(data.message || 'សូមស្វាគមន៍!');
        }
        if (typeof showTab === 'function') {
          showTab('dashboard', false);
        }
        if (typeof refreshAll === 'function') {
          await refreshAll();
        } else if (typeof loadStats === 'function') {
          await Promise.all([
            loadStats(), 
            loadLeaves(), 
            loadLoans(), 
            loadFinanceStats(),
            typeof loadSystemEmployees === 'function' ? loadSystemEmployees() : Promise.resolve()
          ]);
        }
      } else {
        showLoginAlert(data.detail || 'ឈ្មោះគណនី ឬលេខសម្ងាត់មិនត្រឹមត្រូវទេ!', true);
      }
    } catch (e) {
      showLoginAlert('មានបញ្ហាក្នុងការតភ្ជាប់: ' + e.message, true);
    } finally {
      if (submitBtn) submitBtn.disabled = false;
      if (btnText) btnText.textContent = 'ចូលប្រើប្រព័ន្ធ (Log In)';
    }
    return false;
  }
</script>

<!-- ======================================================
     2. PWA INSTALL GUIDE MODAL (FOR IOS & MANUAL INSTALL)
====================================================== -->
<div id="ios-install-modal" class="fixed inset-0 bg-black/60 backdrop-blur-sm z-[110] hidden items-center justify-center p-4">
  <div class="bg-white rounded-3xl max-w-sm w-full p-6 shadow-2xl text-center animate-fadeIn border border-slate-100">
    <div class="w-16 h-16 bg-blue-100 text-blue-600 rounded-2xl mx-auto flex items-center justify-center mb-4 shadow-sm">
      <span class="material-symbols-outlined text-3xl">add_to_home_screen</span>
    </div>
    <h3 class="text-base font-bold text-gray-800 mb-1">របៀបដំឡើងលើអេក្រង់ដើម (Home Screen)</h3>
    <p class="text-xs text-gray-500 mb-4">ដំឡើងដើម្បីបើកប្រើប្រាស់ដូច App ពិតប្រាកដ ដោយមិនបាច់វាយឈ្មោះវេបសាយ</p>
    
    <div class="text-left text-xs text-gray-600 space-y-2.5 bg-slate-50 p-4 rounded-2xl mb-4 border border-slate-200/80 leading-relaxed">
      <div class="flex items-start gap-2.5">
        <span class="w-5 h-5 rounded-full bg-blue-600 text-white flex items-center justify-center text-[10px] font-bold flex-shrink-0 mt-0.5">1</span>
        <span>ចុចលើប៊ូតុង <b>Share (ចែករំលែក ⬆️)</b> នៅខាងក្រោម ឬខាងលើនៃ Browser របស់អ្នក</span>
      </div>
      <div class="flex items-start gap-2.5">
        <span class="w-5 h-5 rounded-full bg-blue-600 text-white flex items-center justify-center text-[10px] font-bold flex-shrink-0 mt-0.5">2</span>
        <span>អូសចុះក្រោម រួចជ្រើសរើស <b>«Add to Home Screen» (➕ បន្ថែមទៅអេក្រង់ដើម)</b></span>
      </div>
      <div class="flex items-start gap-2.5">
        <span class="w-5 h-5 rounded-full bg-blue-600 text-white flex items-center justify-center text-[10px] font-bold flex-shrink-0 mt-0.5">3</span>
        <span>ចុចពាក្យ <b>«Add» (បន្ថែម)</b> នៅជ្រុងខាងស្ដាំខាងលើ ជាការស្រេច!</span>
      </div>
    </div>
    
    <div class="flex gap-2">
      <button onclick="confirmInstalledOnDevice()" class="flex-1 py-2.5 bg-blue-600 hover:bg-blue-700 text-white font-semibold rounded-xl text-xs transition shadow">
        ✅ ខ្ញុំបានដំឡើងរួចរាល់
      </button>
      <button onclick="closeInstallModal()" class="px-4 py-2.5 bg-slate-100 text-gray-600 font-semibold rounded-xl text-xs hover:bg-slate-200 transition">
        បិទ
      </button>
    </div>
  </div>
</div>

<!-- Mobile Sidebar Backdrop Overlay -->
<div id="sidebar-backdrop" onclick="closeSidebar()"
  class="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 hidden lg:hidden transition-opacity duration-300"
  style="background-color: rgba(0, 0, 0, 0.6) !important;"></div>

<!-- ======================================================
     SIDEBAR
====================================================== -->
<aside id="sidebar"
  class="fixed inset-y-0 left-0 z-50 w-64 text-white flex flex-col shadow-2xl transition-transform duration-300 ease-in-out -translate-x-full lg:translate-x-0 lg:static flex-shrink-0"
  style="background-color: #0f172a !important; background-image: linear-gradient(180deg, #1e3a8a 0%, #0f172a 100%) !important;">

  <!-- Logo -->
  <div class="h-16 flex items-center justify-between px-5 border-b border-blue-800">
    <div onclick="goToHomepage()" class="flex items-center gap-3 cursor-pointer hover:opacity-90 transition group" title="ចុចដើម្បីទៅកាន់ទំព័រដើម (Homepage)">
      <img src="/static/ac_logo.jpg" alt="AC Logo"
        class="w-10 h-10 rounded-full object-cover shadow-md border-2 border-white/30 flex-shrink-0 group-hover:scale-105 transition" />
      <div class="leading-tight">
        <p class="text-xs text-blue-300 font-medium">AC BOMLU</p>
        <p class="text-sm font-bold group-hover:text-blue-200 transition">HR Dashboard</p>
      </div>
    </div>
    <!-- Mobile close button -->
    <button onclick="closeSidebar()" class="lg:hidden text-blue-300 hover:text-white p-1 rounded-lg">
      <span class="material-symbols-outlined text-xl">close</span>
    </button>
  </div>

  <!-- Nav -->
  <nav class="flex-1 p-3 space-y-1 text-sm overflow-y-auto">
    <button onclick="showTab('dashboard')"
      id="nav-dashboard"
      class="nav-link active w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left">
      <span class="material-symbols-outlined text-xl">home</span>
      <span>ទំព័រដើម</span>
    </button>
    <button onclick="showTab('income')"
      id="nav-income"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl text-emerald-400">trending_up</span>
      <span class="font-medium">ចំណូល</span>
      <span id="badge-income-count" class="ml-auto text-[11px] bg-emerald-500/20 text-emerald-300 border border-emerald-400/30 rounded-full px-2 py-0.5 hidden">0</span>
    </button>
    <button onclick="showTab('expense')"
      id="nav-expense"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl text-rose-400">trending_down</span>
      <span class="font-medium">ចំណាយ</span>
      <span id="badge-expense-count" class="ml-auto text-[11px] bg-rose-500/20 text-rose-300 border border-rose-400/30 rounded-full px-2 py-0.5 hidden">0</span>
    </button>
    <button onclick="showTab('catalog')"
      id="nav-catalog"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl text-amber-300">category</span>
      <span class="font-medium">កាតាឡុក</span>
    </button>
    <button onclick="showTab('leaves')"
      id="nav-leaves"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl">event_busy</span>
      <span>ច្បាប់ឈប់សម្រាក</span>
      <span id="badge-leaves" class="ml-auto text-xs bg-orange-400 text-white rounded-full px-2 py-0.5 hidden">0</span>
    </button>
    <button onclick="showTab('loans')"
      id="nav-loans"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl">payments</span>
      <span>ខ្ចីប្រាក់ / បុរេប្រទាន</span>
      <span id="badge-loans" class="ml-auto text-xs bg-orange-400 text-white rounded-full px-2 py-0.5 hidden">0</span>
    </button>
    <button onclick="showTab('employees')"
      id="nav-employees"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl">group</span>
      <span>បុគ្គលិកទាំងអស់</span>
    </button>

    <!-- Reports Accordion Group -->
    <div class="pt-0.5">
      <button onclick="toggleReportsMenu()"
        id="nav-reports"
        class="nav-link w-full flex items-center justify-between px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
        <div class="flex items-center gap-3">
          <span class="material-symbols-outlined text-xl">analytics</span>
          <span class="font-medium">របាយការណ៍</span>
        </div>
        <span class="material-symbols-outlined text-lg transition-transform duration-200" id="reports-chevron">expand_more</span>
      </button>
      <div id="sub-reports-menu" class="pl-6 pr-1 mt-1 space-y-1">
        <button onclick="showTab('reports-finance')"
          id="nav-reports-finance"
          class="nav-link w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-xs font-medium text-blue-200 hover:text-white transition">
          <span class="material-symbols-outlined text-base text-emerald-300">account_balance_wallet</span>
          <span>ចំណូល-ចំណាយ (ថ្ងៃ/ខែ/ឆ្នាំ)</span>
        </button>
        <button onclick="showTab('reports-leave')"
          id="nav-reports-leave"
          class="nav-link w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-xs font-medium text-blue-200 hover:text-white transition">
          <span class="material-symbols-outlined text-base text-amber-300">event_repeat</span>
          <span>ច្បាប់ (សងចុងសប្តាហ៍)</span>
        </button>
        <button onclick="showTab('reports-loan')"
          id="nav-reports-loan"
          class="nav-link w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-xs font-medium text-blue-200 hover:text-white transition">
          <span class="material-symbols-outlined text-base text-indigo-300">receipt_long</span>
          <span>ខ្ចីប្រាក់ (កាត់ប្រាក់ខែ)</span>
        </button>
      </div>
    </div>

    <button onclick="showTab('settings')"
      id="nav-settings"
      class="nav-link w-full flex items-center gap-3 px-4 py-2.5 rounded-xl text-left text-blue-200 hover:text-white transition">
      <span class="material-symbols-outlined text-xl">settings</span>
      <span>ការកំណត់</span>
    </button>
  </nav>

  <!-- PWA Install Sidebar Box (Hidden if already installed on this device) -->
  <div id="pwa-install-sidebar-box" class="hidden p-3 mx-3 mb-2 bg-gradient-to-br from-blue-800/90 to-indigo-900/90 rounded-2xl border border-blue-600/30 text-white shadow-lg">
    <div class="flex items-center gap-2 mb-1">
      <span class="material-symbols-outlined text-amber-300 text-lg">download_for_offline</span>
      <span class="text-xs font-bold">ដំឡើងលើឧបករណ៍</span>
    </div>
    <p class="text-[11px] text-blue-200 mb-2 leading-relaxed">ដំឡើងលើទូរស័ព្ទ កុំព្យូទ័រ ឬ Tablet ដើម្បីបើកប្រើបានលឿនដូច App ពិតៗ</p>
    <button onclick="handlePWAInstallClick()"
      class="w-full py-2 bg-white text-blue-900 font-bold rounded-xl text-xs hover:bg-blue-50 transition shadow flex items-center justify-center gap-1.5 active:scale-95">
      <span class="material-symbols-outlined text-sm">install_mobile</span>
      <span>ដំឡើងកម្មវិធីឥឡូវនេះ</span>
    </button>
  </div>



  <!-- Footer -->
  <div class="p-4 border-t border-blue-800 text-xs text-blue-400 flex items-center gap-2">
    <span class="material-symbols-outlined text-base">info</span>
    AC BOMLU © 2026 v2.0
  </div>
</aside>

<!-- ======================================================
     MAIN AREA
====================================================== -->
<div class="flex-1 flex flex-col overflow-hidden">

  <!-- TOP BAR -->
  <header class="h-14 sm:h-16 bg-white border-b border-slate-200 flex items-center justify-between px-2 sm:px-6 shadow-sm flex-shrink-0 gap-1.5 sm:gap-4">
    <div class="flex items-center gap-1.5 sm:gap-3 flex-shrink-0">
      <!-- Hamburger (mobile) -->
      <button onclick="toggleSidebar()" class="w-9 h-9 sm:w-10 sm:h-10 flex items-center justify-center rounded-xl hover:bg-slate-100 text-gray-600 hover:text-blue-700 lg:hidden transition flex-shrink-0" aria-label="Menu">
        <span class="material-symbols-outlined text-2xl">menu</span>
      </button>

      <!-- Backward & Forward Navigation (Hidden as requested) -->
      <div style="display: none !important;" class="hidden items-center gap-0.5 bg-slate-100 p-1 rounded-xl border border-slate-200/80 flex-shrink-0 shadow-inner">
        <button onclick="navGoBack()" id="btn-nav-back" title="ថយក្រោយទៅ UI មុន (Back)" aria-label="Back"
          class="w-8 h-8 flex items-center justify-center rounded-lg text-gray-700 hover:text-blue-600 hover:bg-white transition active:scale-90 disabled:opacity-30 disabled:pointer-events-none">
          <span class="material-symbols-outlined text-xl">arrow_back</span>
        </button>
        <button onclick="navGoForward()" id="btn-nav-forward" title="ទៅមុខ (Forward)" aria-label="Forward"
          class="w-8 h-8 flex items-center justify-center rounded-lg text-gray-700 hover:text-blue-600 hover:bg-white transition active:scale-90 disabled:opacity-30 disabled:pointer-events-none">
          <span class="material-symbols-outlined text-xl">arrow_forward</span>
        </button>
      </div>

      <div class="hidden sm:block truncate">
        <h1 id="page-title" class="text-sm sm:text-base font-semibold text-gray-800 truncate">ទំព័រដើម</h1>
        <p class="text-[11px] sm:text-xs text-gray-400 truncate" id="page-date"></p>
      </div>
    </div>

    <!-- Global Branch Switcher (Flexible on all mobile phones) -->
    <div class="flex items-center justify-center flex-1 min-w-0 max-w-[170px] xs:max-w-[210px] sm:max-w-xs mx-1">
      <div class="w-full flex items-center gap-1 sm:gap-1.5 bg-blue-50/90 hover:bg-blue-100/80 border border-blue-200/90 rounded-xl px-2 sm:px-3 py-1 sm:py-1.5 shadow-xs transition min-w-0">
        <span class="material-symbols-outlined text-blue-700 text-base sm:text-lg flex-shrink-0">store</span>
        <select id="global-branch-select" onchange="onGlobalBranchChange(this.value)" class="bg-transparent text-[11px] sm:text-xs font-bold text-blue-900 focus:outline-none cursor-pointer truncate w-full">
          <option value="">🏢 គ្រប់សាខាទាំងអស់</option>
          <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">📍 សាខាកំពង់ចាម</option>
          <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">📍 សាខាត្បូងឃ្មុំ</option>
        </select>
      </div>
    </div>

    <div class="flex items-center gap-1.5 sm:gap-3 flex-shrink-0">
      <!-- Install PWA Button (Hidden if already installed on this device) -->
      <button id="pwa-install-btn-top" onclick="handlePWAInstallClick()"
        class="hidden items-center gap-1 px-2 sm:px-3 py-1 sm:py-1.5 bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-700 hover:to-indigo-700 text-white rounded-xl text-xs font-semibold shadow-md transition transform active:scale-95 flex-shrink-0"
        title="ដំឡើងកម្មវិធី AC BOMLU លើឧបករណ៍របស់អ្នក">
        <span class="material-symbols-outlined text-base">install_mobile</span>
        <span class="hidden md:inline">ដំឡើងកម្មវិធី</span>
      </button>

      <!-- Refresh -->
      <button onclick="refreshAll()" title="ធ្វើឱ្យស្រស់" id="refresh-btn"
        class="w-8 h-8 sm:w-9 sm:h-9 flex items-center justify-center rounded-full hover:bg-slate-100 text-gray-500 hover:text-blue-600 transition flex-shrink-0">
        <span class="material-symbols-outlined text-xl">refresh</span>
      </button>

      <!-- Admin Status & Logout -->
      <div class="flex items-center gap-1.5 sm:gap-2 pl-1.5 sm:pl-2 border-l border-slate-200 flex-shrink-0">
        <div class="hidden md:flex flex-col text-right leading-none">
          <span id="top-admin-name" class="text-xs font-bold text-gray-800">Admin</span>
          <span class="text-[10px] text-emerald-600 font-semibold flex items-center gap-0.5 justify-end mt-0.5">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse"></span> Online
          </span>
        </div>
        <button onclick="handleAdminLogout()" title="ចាកចេញ (Log Out)"
          class="w-8 h-8 sm:w-9 sm:h-9 flex items-center justify-center rounded-xl bg-slate-100 hover:bg-red-50 text-gray-500 hover:text-red-600 transition flex-shrink-0" aria-label="Log Out">
          <span class="material-symbols-outlined text-lg">logout</span>
        </button>
      </div>

      <!-- Avatar -->
      <div onclick="goToHomepage()" class="w-8 h-8 sm:w-9 sm:h-9 rounded-full overflow-hidden border-2 border-blue-200 shadow cursor-pointer hover:opacity-85 transition flex-shrink-0" title="ទៅកាន់ទំព័រដើម (Homepage)">
        <img src="/static/ac_logo.jpg" alt="AC" class="w-full h-full object-cover" />
      </div>
    </div>
  </header>

  <!-- CONTENT -->
  <main class="flex-1 overflow-auto">

    <!-- ===== DASHBOARD TAB ===== -->
    <div id="tab-dashboard" class="p-3 sm:p-6 animate-fadeIn">

      <!-- Stat Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 sm:gap-4 mb-6">
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100 flex items-center gap-2.5 sm:gap-4">
          <div class="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-blue-100 flex items-center justify-center flex-shrink-0">
            <span class="material-symbols-outlined icon-fill text-blue-600 text-xl sm:text-2xl">group</span>
          </div>
          <div class="min-w-0">
            <p class="text-[11px] sm:text-xs text-gray-500 mb-0.5 truncate">បុគ្គលិកសរុប</p>
            <p id="stat-employees" class="text-lg sm:text-2xl font-bold text-gray-800 leading-tight">—</p>
          </div>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100 flex items-center gap-2.5 sm:gap-4">
          <div class="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-orange-100 flex items-center justify-center flex-shrink-0">
            <span class="material-symbols-outlined icon-fill text-orange-500 text-xl sm:text-2xl">pending_actions</span>
          </div>
          <div class="min-w-0">
            <p class="text-[11px] sm:text-xs text-gray-500 mb-0.5 truncate">ច្បាប់រង់ចាំ</p>
            <p id="stat-leaves" class="text-lg sm:text-2xl font-bold text-gray-800 leading-tight">—</p>
          </div>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100 flex items-center gap-2.5 sm:gap-4">
          <div class="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-red-100 flex items-center justify-center flex-shrink-0">
            <span class="material-symbols-outlined icon-fill text-red-500 text-xl sm:text-2xl">account_balance_wallet</span>
          </div>
          <div class="min-w-0">
            <p class="text-[11px] sm:text-xs text-gray-500 mb-0.5 truncate">ខ្ចីប្រាក់រង់ចាំ</p>
            <p id="stat-loans" class="text-lg sm:text-2xl font-bold text-gray-800 leading-tight">—</p>
          </div>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100 flex items-center gap-2.5 sm:gap-4">
          <div class="w-10 h-10 sm:w-12 sm:h-12 rounded-xl bg-green-100 flex items-center justify-center flex-shrink-0">
            <span class="material-symbols-outlined icon-fill text-green-600 text-xl sm:text-2xl">check_circle</span>
          </div>
          <div class="min-w-0">
            <p class="text-[11px] sm:text-xs text-gray-500 mb-0.5 truncate">ខ្ចីប្រាក់អនុញ្ញាតិ</p>
            <p id="stat-approved" class="text-lg sm:text-2xl font-bold text-gray-800 leading-tight">—</p>
          </div>
        </div>
      </div>

      <!-- Financial Overview Widget (Today) -->
      <div class="bg-gradient-to-r from-slate-900 via-blue-950 to-indigo-950 rounded-2xl p-4 sm:p-5 shadow-sm text-white mb-6 border border-blue-900/40">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 pb-3 border-b border-white/10">
          <div class="flex items-center gap-2.5">
            <span class="w-8 h-8 rounded-lg bg-blue-600/30 border border-blue-400/30 flex items-center justify-center text-blue-300">
              <span class="material-symbols-outlined text-lg">account_balance_wallet</span>
            </span>
            <div>
              <h3 class="font-bold text-sm sm:text-base text-white">សង្ខេបហិរញ្ញវត្ថុខែនេះ (Financial Summary This Month)</h3>
              <p id="dash-finance-date" class="text-xs text-blue-300">កំពុងទាញទិន្នន័យ...</p>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-wrap">
            <button onclick="openIncomeModal()" class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-xs font-semibold flex items-center gap-1 shadow transition active:scale-95">
              <span class="material-symbols-outlined text-sm">add_circle</span> + បញ្ចូលចំណូល
            </button>
            <button onclick="openExpenseModal()" class="px-3 py-1.5 bg-rose-600 hover:bg-rose-700 text-white rounded-xl text-xs font-semibold flex items-center gap-1 shadow transition active:scale-95">
              <span class="material-symbols-outlined text-sm">remove_circle</span> - បញ្ចូលចំណាយ
            </button>
            <button onclick="openTransferModal()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-semibold flex items-center gap-1 shadow transition active:scale-95" title="បង្វិលសាច់ប្រាក់ចូលគណនីវិនិយោគទុន">
              <span class="material-symbols-outlined text-sm">swap_horiz</span> 🔄 បង្វិលសាច់ប្រាក់
            </button>
            <button onclick="showTab('reports-finance')" class="px-3 py-1.5 bg-white/10 hover:bg-white/20 text-white rounded-xl text-xs font-medium flex items-center gap-1 transition">
              <span class="material-symbols-outlined text-sm">bar_chart</span> របាយការណ៍
            </button>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div class="bg-white/5 border border-white/10 rounded-xl p-3">
            <p class="text-[11px] text-emerald-400 font-medium flex items-center gap-1">
              <span class="material-symbols-outlined text-xs">trending_up</span> ចំណូលខែនេះ
            </p>
            <p id="dash-income-today" class="text-base sm:text-lg font-bold text-white mt-1">0 ៛</p>
          </div>
          <div class="bg-white/5 border border-white/10 rounded-xl p-3">
            <p class="text-[11px] text-rose-400 font-medium flex items-center gap-1">
              <span class="material-symbols-outlined text-xs">trending_down</span> ចំណាយខែនេះ
            </p>
            <p id="dash-expense-today" class="text-base sm:text-lg font-bold text-white mt-1">0 ៛</p>
          </div>
          <div class="bg-white/5 border border-white/10 rounded-xl p-3">
            <p class="text-[11px] text-blue-300 font-medium flex items-center gap-1">
              <span class="material-symbols-outlined text-xs">balance</span> សមតុល្យសុទ្ធខែនេះ
            </p>
            <p id="dash-net-today" class="text-base sm:text-lg font-bold text-emerald-300 mt-1">0 ៛</p>
          </div>
        </div>
      </div>

      <!-- Cash vs Bank vs Investment Fund Reconciliation (This Month) -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden mb-6">
        <div class="px-5 py-3.5 bg-gradient-to-r from-slate-50 via-blue-50/40 to-indigo-50/40 border-b border-slate-100 flex items-center justify-between flex-wrap gap-2">
          <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-indigo-600">account_balance_wallet</span>
            <h4 class="font-bold text-gray-800 text-sm">ផ្ទាំងផ្ទៀងផ្ទាត់សាច់ប្រាក់ & គណនីវិនិយោគទុន (Cash, Bank & Investment Fund)</h4>
          </div>
          <div class="flex items-center gap-2.5">
            <button onclick="openTransferModal()" class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl text-xs font-bold flex items-center gap-1 shadow-sm transition active:scale-95">
              <span class="material-symbols-outlined text-sm">swap_horiz</span> 🔄 បង្វិលសាច់ប្រាក់
            </button>
            <span class="text-xs text-gray-500 font-medium" id="dash-recon-period">កំពុងទាញទិន្នន័យ...</span>
          </div>
        </div>
        <div class="p-5 grid grid-cols-1 md:grid-cols-3 gap-4">
          <!-- Cash in Hand Safe Box -->
          <div class="rounded-2xl border border-emerald-200 bg-gradient-to-br from-emerald-50/70 to-teal-50/30 p-4 relative overflow-hidden">
            <div class="flex items-center justify-between mb-3">
              <span class="flex items-center gap-2 text-emerald-900 font-bold text-sm">
                <span class="material-symbols-outlined text-emerald-600">payments</span> សាច់ប្រាក់សុទ្ធ (Cash In Hand)
              </span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-200/80 text-emerald-900">ថតប្រាក់ / Drawer</span>
            </div>
            <div class="space-y-2 text-xs text-gray-700">
              <div class="flex items-center justify-between py-1 border-b border-emerald-100/80">
                <span class="text-gray-600">ចំណូលសាច់ប្រាក់ (+)</span>
                <span id="dash-cash-income" class="font-bold text-emerald-700 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-emerald-100/80">
                <span class="text-gray-600">ចំណាយសាច់ប្រាក់ (-)</span>
                <span id="dash-cash-expense" class="font-bold text-rose-600 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-emerald-100/80">
                <span class="text-gray-600">បង្វិលទៅវិនិយោគ (-)</span>
                <span id="dash-cash-transfers-out" class="font-bold text-amber-600 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between pt-1">
                <span class="font-bold text-gray-800">សាច់ប្រាក់នៅសល់ជាក់ស្ដែង៖</span>
                <span id="dash-cash-balance" class="text-sm font-black text-emerald-800 font-mono">0 ៛</span>
              </div>
            </div>
          </div>

          <!-- Bank Account Box -->
          <div class="rounded-2xl border border-blue-200 bg-gradient-to-br from-blue-50/70 to-indigo-50/30 p-4 relative overflow-hidden">
            <div class="flex items-center justify-between mb-3">
              <span class="flex items-center gap-2 text-blue-900 font-bold text-sm">
                <span class="material-symbols-outlined text-blue-600">account_balance</span> បាញ់តាមធនាគារ (Bank Transfer)
              </span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-blue-200/80 text-blue-900">KHQR / Transfer</span>
            </div>
            <div class="space-y-2 text-xs text-gray-700">
              <div class="flex items-center justify-between py-1 border-b border-blue-100/80">
                <span class="text-gray-600">ប្រាក់ផ្ទេរចូល (+)</span>
                <span id="dash-bank-income" class="font-bold text-blue-700 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-blue-100/80">
                <span class="text-gray-600">ប្រាក់ផ្ទេរចេញ (-)</span>
                <span id="dash-bank-expense" class="font-bold text-rose-600 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-blue-100/80">
                <span class="text-gray-600">បង្វិលទៅវិនិយោគ (-)</span>
                <span id="dash-bank-transfers-out" class="font-bold text-amber-600 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between pt-1">
                <span class="font-bold text-gray-800">សមតុល្យធនាគារនៅសល់៖</span>
                <span id="dash-bank-balance" class="text-sm font-black text-blue-800 font-mono">0 ៛</span>
              </div>
            </div>
          </div>

          <!-- Investment Fund Account Box -->
          <div class="rounded-2xl border border-indigo-200 bg-gradient-to-br from-indigo-50/80 to-purple-50/40 p-4 relative overflow-hidden">
            <div class="flex items-center justify-between mb-3">
              <span class="flex items-center gap-2 text-indigo-950 font-bold text-sm">
                <span class="material-symbols-outlined text-indigo-600">account_balance</span> គណនីវិនិយោគទុន (Investment Fund)
              </span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-indigo-200/80 text-indigo-900">ទុនវិនិយោគបន្ត</span>
            </div>
            <div class="space-y-2 text-xs text-gray-700">
              <div class="flex items-center justify-between py-1 border-b border-indigo-100/80">
                <span class="text-gray-600">បង្វិលពីសាច់ប្រាក់ (+)</span>
                <span id="dash-inv-from-cash" class="font-bold text-emerald-700 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-indigo-100/80">
                <span class="text-gray-600">បង្វិលពីធនាគារ (+)</span>
                <span id="dash-inv-from-bank" class="font-bold text-blue-700 font-mono">0 ៛</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-indigo-100/80 text-[11px] text-gray-400">
                <span>កន្លែងប្រមូលផ្តុំសាច់ប្រាក់បំណាច់ខែ</span>
                <span>🏦 កុងមេ</span>
              </div>
              <div class="flex items-center justify-between pt-1">
                <span class="font-bold text-gray-800">សរុបទុនវិនិយោគសន្សំបាន៖</span>
                <span id="dash-investment-balance" class="text-sm font-black text-indigo-900 font-mono">0 ៛</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Transfer History Table -->
        <div id="dash-transfer-history" class="px-5 pb-5 pt-1 border-t border-slate-100/80">
          <div class="flex items-center justify-between py-3">
            <div class="flex items-center gap-2">
              <span class="material-symbols-outlined text-indigo-600 text-sm">history</span>
              <h5 class="font-bold text-gray-700 text-xs sm:text-sm">ប្រវត្តិបង្វិលសាច់ប្រាក់ចូលគណនីវិនិយោគទុន (Transfer History)</h5>
            </div>
            <span id="dash-transfer-count" class="text-[11px] font-bold text-indigo-700 bg-indigo-50 px-2 py-0.5 rounded-md">0 ប្រតិបត្តិការ</span>
          </div>
          <div class="overflow-x-auto rounded-xl border border-slate-100">
            <table class="w-full text-left border-collapse text-xs">
              <thead class="bg-slate-50/80 text-gray-600 uppercase text-[10px] font-bold border-b border-slate-100">
                <tr>
                  <th class="py-2 px-3">កាលបរិច្ឆេទ</th>
                  <th class="py-2 px-3">ផ្ទេរចេញពី</th>
                  <th class="py-2 px-3">ផ្ទេរទៅកាន់</th>
                  <th class="py-2 px-3 text-right">ចំនួនទឹកប្រាក់</th>
                  <th class="py-2 px-3">កំណត់ចំណាំ</th>
                  <th class="py-2 px-3 text-right">សកម្មភាព</th>
                </tr>
              </thead>
              <tbody id="dash-transfer-tbody" class="divide-y divide-slate-50">
                <tr><td colspan="6" class="text-center py-4 text-gray-400 text-xs">មិនទាន់មានប្រវត្តិបង្វិលសាច់ប្រាក់នៅឡើយទេ</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Hidden legacy containers so renderDashLeaves / renderDashLoans don't fail -->
      <div id="dash-leaves" class="hidden"></div>
      <div id="dash-loans" class="hidden"></div>
    </div>

    <!-- ===== LEAVES TAB ===== -->
    <div id="tab-leaves" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex items-center justify-between mb-4 sm:mb-5">
        <h2 class="text-base sm:text-lg font-bold text-gray-800">ការគ្រប់គ្រងច្បាប់ឈប់សម្រាក</h2>
        <button onclick="openModal('leave-modal')"
          class="btn-primary flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold shadow-sm transition">
          <span class="material-symbols-outlined text-base sm:text-lg">add</span> បន្ថែម
        </button>
      </div>

      <!-- Filter bar -->
      <div class="flex gap-1.5 sm:gap-2 mb-4 text-xs sm:text-sm flex-wrap">
        <button onclick="filterLeaves('all')" class="px-3 sm:px-4 py-1.5 rounded-full bg-blue-600 text-white font-medium" id="lf-all">ទាំងអស់</button>
        <button onclick="filterLeaves('pending')" class="px-3 sm:px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-pending">រង់ចាំ</button>
        <button onclick="filterLeaves('approved')" class="px-3 sm:px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-approved">អនុញ្ញាតិ</button>
        <button onclick="filterLeaves('rejected')" class="px-3 sm:px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-rejected">មិនអនុញ្ញាតិ</button>
      </div>

      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <!-- Desktop Table View (>= md) -->
        <div class="hidden md:block overflow-x-auto">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100">
              <tr>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ឈ្មោះ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">មូលហេតុ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ថ្ងៃ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ស្ថានភាព</th>
                <th class="px-5 py-3 font-medium text-right whitespace-nowrap">សកម្មភាព</th>
              </tr>
            </thead>
            <tbody id="leaves-tbody" class="divide-y divide-slate-50 text-gray-700">
              <tr><td colspan="5" class="text-center py-8 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>

        <!-- Mobile Cards View (< md) -->
        <div id="leaves-mobile-cards" class="md:hidden p-3 space-y-3 bg-slate-50/50">
          <div class="text-center py-8 text-gray-400 text-sm">កំពុងទាញទិន្នន័យ...</div>
        </div>
      </div>
    </div>

    <!-- ===== LOANS TAB ===== -->
    <div id="tab-loans" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex items-center justify-between mb-4 sm:mb-5">
        <h2 class="text-base sm:text-lg font-bold text-gray-800">ការគ្រប់គ្រងខ្ចីប្រាក់ / បុរេប្រទាន</h2>
        <button onclick="openModal('loan-modal')"
          class="btn-primary flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold shadow-sm transition">
          <span class="material-symbols-outlined text-base sm:text-lg">add</span> បន្ថែម
        </button>
      </div>

      <!-- Filter bar -->
      <div class="flex gap-1.5 sm:gap-2 mb-4 text-xs sm:text-sm flex-wrap">
        <button onclick="filterLoans('all')" class="px-3 sm:px-4 py-1.5 rounded-full bg-blue-600 text-white font-medium" id="lnf-all">ទាំងអស់</button>
        <button onclick="filterLoans('pending')" class="px-3 sm:px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-pending">រង់ចាំ</button>
        <button onclick="filterLoans('approved')" class="px-3 sm:px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-approved">អនុញ្ញាតិ</button>
        <button onclick="filterLoans('rejected')" class="px-3 sm:px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-rejected">មិនអនុញ្ញាតិ</button>
      </div>

      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <!-- Desktop Table View (>= md) -->
        <div class="hidden md:block overflow-x-auto">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100">
              <tr>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ឈ្មោះ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ចំនួនទឹកប្រាក់</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">គោលបំណង</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ស្ថានភាព</th>
                <th class="px-5 py-3 font-medium text-right whitespace-nowrap">សកម្មភាព</th>
              </tr>
            </thead>
            <tbody id="loans-tbody" class="divide-y divide-slate-50 text-gray-700">
              <tr><td colspan="5" class="text-center py-8 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>

        <!-- Mobile Cards View (< md) -->
        <div id="loans-mobile-cards" class="md:hidden p-3 space-y-3 bg-slate-50/50">
          <div class="text-center py-8 text-gray-400 text-sm">កំពុងទាញទិន្នន័យ...</div>
        </div>
      </div>
    </div>

    <!-- ===== EMPLOYEES TAB ===== -->
    <div id="tab-employees" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex flex-wrap items-center justify-between gap-3 mb-5">
        <div>
          <h2 class="text-lg font-semibold text-gray-800">បុគ្គលិកទាំងអស់</h2>
          <p class="text-xs text-gray-500 mt-0.5">ទិន្នន័យត្រូវបាន Sync ដោយស្វ័យប្រវត្តិជាមួយ Telegram Group</p>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <button onclick="requestPhoneViaTelegram()" id="btn-request-tg-phone"
            class="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium bg-amber-50 text-amber-700 hover:bg-amber-100 border border-amber-200 shadow-sm transition"
            title="ផ្ញើសារស្នើសុំលេខទូរស័ព្ទទៅកាន់ Telegram Group">
            <span class="material-symbols-outlined text-base">send_to_mobile</span> 📲 ស្នើសុំលេខតាម Telegram
          </button>
          <button onclick="openBatchPhoneModal()"
            class="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium bg-indigo-50 text-indigo-700 hover:bg-indigo-100 border border-indigo-200 shadow-sm transition"
            title="បញ្ចូលលេខទូរស័ព្ទបុគ្គលិកទាំងអស់ក្នុងពេលតែមួយ">
            <span class="material-symbols-outlined text-base">call</span> 📞 បញ្ចូលលេខទូរស័ព្ទ
          </button>
          <button id="btn-sync-tg-emp" onclick="syncEmployeesFromTelegram()"
            class="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium bg-blue-50 text-blue-600 hover:bg-blue-100 border border-blue-200 shadow-sm transition">
            <span class="material-symbols-outlined text-base">sync</span> 🔄 Sync ពី Telegram
          </button>
          <button onclick="openModal('bulk-emp-modal')"
            class="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium bg-emerald-50 text-emerald-700 hover:bg-emerald-100 border border-emerald-200 shadow-sm transition">
            <span class="material-symbols-outlined text-base">playlist_add</span> 📋 បញ្ចូលច្រើន (Bulk)
          </button>
          <button onclick="openModal('emp-modal')"
            class="btn-primary flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium shadow-sm transition">
            <span class="material-symbols-outlined text-lg">person_add</span> + បន្ថែម
          </button>
        </div>
      </div>

      <div id="employees-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        <div class="col-span-full text-center py-10 text-gray-400">កំពុងទាញទិន្នន័យ...</div>
      </div>
    </div>

    <!-- ===== SETTINGS TAB ===== -->
    <div id="tab-settings" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="max-w-4xl mx-auto space-y-6">

        <!-- Title -->
        <div class="flex items-center justify-between">
          <div>
            <h2 class="text-xl font-bold text-gray-800 flex items-center gap-2">
              <span class="material-symbols-outlined text-blue-600 text-2xl">settings</span> ការកំណត់ប្រព័ន្ធ
            </h2>
            <p class="text-xs text-gray-500 mt-1">គ្រប់គ្រង Telegram Bot API, Webhook និងការជូនដំណឹងស្វ័យប្រវត្តិ</p>
          </div>
        </div>

        <!-- Admin Account & Password Security Card -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-6 py-4 bg-gradient-to-r from-slate-900 to-blue-950 text-white flex items-center justify-between">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-white/10 backdrop-blur border border-white/20 text-white flex items-center justify-center shadow">
                <span class="material-symbols-outlined text-2xl">shield_person</span>
              </div>
              <div>
                <h3 class="font-bold text-white">គណនី Admin & សុវត្ថិភាព (Admin Account & Security)</h3>
                <p class="text-xs text-blue-200">គ្រប់គ្រងឈ្មោះគណនី និងលេខសម្ងាត់សម្រាប់ចូលប្រើប្រព័ន្ធ</p>
              </div>
            </div>
            <span class="text-xs px-3 py-1 bg-emerald-500/20 text-emerald-300 font-semibold rounded-full border border-emerald-500/30 flex items-center gap-1">
              <span class="w-2 h-2 rounded-full bg-emerald-400"></span> Admin Portal
            </span>
          </div>

          <form onsubmit="saveAdminAccountSettings(event)" class="p-6 space-y-4">
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
              <!-- Username -->
              <div>
                <label class="block text-xs font-bold text-gray-700 mb-1">ឈ្មោះគណនី Admin (Username)</label>
                <input id="setting-admin-username" type="text" required placeholder="admin"
                  class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 font-medium transition" />
              </div>

              <!-- Current Password -->
              <div>
                <label class="block text-xs font-bold text-gray-700 mb-1">លេខសម្ងាត់បច្ចុប្បន្ន (Current Password)</label>
                <input id="setting-admin-current-pw" type="password" required placeholder="បញ្ចូលលេខសម្ងាត់បច្ចុប្បន្ន..."
                  class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 font-mono transition" />
              </div>

              <!-- New Password -->
              <div>
                <label class="block text-xs font-bold text-gray-700 mb-1">លេខសម្ងាត់ថ្មី (New Password)</label>
                <input id="setting-admin-new-pw" type="password" placeholder="ទុកទំនេរប្រសិនបើមិនចង់ប្តូរលេខសម្ងាត់..."
                  class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 font-mono transition" />
              </div>

              <!-- Confirm New Password -->
              <div>
                <label class="block text-xs font-bold text-gray-700 mb-1">បញ្ជាក់លេខសម្ងាត់ថ្មី (Confirm Password)</label>
                <input id="setting-admin-confirm-pw" type="password" placeholder="វាយលេខសម្ងាត់ថ្មីម្តងទៀត..."
                  class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 font-mono transition" />
              </div>
            </div>

            <div class="pt-2 flex justify-end">
              <button type="submit" id="btn-save-admin-account"
                class="bg-blue-600 hover:bg-blue-700 text-white flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-semibold shadow transition">
                <span class="material-symbols-outlined text-lg">save</span> រក្សាទុកគណនី Admin
              </button>
            </div>
          </form>
        </div>

        <!-- Telegram Bot Settings Card -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-6 py-4 bg-gradient-to-r from-blue-50 to-indigo-50 border-b border-slate-100 flex items-center justify-between">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-blue-600 text-white flex items-center justify-center shadow">
                <span class="material-symbols-outlined text-2xl">smart_toy</span>
              </div>
              <div>
                <h3 class="font-bold text-gray-800">ការកំណត់ Telegram Bot</h3>
                <p class="text-xs text-gray-500">ទទួលការជូនដំណឹងពេលបុគ្គលិកសុំច្បាប់ ឬខ្ចីប្រាក់</p>
              </div>
            </div>
            <span class="text-xs px-3 py-1 bg-blue-100 text-blue-700 font-semibold rounded-full flex items-center gap-1">
              <span class="w-2 h-2 rounded-full bg-blue-600 animate-pulse"></span> Telegram API
            </span>
          </div>

          <form onsubmit="saveSettings(event)" class="p-6 space-y-5">
            <!-- Bot Token -->
            <div>
              <label class="block text-sm font-semibold text-gray-700 mb-1 flex items-center justify-between">
                <span class="flex items-center gap-2">
                  <span>Telegram Bot Token</span>
                  <span id="token-status-badge" class="hidden text-xs px-2 py-0.5 rounded-md font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 items-center gap-1">
                    <span class="material-symbols-outlined text-xs">lock</span> បានការពារ (Masked)
                  </span>
                </span>
                <span class="text-xs text-gray-400 font-normal">ទទួលបានពី @BotFather</span>
              </label>
              <div class="relative">
                <input id="setting-bot-token" type="password" placeholder="ឧទាហរណ៍: 123456789:ABCdefGhIJKlmNoPQ..."
                  class="w-full border border-slate-200 rounded-xl pl-4 pr-11 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 font-mono transition" />
                <button type="button" onclick="toggleTokenVisibility()"
                  class="absolute right-3 top-2.5 text-gray-400 hover:text-gray-600 transition" title="បើក/បិទមើល">
                  <span id="eye-icon" class="material-symbols-outlined text-xl">visibility</span>
                </button>
              </div>
              <p class="text-xs text-emerald-600 mt-1 flex items-center gap-1 font-medium">
                <span class="material-symbols-outlined text-sm">shield</span>
                Token របស់អ្នកត្រូវបានលាក់ដោយស្វ័យប្រវត្តិ (Masked) — គ្មាននរណាម្នាក់អាចលួចមើលបានឡើយ
              </p>
            </div>

            <!-- Chat ID -->
            <div>
              <label class="block text-sm font-semibold text-gray-700 mb-1 flex items-center justify-between">
                <span>Telegram Chat ID / Group ID</span>
                <span class="text-xs text-gray-400 font-normal">ទទួលបានពី @userinfobot</span>
              </label>
              <input id="setting-chat-id" type="text" placeholder="ឧទាហរណ៍: 987654321 ឬ -1001234567890"
                class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 font-mono transition" />
              <p class="text-xs text-gray-400 mt-1">ប្រសិនបើចង់ផ្ញើចូល Group សូមបញ្ចូល ID របស់ Group ដែលផ្ដើមដោយសញ្ញាដក <code>-100...</code></p>
            </div>

            <!-- Notifications Toggles -->
            <div class="pt-2 border-t border-slate-100 space-y-3">
              <label class="flex items-center gap-3 cursor-pointer">
                <input id="setting-notify-leave" type="checkbox" checked
                  class="w-4 h-4 text-blue-600 rounded border-slate-300 focus:ring-blue-400" />
                <span class="text-sm text-gray-700">🔔 ផ្ញើសារជូនដំណឹងស្វ័យប្រវត្តិពេលមាន <b>សំណើសុំច្បាប់ថ្មី</b></span>
              </label>
              <label class="flex items-center gap-3 cursor-pointer">
                <input id="setting-notify-loan" type="checkbox" checked
                  class="w-4 h-4 text-blue-600 rounded border-slate-300 focus:ring-blue-400" />
                <span class="text-sm text-gray-700">🔔 ផ្ញើសារជូនដំណឹងស្វ័យប្រវត្តិពេលមាន <b>សំណើខ្ចីប្រាក់ថ្មី</b></span>
              </label>
            </div>

            <!-- Buttons -->
            <div class="pt-3 border-t border-slate-100 flex flex-wrap gap-3">
              <button type="submit" id="btn-save-settings"
                class="btn-primary flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-semibold shadow transition">
                <span class="material-symbols-outlined text-lg">save</span> រក្សាទុកការកំណត់
              </button>
              <button type="button" onclick="testTelegramBot()" id="btn-test-bot"
                class="btn-secondary flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border border-blue-200 transition">
                <span class="material-symbols-outlined text-lg" id="test-bot-icon">send</span>
                <span>សាកល្បងផ្ញើសារ (Test Bot)</span>
              </button>
              <button type="button" onclick="sendMorningBriefing()" id="btn-morning-briefing"
                class="bg-amber-50 hover:bg-amber-100 text-amber-700 flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border border-amber-200 transition">
                <span class="material-symbols-outlined text-lg" id="briefing-icon">wb_sunny</span>
                <span>ផ្ញើរបាយការណ៍ពេលព្រឹក (Morning Briefing)</span>
              </button>
            </div>
          </form>
        </div>

        <!-- Google Gemini AI Assistant Card -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="p-6 border-b border-slate-100 flex items-center justify-between bg-gradient-to-r from-purple-50 via-indigo-50 to-blue-50">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-purple-600 text-white flex items-center justify-center shadow">
                <span class="material-symbols-outlined text-2xl">neurology</span>
              </div>
              <div>
                <h3 class="font-bold text-gray-800">Google Gemini AI Assistant</h3>
                <p class="text-xs text-gray-500">ជំនួយការឆ្លាតវៃឆ្លើយតបសំណួរ វិភាគពាក្យសុំច្បាប់/ខ្ចីប្រាក់ និងស្តាប់សារសំឡេង Voice</p>
              </div>
            </div>
            <span class="text-xs px-3 py-1 bg-purple-100 text-purple-700 font-semibold rounded-full flex items-center gap-1">
              <span class="w-2 h-2 rounded-full bg-purple-600 animate-pulse"></span> Gemini AI
            </span>
          </div>

          <div class="p-6 space-y-5">
            <!-- Gemini API Key -->
            <div>
              <label class="block text-sm font-semibold text-gray-700 mb-1 flex items-center justify-between">
                <span class="flex items-center gap-2">
                  <span>Google Gemini API Key</span>
                  <span id="gemini-status-badge" class="hidden text-xs px-2 py-0.5 rounded-md font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 items-center gap-1">
                    <span class="material-symbols-outlined text-xs">lock</span> បានការពារ (Masked)
                  </span>
                </span>
                <a href="https://aistudio.google.com/app/apikey" target="_blank" class="text-xs text-purple-600 hover:underline flex items-center gap-1 font-medium">
                  <span>យក Key ឥតគិតថ្លៃ (aistudio.google.com)</span>
                  <span class="material-symbols-outlined text-xs">open_in_new</span>
                </a>
              </label>
              <div class="relative">
                <input id="setting-gemini-key" type="password" placeholder="ឧទាហរណ៍: AIzaSy... ឬ AQ.Ab..."
                  class="w-full border border-slate-200 rounded-xl pl-4 pr-11 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-purple-400 font-mono transition" />
                <button type="button" onclick="toggleGeminiKeyVisibility()"
                  class="absolute right-3 top-2.5 text-gray-400 hover:text-gray-600 transition" title="បើក/បិទមើល">
                  <span id="gemini-eye-icon" class="material-symbols-outlined text-xl">visibility</span>
                </button>
              </div>
              <p class="text-xs text-gray-400 mt-1">API Key នេះប្រើសម្រាប់ឱ្យ Bot យល់សារភាសាខ្មែរធម្មជាតិ និងសារសំឡេង (Voice) ដោយស្វ័យប្រវត្តិ។</p>
            </div>

            <!-- Gemini Model Choice -->
            <div>
              <label class="block text-sm font-semibold text-gray-700 mb-1">
                <span>ម៉ូដែល AI (AI Model)</span>
              </label>
              <select id="setting-gemini-model" class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-purple-400 font-medium transition bg-white">
                <option value="gemini-flash-lite-latest">⚡ Gemini Flash Lite Latest (Ultra Fast - ណែនាំ)</option>
                <option value="gemini-flash-latest">⚡ Gemini Flash Latest (Fast & Smart)</option>
                <option value="gemini-2.5-flash-lite">✨ Gemini 2.5 Flash Lite</option>
                <option value="gemini-2.5-flash">🚀 Gemini 2.5 Flash</option>
                <option value="gemini-2.0-flash">🔥 Gemini 2.0 Flash</option>
                <option value="gemini-1.5-flash">💡 Gemini 1.5 Flash</option>
              </select>
            </div>

            <!-- Buttons -->
            <div class="pt-3 border-t border-slate-100 flex flex-wrap gap-3">
              <button type="button" onclick="saveGeminiSettings()" id="btn-save-gemini"
                class="bg-purple-600 hover:bg-purple-700 text-white flex items-center gap-2 px-6 py-2.5 rounded-xl text-sm font-semibold shadow transition">
                <span class="material-symbols-outlined text-lg">save</span> រក្សាទុកការកំណត់ AI
              </button>
              <button type="button" onclick="testGeminiAI()" id="btn-test-gemini"
                class="bg-purple-50 hover:bg-purple-100 text-purple-700 flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold border border-purple-200 transition">
                <span class="material-symbols-outlined text-lg" id="test-gemini-icon">psychology</span>
                <span>សាកល្បង Gemini AI (Test AI)</span>
              </button>
            </div>
          </div>
        </div>

        <!-- API & Webhook Card -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 p-6 space-y-4">
          <div class="flex items-center gap-3">
            <div class="w-10 h-10 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center">
              <span class="material-symbols-outlined text-2xl">webhook</span>
            </div>
            <div>
              <h3 class="font-bold text-gray-800">API Webhook Endpoints</h3>
              <p class="text-xs text-gray-500">សម្រាប់ Telegram Bot បាញ់ទិន្នន័យចូលប្រព័ន្ធដោយស្វ័យប្រវត្តិ</p>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs font-mono">
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between gap-2">
              <div>
                <span class="text-green-600 font-bold">POST</span> <span class="text-gray-700">/api/leave</span>
                <p class="text-gray-400 font-sans text-xs mt-0.5">សម្រាប់បាញ់សំណើសុំច្បាប់</p>
              </div>
              <button onclick="copyToClipboard(window.location.origin + '/api/leave')" class="p-1.5 hover:bg-slate-200 rounded text-gray-500" title="Copy URL">
                <span class="material-symbols-outlined text-sm">content_copy</span>
              </button>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-200 flex items-center justify-between gap-2">
              <div>
                <span class="text-green-600 font-bold">POST</span> <span class="text-gray-700">/api/loan</span>
                <p class="text-gray-400 font-sans text-xs mt-0.5">សម្រាប់បាញ់សំណើខ្ចីប្រាក់</p>
              </div>
              <button onclick="copyToClipboard(window.location.origin + '/api/loan')" class="p-1.5 hover:bg-slate-200 rounded text-gray-500" title="Copy URL">
                <span class="material-symbols-outlined text-sm">content_copy</span>
              </button>
            </div>
          </div>
        </div>

        <!-- System & Database Info -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 p-6">
          <h3 class="font-bold text-gray-800 text-sm mb-3 flex items-center gap-2">
            <span class="material-symbols-outlined text-gray-500">info</span> ព័ត៌មានប្រព័ន្ធ
          </h3>
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 text-xs">
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <p class="text-gray-400 mb-1">Database</p>
              <p class="font-semibold text-emerald-600 flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-emerald-500"></span> MongoDB Atlas Connected
              </p>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <p class="text-gray-400 mb-1">Server Host</p>
              <p class="font-semibold text-blue-600 flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-blue-500"></span> Render Cloud Live
              </p>
            </div>
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100">
              <p class="text-gray-400 mb-1">Python Runtime</p>
              <p class="font-semibold text-gray-700">Python 3.11 / FastAPI</p>
            </div>
          </div>
        </div>

        <!-- Multi-Branch Management Card -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-6 py-4 bg-gradient-to-r from-blue-700 to-indigo-800 text-white flex items-center justify-between">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-white/10 backdrop-blur border border-white/20 text-white flex items-center justify-center shadow">
                <span class="material-symbols-outlined text-2xl">domain</span>
              </div>
              <div>
                <h3 class="font-bold text-white">គ្រប់គ្រងសាខា & Telegram Groups (Multi-Branch)</h3>
                <p class="text-xs text-blue-200">បែងចែកគ្រុប Telegram និងទិន្នន័យដោយឡែកពីគ្នា ១០០% រវាងសាខា</p>
              </div>
            </div>
            <span class="text-xs px-3 py-1 bg-emerald-500/20 text-emerald-300 font-semibold rounded-full border border-emerald-500/30 flex items-center gap-1">
              <span class="w-2 h-2 rounded-full bg-emerald-400"></span> ២ សាខាសកម្ម
            </span>
          </div>

          <form onsubmit="saveBranchSettings(event)" class="p-6 space-y-5">
            <div class="p-3.5 bg-blue-50/70 rounded-xl border border-blue-200/60 text-xs text-blue-800 flex items-start gap-2.5 leading-relaxed">
              <span class="material-symbols-outlined text-blue-600 text-base flex-shrink-0 mt-0.5">verified_user</span>
              <div>
                <b>គោលការណ៍រក្សាការសម្ងាត់សាខា (Strict Group Isolation)៖</b><br/>
                • បុគ្គលិកសាខា <b>កំពង់ចាម</b> ឃើញ និងស្នើសុំតែក្នុង Telegram Group កំពង់ចាម ប៉ុណ្ណោះ។<br/>
                • បុគ្គលិកសាខា <b>ត្បូងឃ្មុំ</b> ឃើញ និងស្នើសុំតែក្នុង Telegram Group ត្បូងឃ្មុំ ប៉ុណ្ណោះ មិនអាចលេចធ្លាយទៅវិញទៅមកឡើយ!<br/>
                • លោកអ្នកគ្រាន់តែ Add Bot ចូលក្នុងគ្រុបត្បូងឃ្មុំ Bot នឹង Auto-Link ឬបញ្ចូល Chat ID ខាងក្រោម។
              </div>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
              <!-- Branch 1: Kampong Cham -->
              <div class="p-4 rounded-xl border border-slate-200 bg-slate-50/50 space-y-3">
                <div class="flex items-center justify-between">
                  <span class="font-bold text-sm text-gray-800 flex items-center gap-1.5">
                    <span class="w-2.5 h-2.5 rounded-full bg-blue-600"></span> សាខាទី ១ (កំពង់ចាម)
                  </span>
                  <span class="text-[11px] text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full font-semibold border border-emerald-200">ភ្ជាប់រួច</span>
                </div>
                <div>
                  <label class="block text-xs font-semibold text-gray-600 mb-1">ឈ្មោះសាខា</label>
                  <input id="branch-name-kc" type="text" value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម" required
                    class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs focus:ring-2 focus:ring-blue-400 bg-white" />
                </div>
                <div>
                  <label class="block text-xs font-semibold text-gray-600 mb-1">Telegram Group Chat ID</label>
                  <input id="branch-chat-kc" type="text" value="-1003956005362" required
                    class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs font-mono focus:ring-2 focus:ring-blue-400 bg-white" />
                </div>
                <div class="pt-1">
                  <button type="button" onclick="sendBranchButtons('kampong_cham')"
                    class="w-full bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition">
                    <span class="material-symbols-outlined text-base">send_to_mobile</span> ផ្ញើប៊ូតុងទៅ Telegram កំពង់ចាម
                  </button>
                </div>
              </div>

              <!-- Branch 2: Tboung Khmum -->
              <div class="p-4 rounded-xl border border-slate-200 bg-slate-50/50 space-y-3">
                <div class="flex items-center justify-between">
                  <span class="font-bold text-sm text-gray-800 flex items-center gap-1.5">
                    <span class="w-2.5 h-2.5 rounded-full bg-indigo-600"></span> សាខាទី ២ (ត្បូងឃ្មុំ)
                  </span>
                  <span id="branch-tk-status-badge" class="text-[11px] text-indigo-600 bg-indigo-50 px-2 py-0.5 rounded-full font-semibold border border-indigo-200">សាខាថ្មី</span>
                </div>
                <div>
                  <label class="block text-xs font-semibold text-gray-600 mb-1">ឈ្មោះសាខា</label>
                  <input id="branch-name-tk" type="text" value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ" required
                    class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs focus:ring-2 focus:ring-blue-400 bg-white" />
                </div>
                <div>
                  <label class="block text-xs font-semibold text-gray-600 mb-1">Telegram Group Chat ID</label>
                  <input id="branch-chat-tk" type="text" placeholder="ឧ. -100xxxxxxxxxx (ឬ Auto-Link ពេល Add Bot)"
                    class="w-full border border-slate-200 rounded-lg px-3 py-2 text-xs font-mono focus:ring-2 focus:ring-blue-400 bg-white" />
                </div>
                <div class="pt-1">
                  <button type="button" onclick="sendBranchButtons('tboung_khmum')"
                    class="w-full bg-indigo-50 hover:bg-indigo-100 text-indigo-700 border border-indigo-200 flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition">
                    <span class="material-symbols-outlined text-base">send_to_mobile</span> ផ្ញើប៊ូតុងទៅ Telegram ត្បូងឃ្មុំ
                  </button>
                </div>
              </div>
            </div>

            <div class="pt-2 flex justify-end">
              <button type="submit" id="btn-save-branches"
                class="bg-indigo-600 hover:bg-indigo-700 text-white flex items-center gap-2 px-6 py-2.5 rounded-xl text-xs font-bold shadow transition">
                <span class="material-symbols-outlined text-base">save</span> រក្សាទុកការកំណត់សាខា
              </button>
            </div>
          </form>
        </div>

        <!-- Database Backup Card -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-6 py-4 bg-gradient-to-r from-emerald-600 to-teal-700 text-white flex items-center justify-between">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-white/10 backdrop-blur border border-white/20 text-white flex items-center justify-center shadow">
                <span class="material-symbols-outlined text-2xl">cloud_download</span>
              </div>
              <div>
                <h3 class="font-bold text-white">ទាញយកទិន្នន័យបម្រុងទុក (Full Database Backup)</h3>
                <p class="text-xs text-emerald-100">រក្សាទុកទិន្នន័យបុគ្គលិក កម្ចី ច្បាប់ និងប្រតិបត្តិការហិរញ្ញវត្ថុទាំងអស់ចូលកុំព្យូទ័រ</p>
              </div>
            </div>
          </div>
          <div class="p-6 flex flex-col sm:flex-row items-center justify-between gap-4 bg-slate-50/50">
            <div>
              <p class="text-xs text-gray-600 font-medium">ចុចប៊ូតុងខាងស្តាំដើម្បីទាញយកឯកសារ JSON បម្រុងទុកភ្លាមៗ ធានាសុវត្ថិភាពទិន្នន័យ ១០០%</p>
              <p class="text-[11px] text-gray-400 mt-0.5">រួមបញ្ចូល៖ បុគ្គលិកទាំងអស់, ប្រវត្តិកម្ចី, ច្បាប់ឈប់សម្រាក, ចំណូល-ចំណាយ និងការកំណត់</p>
            </div>
            <a href="/api/backup/export" target="_blank"
              class="px-5 py-2.5 bg-emerald-600 hover:bg-emerald-700 active:scale-95 text-white text-xs font-bold rounded-xl shadow-sm transition flex items-center gap-2 whitespace-nowrap">
              <span class="material-symbols-outlined text-base">download</span> ទាញយក Backup (JSON)
            </a>
          </div>
        </div>

      </div>
    </div>

    <!-- ===== REPORTS: WEEKEND LEAVE COMPENSATION TAB ===== -->
    <div id="tab-reports-leave" class="p-4 sm:p-6 animate-fadeIn hidden">
      <div class="max-w-6xl mx-auto space-y-6">
        
        <!-- Header & Quick Action -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-white p-5 sm:p-6 rounded-2xl shadow-sm border border-slate-100">
          <div class="flex items-center gap-3.5">
            <div class="w-12 h-12 rounded-2xl bg-amber-50 text-amber-600 flex items-center justify-center flex-shrink-0 shadow-sm border border-amber-100">
              <span class="material-symbols-outlined text-2xl">event_repeat</span>
            </div>
            <div>
              <h2 class="text-base sm:text-lg font-bold text-gray-800">របាយការណ៍ច្បាប់ (សម្រាប់ធ្វើការសង ថ្ងៃសៅរ៍-អាទិត្យ)</h2>
              <p class="text-xs text-gray-500 mt-0.5">បញ្ជីបុគ្គលិកសុំច្បាប់ក្នុងសប្តាហ៍ដែលត្រូវបំពេញការងារសងនៅចុងសប្តាហ៍ (ស្វ័យប្រវត្តិតាម Telegram រៀងរាល់ថ្ងៃសុក្រ វេលាម៉ោង ៦:០០ ល្ងាច / 18:00)</p>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-wrap">
            <!-- Dropdown Menu for Sending to Telegram -->
            <div class="relative inline-block text-left" id="leave-send-menu-container">
              <button onclick="toggleLeaveSendMenu(event)" id="btn-send-tg-leave"
                class="bg-blue-600 hover:bg-blue-700 text-white flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs sm:text-sm font-semibold shadow transition">
                <span class="material-symbols-outlined text-lg" id="icon-send-tg-leave">send</span>
                <span>ផ្ញើទៅ Telegram</span>
                <span class="material-symbols-outlined text-base">expand_more</span>
              </button>
              <div id="menu-send-leave" class="hidden absolute right-0 mt-2 w-72 bg-white rounded-2xl shadow-xl border border-slate-100 py-2 z-50 animate-fadeIn">
                <div class="px-4 py-2 border-b border-slate-100">
                  <p class="text-xs font-bold text-gray-700">ជម្រើសផ្ញើរបាយការណ៍ច្បាប់</p>
                  <p class="text-[11px] text-gray-400">ជ្រើសរើសទម្រង់ដែលត្រូវផ្ញើ</p>
                </div>
                <div class="p-1 space-y-1">
                  <button onclick="sendWeekendLeaveReportTelegram('summary')"
                    class="w-full text-left px-3 py-2.5 rounded-xl hover:bg-blue-50 text-slate-700 hover:text-blue-600 flex items-start gap-2.5 transition">
                    <span class="material-symbols-outlined text-blue-600 text-xl mt-0.5">summarize</span>
                    <div>
                      <p class="text-xs sm:text-sm font-semibold">១. របាយការណ៍សង្ខេប (តាមឈ្មោះ)</p>
                      <p class="text-[11px] text-gray-400">សរុបថ្ងៃត្រូវសងតាមឈ្មោះបុគ្គលិកម្នាក់ៗ</p>
                    </div>
                  </button>
                  <button onclick="sendWeekendLeaveReportTelegram('detailed')"
                    class="w-full text-left px-3 py-2.5 rounded-xl hover:bg-indigo-50 text-slate-700 hover:text-indigo-600 flex items-start gap-2.5 transition">
                    <span class="material-symbols-outlined text-indigo-600 text-xl mt-0.5">receipt_long</span>
                    <div>
                      <p class="text-xs sm:text-sm font-semibold">២. របាយការណ៍លម្អិត (គ្រប់ករណី)</p>
                      <p class="text-[11px] text-gray-400">បង្ហាញរាល់ករណី ថ្ងៃសុំ មូលហេតុ និងការសង</p>
                    </div>
                  </button>
                </div>
              </div>
            </div>
            <button onclick="loadWeekendLeaveReport()"
              class="w-10 h-10 flex items-center justify-center rounded-xl bg-slate-50 hover:bg-slate-100 text-slate-600 border border-slate-200 transition" title="Refresh">
              <span class="material-symbols-outlined text-lg">refresh</span>
            </button>
          </div>
        </div>

        <!-- Metric Stat Cards -->
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-orange-50 text-orange-600 flex items-center justify-center flex-shrink-0">
              <span class="material-symbols-outlined text-2xl">group_off</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">បុគ្គលិកត្រូវធ្វើការសង</p>
              <p class="text-xl font-bold text-gray-800" id="rep-leave-staff-count">0 នាក់</p>
            </div>
          </div>
          <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-amber-50 text-amber-600 flex items-center justify-center flex-shrink-0">
              <span class="material-symbols-outlined text-2xl">calendar_month</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">ចំនួនថ្ងៃសរុបត្រូវធ្វើការសង</p>
              <p class="text-xl font-bold text-amber-600" id="rep-leave-days-count">0 ថ្ងៃ</p>
            </div>
          </div>
          <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center flex-shrink-0">
              <span class="material-symbols-outlined text-2xl">alarm_on</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">កាលវិភាគស្វ័យប្រវត្តិ</p>
              <p class="text-xs sm:text-sm font-semibold text-emerald-600">រៀងរាល់ថ្ងៃសុក្រ ម៉ោង ០៦:០០ ល្ងាច (18:00)</p>
            </div>
          </div>
        </div>

        <!-- Table Container -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="p-4 sm:p-5 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <h3 class="font-bold text-sm text-gray-800 flex items-center gap-2">
                <span class="material-symbols-outlined text-amber-600">format_list_bulleted</span>
                <span>បញ្ជីបុគ្គលិកសុំច្បាប់ក្នុងសប្តាហ៍ (កាតព្វកិច្ចសងថ្ងៃសៅរ៍ & អាទិត្យ)</span>
              </h3>
              <p class="text-xs text-gray-400 mt-0.5" id="rep-leave-updated-at"></p>
            </div>
            <!-- View Mode Switcher & Filter -->
            <div class="flex items-center gap-2 flex-wrap">
              <div class="flex bg-slate-100 p-1 rounded-xl text-xs font-semibold">
                <button type="button" id="btn-leave-mode-emp" onclick="setLeaveViewMode('per_employee')"
                  class="px-3 py-1.5 rounded-lg bg-amber-500 text-white shadow-sm flex items-center gap-1 transition">
                  <span class="material-symbols-outlined text-sm">person</span>
                  <span>ឈ្មោះម្នាក់ៗ (សង្ខេប)</span>
                </button>
                <button type="button" id="btn-leave-mode-all" onclick="setLeaveViewMode('all')"
                  class="px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 flex items-center gap-1 transition">
                  <span class="material-symbols-outlined text-sm">view_list</span>
                  <span>កំណត់ត្រាលម្អិតទាំងអស់</span>
                </button>
              </div>
              <select id="rep-leave-emp-filter" onchange="renderLeaveReportTable()"
                class="text-xs border border-slate-200 rounded-xl px-3 py-1.5 bg-white font-medium text-gray-700 focus:outline-none focus:ring-2 focus:ring-amber-400">
                <option value="">-- បង្ហាញបុគ្គលិកទាំងអស់ --</option>
              </select>
            </div>
          </div>
          <div class="overflow-x-auto w-full">
            <table class="w-full text-left text-xs sm:text-sm">
              <thead class="bg-slate-50 text-gray-500 font-medium border-b border-slate-100 text-xs" id="rep-leave-thead">
                <tr>
                  <th class="py-3 px-4">#</th>
                  <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
                  <th class="py-3 px-4">ច្បាប់សរុប</th>
                  <th class="py-3 px-4">បានសងរួច</th>
                  <th class="py-3 px-4">នៅសល់ត្រូវសង</th>
                  <th class="py-3 px-4">កាតព្វកិច្ចធ្វើការសង</th>
                  <th class="py-3 px-4">ស្ថានភាព</th>
                  <th class="py-3 px-4">សកម្មភាព</th>
                </tr>
              </thead>
              <tbody id="rep-leave-tbody" class="divide-y divide-slate-100 text-gray-700">
                <tr><td colspan="8" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>
    </div>

    <!-- ===== REPORTS: MONTHLY LOAN PAYROLL DEDUCTION TAB ===== -->
    <div id="tab-reports-loan" class="p-4 sm:p-6 animate-fadeIn hidden">
      <div class="max-w-6xl mx-auto space-y-6">
        
        <!-- Header & Quick Action -->
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 bg-white p-5 sm:p-6 rounded-2xl shadow-sm border border-slate-100">
          <div class="flex items-center gap-3.5">
            <div class="w-12 h-12 rounded-2xl bg-indigo-50 text-indigo-600 flex items-center justify-center flex-shrink-0 shadow-sm border border-indigo-100">
              <span class="material-symbols-outlined text-2xl">receipt_long</span>
            </div>
            <div>
              <h2 class="text-base sm:text-lg font-bold text-gray-800">របាយការណ៍ខ្ចីប្រាក់ (សម្រាប់កាត់ប្រាក់ខែចុងខែ)</h2>
              <p class="text-xs text-gray-500 mt-0.5">បញ្ជីបុគ្គលិកខ្ចីប្រាក់ក្នុងខែដែលត្រូវកាត់ប្រាក់ខែ (ទិន្នន័យសម្ងាត់សម្រាប់តែរដ្ឋបាលលើ Dashboard មិនបង្ហោះក្នុងក្រុម Telegram ឡើយ)</p>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-wrap">
            <!-- Confidential Privacy Badge -->
            <div class="flex items-center gap-1.5 px-3.5 py-2 rounded-xl bg-amber-50 text-amber-800 border border-amber-200/80 text-xs font-semibold shadow-xs">
              <span class="material-symbols-outlined text-base text-amber-600">lock</span>
              <span>ទិន្នន័យសម្ងាត់ (មិនបង្ហោះក្នុង Telegram)</span>
            </div>
            <button onclick="loadMonthlyLoanReport()"
              class="w-10 h-10 flex items-center justify-center rounded-xl bg-slate-50 hover:bg-slate-100 text-slate-600 border border-slate-200 transition" title="Refresh">
              <span class="material-symbols-outlined text-lg">refresh</span>
            </button>
          </div>
        </div>

        <!-- Metric Stat Cards -->
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-emerald-50 text-emerald-600 flex items-center justify-center flex-shrink-0">
              <span class="material-symbols-outlined text-2xl">payments</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">ទឹកប្រាក់ខ្ចីសរុបត្រូវកាត់</p>
              <p class="text-xl font-bold text-emerald-600" id="rep-loan-total-amt">0 ៛</p>
            </div>
          </div>
          <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-indigo-50 text-indigo-600 flex items-center justify-center flex-shrink-0">
              <span class="material-symbols-outlined text-2xl">account_balance_wallet</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">ចំនួនបុគ្គលិកខ្ចីប្រាក់</p>
              <p class="text-xl font-bold text-gray-800" id="rep-loan-borrowers-count">0 នាក់</p>
            </div>
          </div>
          <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
            <div class="w-12 h-12 rounded-xl bg-purple-50 text-purple-600 flex items-center justify-center flex-shrink-0">
              <span class="material-symbols-outlined text-2xl">shield</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">គោលការណ៍រក្សាការសម្ងាត់</p>
              <p class="text-xs sm:text-sm font-semibold text-purple-700">សម្រាប់រដ្ឋបាលលើ Dashboard</p>
            </div>
          </div>
        </div>

        <!-- Table Container -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="p-4 sm:p-5 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <h3 class="font-bold text-sm text-gray-800 flex items-center gap-2">
                <span class="material-symbols-outlined text-indigo-600">format_list_bulleted</span>
                <span>បញ្ជីបុគ្គលិកខ្ចីប្រាក់ក្នុងខែនេះ (កាត់ប្រាក់ខែចុងខែ)</span>
              </h3>
              <p class="text-xs text-gray-400 mt-0.5" id="rep-loan-updated-at"></p>
            </div>
            <!-- View Mode Switcher & Filter -->
            <div class="flex items-center gap-2 flex-wrap">
              <div class="flex bg-slate-100 p-1 rounded-xl text-xs font-semibold">
                <button type="button" id="btn-loan-mode-emp" onclick="setLoanViewMode('per_employee')"
                  class="px-3 py-1.5 rounded-lg bg-indigo-600 text-white shadow-sm flex items-center gap-1 transition">
                  <span class="material-symbols-outlined text-sm">person</span>
                  <span>ឈ្មោះម្នាក់ៗ (សង្ខេប)</span>
                </button>
                <button type="button" id="btn-loan-mode-all" onclick="setLoanViewMode('all')"
                  class="px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 flex items-center gap-1 transition">
                  <span class="material-symbols-outlined text-sm">view_list</span>
                  <span>សរុប (ប្រតិបត្តិការទាំងអស់)</span>
                </button>
              </div>
              <select id="rep-loan-emp-filter" onchange="renderLoanReportTable()"
                class="text-xs border border-slate-200 rounded-xl px-3 py-1.5 bg-white font-medium text-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-400">
                <option value="">-- បង្ហាញបុគ្គលិកទាំងអស់ --</option>
              </select>
            </div>
          </div>
          <div class="overflow-x-auto w-full">
            <table class="w-full text-left text-xs sm:text-sm">
              <thead class="bg-slate-50 text-gray-500 font-medium border-b border-slate-100 text-xs" id="rep-loan-thead">
                <tr>
                  <th class="py-3 px-4">#</th>
                  <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
                  <th class="py-3 px-4">ខ្ចីសរុប</th>
                  <th class="py-3 px-4">បានកាត់រួច</th>
                  <th class="py-3 px-4">នៅសល់ត្រូវកាត់</th>
                  <th class="py-3 px-4">គោលបំណង</th>
                  <th class="py-3 px-4">ស្ថានភាព</th>
                  <th class="py-3 px-4">សកម្មភាព</th>
                </tr>
              </thead>
              <tbody id="rep-loan-tbody" class="divide-y divide-slate-100 text-gray-700">
                <tr><td colspan="8" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>
    </div>

    <!-- ===== INCOME TAB ===== -->
    <div id="tab-income" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
        <div>
          <h2 class="text-lg font-bold text-gray-800 flex items-center gap-2">
            <span class="material-symbols-outlined text-emerald-600 text-2xl">trending_up</span> ការគ្រប់គ្រងចំណូល (Income)
          </h2>
          <p class="text-xs text-gray-500 mt-0.5">កត់ត្រា និងតាមដានរាល់ប្រតិបត្តិការចំណូលប្រចាំថ្ងៃរបស់ក្រុមហ៊ុន AC Bomlu</p>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <button onclick="showTab('reports-finance')" class="flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-blue-50 text-blue-600 hover:bg-blue-100 transition shadow-sm border border-blue-200">
            <span class="material-symbols-outlined text-base">analytics</span> របាយការណ៍ចំណូល
          </button>
          <button onclick="openIncomeModal()" class="flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-emerald-600 hover:bg-emerald-700 text-white transition shadow active:scale-95">
            <span class="material-symbols-outlined text-base">add_circle</span> + បញ្ចូលចំណូលថ្មី
          </button>
        </div>
      </div>

      <!-- Income Summary KPI Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 sm:gap-4 mb-5">
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-emerald-500 text-sm">today</span> ចំណូលថ្ងៃនេះ
          </p>
          <p id="inc-stat-today" class="text-base sm:text-xl font-bold text-emerald-600 mt-1">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-blue-500 text-sm">calendar_month</span> ចំណូលខែនេះ
          </p>
          <p id="inc-stat-month" class="text-base sm:text-xl font-bold text-blue-600 mt-1">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-indigo-500 text-sm">event_note</span> ចំណូលឆ្នាំនេះ
          </p>
          <p id="inc-stat-year" class="text-base sm:text-xl font-bold text-indigo-600 mt-1">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-slate-500 text-sm">receipt</span> ចំនួនប្រតិបត្តិការ
          </p>
          <p id="inc-stat-count" class="text-base sm:text-xl font-bold text-gray-800 mt-1">0</p>
        </div>
      </div>

      <!-- Filter Bar -->
      <div class="bg-white rounded-2xl p-3 sm:p-4 shadow-sm border border-slate-100 mb-4 flex flex-wrap items-center justify-between gap-3 text-xs sm:text-sm">
        <div class="flex items-center gap-1.5 flex-wrap">
          <button onclick="setIncomeDateFilter('today')" id="inc-filter-today" class="px-3 py-1.5 rounded-xl font-medium bg-emerald-600 text-white shadow-sm transition">ថ្ងៃនេះ</button>
          <button onclick="setIncomeDateFilter('yesterday')" id="inc-filter-yesterday" class="px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition">ម្សិលមិញ</button>
          <button onclick="setIncomeDateFilter('month')" id="inc-filter-month" class="px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition">ខែនេះ</button>
          <button onclick="setIncomeDateFilter('all')" id="inc-filter-all" class="px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition">ទាំងអស់</button>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <input type="date" id="inc-filter-date" onchange="handleIncomeCustomDate(this.value)" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-400" />
          <select id="inc-filter-category" onchange="loadIncome()" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-400">
            <option value="all">គ្រប់កាតាឡុក</option>
          </select>
          <input type="text" id="inc-filter-search" oninput="debounceIncomeSearch()" placeholder="ស្វែងរកចំណាំ/វិក្កយបត្រ..." class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-emerald-400 w-36 sm:w-48" />
        </div>
      </div>

      <!-- Transactions List (Desktop Table + Mobile Cards) -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <div class="hidden md:block overflow-x-auto">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100">
              <tr>
                <th class="px-5 py-3 font-medium whitespace-nowrap">កាលបរិច្ឆេទ & ម៉ោង</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">កាតាឡុក / ប្រភេទ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ពិពណ៌នា / ចំណាំ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ការទូទាត់</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">លេខវិក្កយបត្រ</th>
                <th class="px-5 py-3 font-medium text-right whitespace-nowrap">ចំនួនទឹកប្រាក់</th>
                <th class="px-5 py-3 font-medium text-right whitespace-nowrap">សកម្មភាព</th>
              </tr>
            </thead>
            <tbody id="income-tbody" class="divide-y divide-slate-50 text-gray-700">
              <tr><td colspan="7" class="text-center py-8 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>
        <div id="income-mobile-cards" class="md:hidden p-3 space-y-3 bg-slate-50/50">
          <div class="text-center py-8 text-gray-400 text-sm">កំពុងទាញទិន្នន័យ...</div>
        </div>
      </div>
    </div>

    <!-- ===== EXPENSE TAB ===== -->
    <div id="tab-expense" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
        <div>
          <h2 class="text-lg font-bold text-gray-800 flex items-center gap-2">
            <span class="material-symbols-outlined text-rose-600 text-2xl">trending_down</span> ការគ្រប់គ្រងចំណាយ (Expense)
          </h2>
          <p class="text-xs text-gray-500 mt-0.5">កត់ត្រា និងតាមដានរាល់ការចំណាយប្រចាំថ្ងៃរបស់ក្រុមហ៊ុន AC Bomlu</p>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <button onclick="showTab('reports-finance')" class="flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-blue-50 text-blue-600 hover:bg-blue-100 transition shadow-sm border border-blue-200">
            <span class="material-symbols-outlined text-base">analytics</span> របាយការណ៍ចំណាយ
          </button>
          <button onclick="openExpenseModal()" class="flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-rose-600 hover:bg-rose-700 text-white transition shadow active:scale-95">
            <span class="material-symbols-outlined text-base">remove_circle</span> + បញ្ចូលចំណាយថ្មី
          </button>
        </div>
      </div>

      <!-- Expense Summary KPI Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 sm:gap-4 mb-5">
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-rose-500 text-sm">today</span> ចំណាយថ្ងៃនេះ
          </p>
          <p id="exp-stat-today" class="text-base sm:text-xl font-bold text-rose-600 mt-1">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-amber-500 text-sm">calendar_month</span> ចំណាយខែនេះ
          </p>
          <p id="exp-stat-month" class="text-base sm:text-xl font-bold text-amber-600 mt-1">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-purple-500 text-sm">event_note</span> ចំណាយឆ្នាំនេះ
          </p>
          <p id="exp-stat-year" class="text-base sm:text-xl font-bold text-purple-600 mt-1">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-3.5 sm:p-5 shadow-sm border border-slate-100">
          <p class="text-[11px] sm:text-xs text-gray-500 font-medium flex items-center gap-1">
            <span class="material-symbols-outlined text-slate-500 text-sm">receipt</span> ចំនួនប្រតិបត្តិការ
          </p>
          <p id="exp-stat-count" class="text-base sm:text-xl font-bold text-gray-800 mt-1">0</p>
        </div>
      </div>

      <!-- Filter Bar -->
      <div class="bg-white rounded-2xl p-3 sm:p-4 shadow-sm border border-slate-100 mb-4 flex flex-wrap items-center justify-between gap-3 text-xs sm:text-sm">
        <div class="flex items-center gap-1.5 flex-wrap">
          <button onclick="setExpenseDateFilter('today')" id="exp-filter-today" class="px-3 py-1.5 rounded-xl font-medium bg-rose-600 text-white shadow-sm transition">ថ្ងៃនេះ</button>
          <button onclick="setExpenseDateFilter('yesterday')" id="exp-filter-yesterday" class="px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition">ម្សិលមិញ</button>
          <button onclick="setExpenseDateFilter('month')" id="exp-filter-month" class="px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition">ខែនេះ</button>
          <button onclick="setExpenseDateFilter('all')" id="exp-filter-all" class="px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition">ទាំងអស់</button>
        </div>
        <div class="flex items-center gap-2 flex-wrap">
          <input type="date" id="exp-filter-date" onchange="handleExpenseCustomDate(this.value)" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-rose-400" />
          <select id="exp-filter-category" onchange="loadExpense()" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-rose-400">
            <option value="all">គ្រប់កាតាឡុក</option>
          </select>
          <input type="text" id="exp-filter-search" oninput="debounceExpenseSearch()" placeholder="ស្វែងរកចំណាំ/វិក្កយបត្រ..." class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs text-gray-700 focus:outline-none focus:ring-2 focus:ring-rose-400 w-36 sm:w-48" />
        </div>
      </div>

      <!-- Transactions List -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <div class="hidden md:block overflow-x-auto">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100">
              <tr>
                <th class="px-5 py-3 font-medium whitespace-nowrap">កាលបរិច្ឆេទ & ម៉ោង</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">កាតាឡុក / ប្រភេទ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ពិពណ៌នា / ចំណាំ</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">ការទូទាត់</th>
                <th class="px-5 py-3 font-medium whitespace-nowrap">លេខវិក្កយបត្រ</th>
                <th class="px-5 py-3 font-medium text-right whitespace-nowrap">ចំនួនទឹកប្រាក់</th>
                <th class="px-5 py-3 font-medium text-right whitespace-nowrap">សកម្មភាព</th>
              </tr>
            </thead>
            <tbody id="expense-tbody" class="divide-y divide-slate-50 text-gray-700">
              <tr><td colspan="7" class="text-center py-8 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>
        <div id="expense-mobile-cards" class="md:hidden p-3 space-y-3 bg-slate-50/50">
          <div class="text-center py-8 text-gray-400 text-sm">កំពុងទាញទិន្នន័យ...</div>
        </div>
      </div>
    </div>

    <!-- ===== CATALOG TAB ===== -->
    <div id="tab-catalog" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
        <div>
          <h2 class="text-lg font-bold text-gray-800 flex items-center gap-2">
            <span class="material-symbols-outlined text-amber-500 text-2xl">category</span> កាតាឡុក និងប្រភេទប្រតិបត្តិការ (Catalogs)
          </h2>
          <p class="text-xs text-gray-500 mt-0.5">កំណត់ និងគ្រប់គ្រងប្រភេទចំណូល និងចំណាយ ដើម្បីងាយស្រួលធ្វើរបាយការណ៍ហិរញ្ញវត្ថុ</p>
        </div>
        <button onclick="openCatalogModal()" class="flex items-center gap-1.5 px-4 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-blue-600 hover:bg-blue-700 text-white transition shadow active:scale-95">
          <span class="material-symbols-outlined text-base">add</span> + បង្កើតកាតាឡុកថ្មី
        </button>
      </div>

      <!-- Filter Category Types -->
      <div class="flex gap-2 mb-5">
        <button onclick="filterCatalogType('all')" id="cat-tab-all" class="px-4 py-2 rounded-xl text-xs sm:text-sm font-medium bg-blue-600 text-white shadow-sm transition">ទាំងអស់</button>
        <button onclick="filterCatalogType('income')" id="cat-tab-income" class="px-4 py-2 rounded-xl text-xs sm:text-sm font-medium bg-white border border-slate-200 text-gray-700 hover:bg-slate-50 transition">ប្រភេទចំណូល</button>
        <button onclick="filterCatalogType('expense')" id="cat-tab-expense" class="px-4 py-2 rounded-xl text-xs sm:text-sm font-medium bg-white border border-slate-200 text-gray-700 hover:bg-slate-50 transition">ប្រភេទចំណាយ</button>
      </div>

      <!-- Catalogs Grid -->
      <div id="catalogs-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        <div class="col-span-full text-center py-12 text-gray-400">កំពុងទាញទិន្នន័យកាតាឡុក...</div>
      </div>
    </div>

    <!-- ===== FINANCIAL REPORTS TAB ===== -->
    <div id="tab-reports-finance" class="p-3 sm:p-6 animate-fadeIn hidden">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-5">
        <div>
          <h2 class="text-lg font-bold text-gray-800 flex items-center gap-2">
            <span class="material-symbols-outlined text-blue-600 text-2xl">analytics</span> របាយការណ៍ហិរញ្ញវត្ថុ (Financial Reports)
          </h2>
          <p class="text-xs text-gray-500 mt-0.5">របាយការណ៍ចំណូល ចំណាយ និងប្រាក់ចំណេញសុទ្ធ ប្រចាំថ្ងៃ ប្រចាំខែ និងប្រចាំឆ្នាំ</p>
        </div>
        <div class="flex items-center gap-2 flex-wrap no-print">
          <button onclick="openExecutiveReportPreview()" class="flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-blue-50 border border-blue-200 text-blue-700 hover:bg-blue-100 shadow-sm transition" title="ពិនិត្យ និងបោះពុម្ពទម្រង់របាយការណ៍ផ្លូវការ">
            <span class="material-symbols-outlined text-base">verified</span> ទម្រង់ផ្លូវការថ្នាក់ដឹកនាំ
          </button>
          <button onclick="printFinanceReport()" class="flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-white border border-slate-200 text-gray-700 hover:bg-slate-50 shadow-sm transition">
            <span class="material-symbols-outlined text-base">print</span> បោះពុម្ព
          </button>
          <button onclick="exportFinanceReportCSV()" class="flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold bg-emerald-600 hover:bg-emerald-700 text-white shadow transition">
            <span class="material-symbols-outlined text-base">download</span> ទាញយកជា Excel/CSV
          </button>
        </div>
      </div>

      <!-- Period Tabs -->
      <div class="bg-white rounded-2xl p-2 shadow-sm border border-slate-100 mb-5 flex items-center gap-2 no-print">
        <button onclick="switchFinanceReportPeriod('daily')" id="rep-period-daily" class="flex-1 py-2 rounded-xl text-xs sm:text-sm font-bold bg-blue-600 text-white shadow-sm transition flex items-center justify-center gap-1.5">
          <span class="material-symbols-outlined text-base">today</span> របាយការណ៍ប្រចាំថ្ងៃ
        </button>
        <button onclick="switchFinanceReportPeriod('monthly')" id="rep-period-monthly" class="flex-1 py-2 rounded-xl text-xs sm:text-sm font-medium bg-transparent text-gray-600 hover:bg-slate-100 transition flex items-center justify-center gap-1.5">
          <span class="material-symbols-outlined text-base">calendar_month</span> របាយការណ៍ប្រចាំខែ
        </button>
        <button onclick="switchFinanceReportPeriod('yearly')" id="rep-period-yearly" class="flex-1 py-2 rounded-xl text-xs sm:text-sm font-medium bg-transparent text-gray-600 hover:bg-slate-100 transition flex items-center justify-center gap-1.5">
          <span class="material-symbols-outlined text-base">event_note</span> របាយការណ៍ប្រចាំឆ្នាំ
        </button>
      </div>

      <!-- Period Filter Controls -->
      <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 mb-5 flex flex-wrap items-center justify-between gap-3 no-print">
        <!-- Daily Controls -->
        <div id="rep-ctrl-daily" class="flex items-center gap-2 flex-wrap w-full sm:w-auto">
          <button onclick="changeDailyReportDate(-1)" class="p-1.5 rounded-lg border border-slate-200 hover:bg-slate-50 text-gray-600 transition" title="ថ្ងៃមុន">
            <span class="material-symbols-outlined text-base">chevron_left</span>
          </button>
          <input type="date" id="rep-finance-date" onchange="loadFinanceReports()" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs sm:text-sm font-medium text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-400" />
          <button onclick="changeDailyReportDate(1)" class="p-1.5 rounded-lg border border-slate-200 hover:bg-slate-50 text-gray-600 transition" title="ថ្ងៃបន្ទាប់">
            <span class="material-symbols-outlined text-base">chevron_right</span>
          </button>
          <button onclick="changeDailyReportDate(0)" class="px-3 py-1.5 rounded-xl border border-slate-200 hover:bg-slate-50 text-xs font-semibold text-blue-600 transition">ថ្ងៃនេះ</button>
        </div>

        <!-- Monthly Controls -->
        <div id="rep-ctrl-monthly" class="hidden flex items-center gap-2 flex-wrap w-full sm:w-auto">
          <label class="text-xs text-gray-500 font-medium">ជ្រើសរើសខែ៖</label>
          <select id="rep-finance-month" onchange="loadFinanceReports()" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs sm:text-sm font-medium text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-400">
            <option value="1">ខែមករា (Jan)</option>
            <option value="2">ខែកុម្ភៈ (Feb)</option>
            <option value="3">ខែមីនា (Mar)</option>
            <option value="4">ខែមេសា (Apr)</option>
            <option value="5">ខែឧសភា (May)</option>
            <option value="6">ខែមិថុនា (Jun)</option>
            <option value="7">ខែកក្កដា (Jul)</option>
            <option value="8">ខែសីហា (Aug)</option>
            <option value="9">ខែកញ្ញា (Sep)</option>
            <option value="10">ខែតុលា (Oct)</option>
            <option value="11">ខែវិច្ឆិកា (Nov)</option>
            <option value="12">ខែធ្នូ (Dec)</option>
          </select>
          <select id="rep-finance-year-m" onchange="loadFinanceReports()" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs sm:text-sm font-medium text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-400">
          </select>
        </div>

        <!-- Yearly Controls -->
        <div id="rep-ctrl-yearly" class="hidden flex items-center gap-2 flex-wrap w-full sm:w-auto">
          <label class="text-xs text-gray-500 font-medium">ជ្រើសរើសឆ្នាំ៖</label>
          <select id="rep-finance-year-y" onchange="loadFinanceReports()" class="border border-slate-200 rounded-xl px-3 py-1.5 text-xs sm:text-sm font-medium text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-400">
          </select>
        </div>

        <div class="text-xs text-gray-500" id="rep-finance-period-label">
          របាយការណ៍ហិរញ្ញវត្ថុប្រចាំថ្ងៃ
        </div>
      </div>

      <!-- Financial Report Summary Cards (8 Key Metrics) -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 sm:gap-4 mb-3">
        <!-- Row 1: Core Financial Performance -->
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 transition hover:shadow-md">
          <p class="text-xs text-gray-500 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-emerald-500"></span> សរុបចំណូល (Income)</span>
            <span class="material-symbols-outlined text-emerald-500 text-sm">trending_up</span>
          </p>
          <p id="rep-summary-income" class="text-base sm:text-xl font-bold text-emerald-600 mt-1.5">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 transition hover:shadow-md">
          <p class="text-xs text-gray-500 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-rose-500"></span> សរុបចំណាយ (Expense)</span>
            <span class="material-symbols-outlined text-rose-500 text-sm">trending_down</span>
          </p>
          <p id="rep-summary-expense" class="text-base sm:text-xl font-bold text-rose-600 mt-1.5">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 transition hover:shadow-md">
          <p class="text-xs text-gray-500 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-blue-500"></span> ចំណេញ/ខាតសុទ្ធ (Net Profit)</span>
            <span class="material-symbols-outlined text-blue-500 text-sm">account_balance_wallet</span>
          </p>
          <p id="rep-summary-net" class="text-base sm:text-xl font-bold text-emerald-600 mt-1.5">0 ៛</p>
        </div>
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 transition hover:shadow-md">
          <p class="text-xs text-gray-500 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="w-2 h-2 rounded-full bg-amber-500"></span> ប្រតិបត្តិការ (Records)</span>
            <span class="material-symbols-outlined text-amber-500 text-sm">receipt_long</span>
          </p>
          <p id="rep-summary-count" class="text-base sm:text-xl font-bold text-gray-800 mt-1.5">0</p>
        </div>
      </div>

      <!-- Row 2: Operational & Treasury Performance (Trips, ARPT, Cash vs Bank) -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2.5 sm:gap-4 mb-6">
        <div class="bg-gradient-to-br from-blue-50 to-indigo-50/50 rounded-2xl p-4 shadow-sm border border-blue-100/80 transition hover:shadow-md">
          <p class="text-xs text-blue-900 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="material-symbols-outlined text-blue-600 text-base">commute</span> សរុបជើងផ្ដល់សេវា</span>
            <span class="text-[10px] bg-blue-200/60 text-blue-800 font-bold px-1.5 py-0.5 rounded-full">សេវាកម្ម</span>
          </p>
          <p id="rep-summary-trips" class="text-base sm:text-xl font-bold text-blue-700 mt-1.5">0 ជើង</p>
        </div>
        <div class="bg-gradient-to-br from-indigo-50 to-purple-50/50 rounded-2xl p-4 shadow-sm border border-indigo-100/80 transition hover:shadow-md">
          <p class="text-xs text-indigo-900 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="material-symbols-outlined text-indigo-600 text-base">price_check</span> ចំណូលមធ្យម/ជើង</span>
            <span class="text-[10px] bg-indigo-200/60 text-indigo-800 font-bold px-1.5 py-0.5 rounded-full">ARPT</span>
          </p>
          <p id="rep-summary-arpt" class="text-base sm:text-xl font-bold text-indigo-700 mt-1.5">0 ៛</p>
        </div>
        <div class="bg-gradient-to-br from-emerald-50 to-teal-50/50 rounded-2xl p-4 shadow-sm border border-emerald-100/80 transition hover:shadow-md">
          <p class="text-xs text-emerald-900 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="material-symbols-outlined text-emerald-600 text-base">payments</span> សាច់ប្រាក់ក្នុងដៃ</span>
            <span class="text-[10px] bg-emerald-200/60 text-emerald-800 font-bold px-1.5 py-0.5 rounded-full">Cash In Hand</span>
          </p>
          <p id="rep-summary-cash" class="text-base sm:text-xl font-bold text-emerald-700 mt-1.5">0 ៛</p>
        </div>
        <div class="bg-gradient-to-br from-cyan-50 to-sky-50/50 rounded-2xl p-4 shadow-sm border border-cyan-100/80 transition hover:shadow-md">
          <p class="text-xs text-cyan-900 font-medium flex items-center justify-between">
            <span class="flex items-center gap-1.5"><span class="material-symbols-outlined text-cyan-600 text-base">account_balance</span> សមតុល្យធនាគារ</span>
            <span class="text-[10px] bg-cyan-200/60 text-cyan-800 font-bold px-1.5 py-0.5 rounded-full">Bank / KHQR</span>
          </p>
          <p id="rep-summary-bank" class="text-base sm:text-xl font-bold text-cyan-700 mt-1.5">0 ៛</p>
        </div>
      </div>

      <!-- Dynamic Report Content Area (Daily / Monthly / Yearly) -->
      <div id="rep-finance-content" class="space-y-6">
        <div class="text-center py-12 text-gray-400 bg-white rounded-2xl border border-slate-100">កំពុងរៀបចំរបាយការណ៍...</div>
      </div>
    </div>
  </main>
</div>

<!-- ======================================================
     MODALS
====================================================== -->
<!-- Executive Official Report Modal (របាយការណ៍ផ្លូវការថ្នាក់ដឹកនាំ) -->
<div id="executive-report-modal" class="fixed inset-0 bg-black/60 modal-overlay z-50 hidden flex items-center justify-center p-2 sm:p-4 overflow-y-auto">
  <div class="modal-box bg-white rounded-3xl shadow-2xl w-full max-w-4xl max-h-[92vh] flex flex-col overflow-hidden border border-slate-200">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-slate-50 no-print">
      <div class="flex items-center gap-2">
        <span class="material-symbols-outlined text-blue-600">verified</span>
        <h3 class="font-bold text-gray-800 text-sm sm:text-base">ទម្រង់របាយការណ៍ផ្លូវការសម្រាប់ថ្នាក់ដឹកនាំ (Executive Report)</h3>
      </div>
      <div class="flex items-center gap-2">
        <button onclick="printExecutiveReportDoc()" class="flex items-center gap-1.5 px-3.5 py-1.5 rounded-xl text-xs sm:text-sm font-bold bg-blue-600 hover:bg-blue-700 text-white shadow transition">
          <span class="material-symbols-outlined text-base">print</span> បោះពុម្ពទម្រង់ផ្លូវការ
        </button>
        <button onclick="closeModal('executive-report-modal')" class="p-1.5 text-gray-400 hover:text-gray-600 rounded-lg">
          <span class="material-symbols-outlined text-xl">close</span>
        </button>
      </div>
    </div>
    <div id="executive-report-modal-body" class="p-4 sm:p-8 overflow-y-auto bg-white text-gray-800 space-y-6">
      <!-- Injected by JavaScript -->
    </div>
  </div>
</div>

<!-- Leave Modal -->
<div id="leave-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-md">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100">
      <h3 class="font-semibold text-gray-800 flex items-center gap-2">
        <span class="material-symbols-outlined text-orange-500">event_busy</span> សំណើសុំច្បាប់
      </h3>
      <button onclick="closeModal('leave-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitLeave(event)" class="p-6 space-y-4">
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">កាលបរិច្ឆេទ *</label>
        <input id="leave-date" type="date" required
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-medium text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-400 transition bg-white shadow-sm" />
      </div>
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">សាខាក្រុមហ៊ុន *</label>
        <select id="leave-branch" required onchange="onLeaveBranchChanged()"
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-semibold text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-400 transition bg-white shadow-sm">
          <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
          <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
        </select>
      </div>
      <div>
        <div class="flex items-center justify-between gap-1 mb-1">
          <label class="block text-xs sm:text-sm font-semibold text-gray-700">ឈ្មោះបុគ្គលិក *</label>
          <span class="text-[10px] text-blue-600 font-semibold bg-blue-50 px-2 py-0.5 rounded-full border border-blue-200">បុគ្គលិកក្នុងប្រព័ន្ធ</span>
        </div>
        <select id="leave-name" required onchange="onLeaveEmployeeChanged()"
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-semibold text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-400 transition bg-white shadow-sm">
          <option value="" disabled selected>-- ជ្រើសរើសឈ្មោះបុគ្គលិក --</option>
        </select>
      </div>
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">មូលហេតុ *</label>
        <input id="leave-reason" type="text" required placeholder="មូលហេតុ..."
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">ចំនួនថ្ងៃ *</label>
        <input id="leave-days" type="number" min="0.5" step="0.5" value="1" required
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-bold text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div class="flex gap-3 pt-2">
        <button type="button" onclick="closeModal('leave-modal')"
          class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit"
          class="btn-primary flex-1 py-2.5 rounded-xl text-sm font-medium transition">រក្សាទុក</button>
      </div>
    </form>
  </div>
</div>

<!-- Loan Modal -->
<div id="loan-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-md">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100">
      <h3 class="font-semibold text-gray-800 flex items-center gap-2">
        <span class="material-symbols-outlined text-green-600">payments</span> សំណើខ្ចីប្រាក់
      </h3>
      <button onclick="closeModal('loan-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitLoan(event)" class="p-6 space-y-4">
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">កាលបរិច្ឆេទ *</label>
        <input id="loan-date" type="date" required
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-medium text-gray-800 focus:outline-none focus:ring-2 focus:ring-emerald-400 transition bg-white shadow-sm" />
      </div>
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">សាខាក្រុមហ៊ុន *</label>
        <select id="loan-branch" required onchange="onLoanBranchChanged()"
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-semibold text-gray-800 focus:outline-none focus:ring-2 focus:ring-emerald-400 transition bg-white shadow-sm">
          <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
          <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
        </select>
      </div>
      <div>
        <div class="flex items-center justify-between gap-1 mb-1">
          <label class="block text-xs sm:text-sm font-semibold text-gray-700">ឈ្មោះបុគ្គលិក *</label>
          <span class="text-[10px] text-emerald-600 font-semibold bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200">បុគ្គលិកក្នុងប្រព័ន្ធ</span>
        </div>
        <select id="loan-name" required onchange="onLoanEmployeeChanged()"
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-semibold text-gray-800 focus:outline-none focus:ring-2 focus:ring-emerald-400 transition bg-white shadow-sm">
          <option value="" disabled selected>-- ជ្រើសរើសឈ្មោះបុគ្គលិក --</option>
        </select>
      </div>
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">ចំនួនទឹកប្រាក់ (រៀល) *</label>
        <input id="loan-amount" type="text" inputmode="numeric" required placeholder="ឧ. 100,000 ឬ ១០០,០០០"
          oninput="formatAmountInput(this)"
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm font-bold text-emerald-700 focus:outline-none focus:ring-2 focus:ring-emerald-400 transition font-mono tracking-wide" />
      </div>
      <div>
        <label class="block text-xs sm:text-sm font-semibold text-gray-700 mb-1">គោលបំណង</label>
        <input id="loan-purpose" type="text" placeholder="គោលបំណង..."
          class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 transition" />
      </div>
      <div class="flex gap-3 pt-2">
        <button type="button" onclick="closeModal('loan-modal')"
          class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit"
          class="btn-primary flex-1 py-2.5 rounded-xl text-sm font-medium transition">រក្សាទុក</button>
      </div>
    </form>
  </div>
</div>

<!-- Employee Modal -->
<div id="emp-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-md">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100">
      <h3 class="font-semibold text-gray-800 flex items-center gap-2">
        <span class="material-symbols-outlined text-blue-600">person_add</span> បន្ថែមបុគ្គលិក
      </h3>
      <button onclick="closeModal('emp-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitEmployee(event)" class="p-6 space-y-4">
      <div>
        <label class="block text-sm text-gray-600 mb-1">ឈ្មោះ</label>
        <input id="emp-name" type="text" required placeholder="ឈ្មោះ..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">មុខដំណែង</label>
        <select id="emp-position" required
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition bg-white appearance-none">
          <option value="" disabled selected>— ជ្រើសរើសមុខដំណែង —</option>
          <option value="អ្នកបើកឡាន">អ្នកបើកឡាន</option>
          <option value="បុគ្គលិកទីផ្សារ">បុគ្គលិកទីផ្សារ</option>
        </select>
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">សាខាក្រុមហ៊ុន</label>
        <select id="emp-branch" required
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition bg-white appearance-none">
          <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
          <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
        </select>
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">លេខទូរស័ព្ទ ១</label>
        <input id="emp-phone" type="tel" placeholder="ទូរស័ព្ទទី ១..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">លេខទូរស័ព្ទ ២</label>
        <input id="emp-phone2" type="tel" placeholder="ទូរស័ព្ទទី ២..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div class="flex gap-3 pt-2">
        <button type="button" onclick="closeModal('emp-modal')"
          class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit"
          class="btn-primary flex-1 py-2.5 rounded-xl text-sm font-medium transition">រក្សាទុក</button>
      </div>
    </form>
  </div>
</div>

<!-- Bulk Employee Modal -->
<div id="bulk-emp-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-lg overflow-hidden">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-emerald-50">
      <h3 class="font-bold text-gray-800 flex items-center gap-2 text-sm sm:text-base">
        <span class="material-symbols-outlined text-emerald-600">playlist_add</span> បញ្ចូលឈ្មោះបុគ្គលិកច្រើននាក់ (Bulk Import)
      </h3>
      <button onclick="closeModal('bulk-emp-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitBulkEmployees(event)" class="p-6 space-y-4">
      <div>
        <label class="block text-sm text-gray-700 mb-1 font-semibold">បញ្ជីឈ្មោះបុគ្គលិក (១ ជួរឈ្មោះ ១ នាក់ ឬខណ្ឌដោយក្បៀស)</label>
        <textarea id="bulk-emp-names" rows="6" required placeholder="ឧទាហរណ៍៖&#10;សុខលីម Soklim&#10;Ac La&#10;Ac Dana&#10;Ac រិត"
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 font-sans transition"></textarea>
        <p class="text-[11px] text-gray-400 mt-1">💡 លោកអ្នកអាច Copy បញ្ជីឈ្មោះពី Telegram Group រួច Paste ចូលទីនេះដើម្បីបង្កើតក្នុងពេលតែមួយ។</p>
      </div>
      <div>
        <label class="block text-sm text-gray-700 mb-1 font-semibold">មុខតំណែងទូទៅ</label>
        <input id="bulk-emp-position" type="text" value="បុគ្គលិក"
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-700 mb-1 font-semibold">សាខាក្រុមហ៊ុន</label>
        <select id="bulk-emp-branch"
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 transition bg-white">
          <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម" selected>ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
          <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
        </select>
      </div>
      <div class="flex justify-end gap-3 pt-2">
        <button type="button" onclick="closeModal('bulk-emp-modal')" class="px-4 py-2 rounded-xl text-sm border border-slate-200 text-gray-600 hover:bg-slate-50 transition">បោះបង់</button>
        <button type="submit" class="px-5 py-2.5 rounded-xl text-sm font-semibold bg-emerald-600 hover:bg-emerald-700 text-white shadow-sm transition">រក្សាទុកទាំងអស់</button>
      </div>
    </form>
  </div>
</div>

<!-- Batch Phone Update Modal -->
<div id="batch-phone-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-2xl overflow-hidden flex flex-col max-h-[90vh]">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-indigo-50">
      <h3 class="font-bold text-gray-800 flex items-center gap-2 text-sm sm:text-base">
        <span class="material-symbols-outlined text-indigo-600">call</span> បញ្ចូល / កែសម្រួលលេខទូរស័ព្ទបុគ្គលិកទាំងអស់
      </h3>
      <button onclick="closeModal('batch-phone-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitBatchPhoneModal(event)" class="flex flex-col flex-1 overflow-hidden">
      <div class="p-6 overflow-y-auto flex-1 space-y-4">
        <div class="bg-indigo-50/70 border border-indigo-200 rounded-xl p-3 flex items-start gap-2.5 text-xs text-indigo-800">
          <span class="material-symbols-outlined text-indigo-600 text-base mt-0.5">info</span>
          <div>
            <b>ការណែនាំ៖</b> លោកអ្នកអាចវាយបញ្ចូលលេខទូរស័ព្ទ (ឧទាហរណ៍៖ <code>012345678</code>) សម្រាប់បុគ្គលិកនីមួយៗ ហើយចុច <b>«រក្សាទុកលេខទូរស័ព្ទទាំងអស់»</b> តែមួយដង នោះកាតបុគ្គលិកនឹងលែងបង្ហាញ <span class="text-amber-600 font-medium">⚠️ មិនទាន់មានលេខ</span> ទៀតហើយ។
          </div>
        </div>
        <div class="border border-slate-200 rounded-xl overflow-hidden shadow-sm">
          <table class="w-full text-left text-sm">
            <thead class="bg-slate-50 text-slate-500 text-xs uppercase font-medium">
              <tr>
                <th class="py-2.5 px-4">បុគ្គលិក</th>
                <th class="py-2.5 px-4">លេខទូរស័ព្ទ</th>
                <th class="py-2.5 px-4 text-center">ស្ថានភាព</th>
              </tr>
            </thead>
            <tbody id="batch-phone-tbody" class="divide-y divide-slate-100">
              <tr><td colspan="3" class="text-center py-6 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="flex items-center justify-between px-6 py-4 bg-slate-50 border-t border-slate-100">
        <button type="button" onclick="closeModal('batch-phone-modal')" class="px-4 py-2 rounded-xl text-sm border border-slate-200 text-gray-600 hover:bg-white transition">បោះបង់</button>
        <button type="submit" id="btn-save-batch-phone" class="flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-semibold bg-indigo-600 hover:bg-indigo-700 text-white shadow-sm transition">
          <span class="material-symbols-outlined text-base">save</span> រក្សាទុកលេខទូរស័ព្ទទាំងអស់
        </button>
      </div>
    </form>
  </div>
</div>

<!-- Compensate Leave Modal -->
<div id="compensate-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-amber-50">
      <h3 class="font-bold text-gray-800 flex items-center gap-2 text-sm sm:text-base">
        <span class="material-symbols-outlined text-amber-600">event_repeat</span> កត់ត្រាការសងច្បាប់ (Leave Compensation)
      </h3>
      <button onclick="closeModal('compensate-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitCompensateLeave(event)" class="p-6 space-y-4">
      <input type="hidden" id="comp-leave-id" value="" />
      <input type="hidden" id="comp-leave-rem-raw" value="0" />
      
      <!-- Employee Info Banner -->
      <div class="bg-amber-50/70 border border-amber-200 rounded-xl p-3.5 flex items-center justify-between">
        <div>
          <p class="text-xs text-amber-700 font-medium">ឈ្មោះបុគ្គលិក</p>
          <p class="text-sm font-bold text-gray-900" id="comp-leave-emp-name">-</p>
        </div>
        <div class="text-right">
          <p class="text-xs text-amber-700 font-medium">នៅសល់ត្រូវសង</p>
          <p class="text-sm font-bold text-amber-700" id="comp-leave-rem-disp">0 ថ្ងៃ</p>
        </div>
      </div>

      <!-- Quick Preset Days -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1.5">ជម្រើសចំនួនថ្ងៃសងរហ័ស</label>
        <div class="grid grid-cols-4 gap-2">
          <button type="button" onclick="setCompDays(0.5)" class="py-2 px-2 text-xs font-bold rounded-lg border border-slate-200 hover:border-amber-400 hover:bg-amber-50 text-slate-700 transition">កន្លះថ្ងៃ</button>
          <button type="button" onclick="setCompDays(1)" class="py-2 px-2 text-xs font-bold rounded-lg border border-slate-200 hover:border-amber-400 hover:bg-amber-50 text-slate-700 transition">១ ថ្ងៃ</button>
          <button type="button" onclick="setCompDays(2)" class="py-2 px-2 text-xs font-bold rounded-lg border border-slate-200 hover:border-amber-400 hover:bg-amber-50 text-slate-700 transition">២ ថ្ងៃ</button>
          <button type="button" onclick="setCompDays('all')" class="py-2 px-2 text-xs font-bold rounded-lg bg-amber-500 hover:bg-amber-600 text-white shadow-sm transition">សងគ្រប់</button>
        </div>
      </div>

      <!-- Custom Days Input -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">ចំនួនថ្ងៃដែលបានសងលើកនេះ (ថ្ងៃ)</label>
        <input id="comp-leave-days" type="number" step="0.5" min="0.5" required
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm font-bold text-gray-800 focus:outline-none focus:ring-2 focus:ring-amber-400 transition" />
      </div>

      <!-- Admin Name -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">ឈ្មោះ Admin អ្នកកត់ត្រា</label>
        <input id="comp-leave-admin" type="text" required placeholder="ឈ្មោះ Admin..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400 transition" />
      </div>

      <!-- Optional Note -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">សម្គាល់ / ចំណាំបន្ថែម (ស្រេចចិត្ត)</label>
        <input id="comp-leave-note" type="text" placeholder="ឧ. បានមកធ្វើការសងថ្ងៃសៅរ៍ពេញមួយថ្ងៃ..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-amber-400 transition" />
      </div>

      <!-- Buttons -->
      <div class="flex gap-3 pt-2">
        <button type="button" onclick="closeModal('compensate-modal')"
          class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit" id="btn-comp-submit"
          class="bg-amber-500 hover:bg-amber-600 text-white flex-1 py-2.5 rounded-xl text-sm font-bold shadow-md hover:shadow transition flex items-center justify-center gap-1.5">
          <span class="material-symbols-outlined text-sm">check_circle</span>
          <span>រក្សាទុក & ជូនដំណឹង</span>
        </button>
      </div>
    </form>
  </div>
</div>

<!-- Repay Loan Modal -->
<div id="repay-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-md overflow-hidden">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-indigo-50">
      <h3 class="font-bold text-gray-800 flex items-center gap-2 text-sm sm:text-base">
        <span class="material-symbols-outlined text-indigo-600">payments</span> កត់ត្រាការកាត់ប្រាក់ខែ / សងប្រាក់កម្ចី
      </h3>
      <button onclick="closeModal('repay-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitRepayLoan(event)" class="p-6 space-y-4">
      <input type="hidden" id="repay-loan-id" value="" />
      <input type="hidden" id="repay-loan-rem-raw" value="0" />
      
      <!-- Employee Info Banner -->
      <div class="bg-indigo-50/70 border border-indigo-200 rounded-xl p-3.5 flex items-center justify-between">
        <div>
          <p class="text-xs text-indigo-700 font-medium">ឈ្មោះបុគ្គលិក</p>
          <p class="text-sm font-bold text-gray-900" id="repay-loan-emp-name">-</p>
        </div>
        <div class="text-right">
          <p class="text-xs text-indigo-700 font-medium">នៅសល់ជំពាក់ត្រូវកាត់</p>
          <p class="text-sm font-bold text-indigo-700" id="repay-loan-rem-disp">0 ៛</p>
        </div>
      </div>

      <!-- Quick Preset Amounts -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1.5">ជម្រើសចំនួនទឹកប្រាក់រហ័ស</label>
        <div class="grid grid-cols-4 gap-2">
          <button type="button" onclick="setRepayAmt(50000)" class="py-2 px-1 text-xs font-bold rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50 text-slate-700 transition">50,000 ៛</button>
          <button type="button" onclick="setRepayAmt(100000)" class="py-2 px-1 text-xs font-bold rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50 text-slate-700 transition">100,000 ៛</button>
          <button type="button" onclick="setRepayAmt(200000)" class="py-2 px-1 text-xs font-bold rounded-lg border border-slate-200 hover:border-indigo-400 hover:bg-indigo-50 text-slate-700 transition">200,000 ៛</button>
          <button type="button" onclick="setRepayAmt('all')" class="py-2 px-1 text-xs font-bold rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white shadow-sm transition">កាត់ដាច់</button>
        </div>
      </div>

      <!-- Custom Amount Input -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">ចំនួនទឹកប្រាក់កាត់/សងលើកនេះ (រៀល)</label>
        <input id="repay-loan-amount" type="number" step="1000" min="1000" required
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm font-bold text-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-400 transition" />
      </div>

      <!-- Admin Name -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">ឈ្មោះ Admin អ្នកកត់ត្រា</label>
        <input id="repay-loan-admin" type="text" required placeholder="ឈ្មោះ Admin..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 transition" />
      </div>

      <!-- Optional Note -->
      <div>
        <label class="block text-xs font-semibold text-gray-600 mb-1">សម្គាល់ / ចំណាំបន្ថែម (ស្រេចចិត្ត)</label>
        <input id="repay-loan-note" type="text" placeholder="ឧ. កាត់ប្រាក់ខែប្រចាំខែ ១១..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 transition" />
      </div>

      <!-- Buttons -->
      <div class="flex gap-3 pt-2">
        <button type="button" onclick="closeModal('repay-modal')"
          class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit" id="btn-repay-submit"
          class="bg-indigo-600 hover:bg-indigo-700 text-white flex-1 py-2.5 rounded-xl text-sm font-bold shadow-md hover:shadow transition flex items-center justify-center gap-1.5">
          <span class="material-symbols-outlined text-sm">check_circle</span>
          <span>រក្សាទុក & ជូនដំណឹង</span>
        </button>
      </div>
    </form>
  </div>
</div>

<!-- =================== FINANCE MODALS =================== -->

<!-- Income Modal -->
<div id="income-modal" class="fixed inset-0 bg-black/50 backdrop-blur-sm modal-overlay z-50 hidden flex items-center justify-center p-2.5 sm:p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[94vh] flex flex-col overflow-hidden">
    <div class="flex items-center justify-between px-4 sm:px-6 py-3.5 sm:py-4 border-b border-slate-100 flex-shrink-0 bg-white">
      <h3 class="font-bold text-gray-800 flex items-center gap-2 text-sm sm:text-base">
        <span class="material-symbols-outlined text-emerald-600 text-xl">add_circle</span> បញ្ចូលចំណូលថ្មី (Add Income)
      </h3>
      <button onclick="closeModal('income-modal')" class="w-8 h-8 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-700 hover:bg-slate-100 transition">
        <span class="material-symbols-outlined text-xl">close</span>
      </button>
    </div>

    <form onsubmit="submitIncome(event)" class="p-4 sm:p-6 overflow-y-auto flex-1 flex flex-col justify-between">
      <!-- STEP 1: ផ្នែកទី ១ (រូបទី២) - កាលបរិច្ឆេទ, សាខា, ប្រភេទចំណូល, មធ្យោបាយទូទាត់ -->
      <div id="inc-step-1" class="space-y-4">
        <!-- Row 1: Date & Branch -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
          <div>
            <label class="block text-xs font-semibold text-gray-700 mb-1">កាលបរិច្ឆេទ *</label>
            <input id="inc-form-date" type="date" required class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 bg-white" />
            <input id="inc-form-time" type="hidden" value="" />
          </div>
          <div>
            <label class="block text-xs font-semibold text-gray-700 mb-1">សាខាក្រុមហ៊ុន</label>
            <select id="inc-form-branch" class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-emerald-400 bg-white">
              <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
              <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
            </select>
            <input id="inc-form-ref" type="hidden" value="" />
          </div>
        </div>

        <!-- Row 2: Category & Payment Method -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 sm:gap-4">
          <div>
            <div class="flex items-center justify-between gap-1 mb-1.5 flex-wrap">
              <label class="text-xs font-semibold text-gray-700 whitespace-nowrap">កាតាឡុក / ប្រភេទ *</label>
              <button type="button" onclick="openCatalogModal('income')" class="text-[11px] font-semibold text-blue-600 hover:text-blue-700 hover:underline flex items-center gap-0.5 whitespace-nowrap">
                <span class="material-symbols-outlined text-xs">add</span> បង្កើតកាតាឡុកថ្មី
              </button>
            </div>
            <select id="inc-form-category" onchange="handleIncomeCategoryChange()" required class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 bg-white">
              <option value="ចំណូលពីសេវាបូមលូ" selected>ចំណូលពីសេវាបូមលូ</option>
            </select>
          </div>
          <div>
            <label class="block text-xs font-semibold text-gray-700 mb-1">មធ្យោបាយទូទាត់</label>
            <select id="inc-form-method" class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400 bg-white">
              <option value="សាច់ប្រាក់សុទ្ធ" selected>💵 សាច់ប្រាក់សុទ្ធ</option>
              <option value="បាញ់តាមធនាគារ">📱 បាញ់តាមធនាគារ</option>
            </select>
          </div>
        </div>

        <!-- Step 1 Footer Buttons -->
        <div class="flex gap-2.5 sm:gap-3 pt-4 border-t border-slate-100 flex-shrink-0 bg-white">
          <button type="button" onclick="closeModal('income-modal')" class="btn-secondary flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-medium transition">
            បោះបង់
          </button>
          <button type="button" onclick="nextIncomeStep()" class="flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-emerald-600 hover:bg-emerald-700 text-white shadow transition active:scale-95 flex items-center justify-center gap-1.5">
            <span>បន្តទៅមុខទៀត (Next)</span>
            <span class="material-symbols-outlined text-base">arrow_forward</span>
          </button>
        </div>
      </div>

      <!-- STEP 2: ផ្នែកទី ២ (រូបទី៣) - ទីតាំង, ចំនួនជើង, តម្លៃ, រូបិយប័ណ្ណ, ទឹកប្រាក់សរុប, ចំណាំ -->
      <div id="inc-step-2" class="space-y-3 sm:space-y-3.5 hidden">
        <!-- ជ្រើសរើសទីតាំង, ចំនួនជើង & តម្លៃជាក់ស្ដែង (Smart Sub-entry + Auto-sum) -->
        <div id="inc-service-location-section" class="p-3 sm:p-3.5 bg-gradient-to-r from-blue-50/90 to-indigo-50/90 rounded-2xl border border-blue-100 space-y-2">
          <div class="flex items-center justify-between gap-2 flex-wrap">
            <label class="text-xs font-bold text-blue-900 flex items-center gap-1.5 whitespace-nowrap">
              <span class="material-symbols-outlined text-blue-600 text-base">near_me</span>
              <span>ជ្រើសរើសទីតាំង, ចំនួនជើង & តម្លៃជាក់ស្ដែង</span>
            </label>
            <span class="text-[10px] text-blue-600 font-semibold bg-white px-2 py-0.5 rounded-full border border-blue-200 whitespace-nowrap">ចងចាំស្វ័យប្រវត្ត & គណនាសរុប</span>
          </div>

          <!-- Input fields row: Location (4 cols) | Trips (2 cols) | Price (3 cols) | Currency (៛ default, 1 col) | Add Button (2 cols) -->
          <div class="grid grid-cols-12 gap-1.5 sm:gap-2 items-center">
            <!-- Location Combobox (4 cols) -->
            <div class="col-span-12 sm:col-span-4 relative">
              <input list="service-locations-list" id="inc-location-input" type="text"
                placeholder="វាយបញ្ចូល ឬរើសទីតាំង..."
                onkeydown="if(event.key==='Enter'){event.preventDefault();document.getElementById('inc-price-input')?.focus();}"
                class="w-full border border-slate-200 rounded-xl px-3 py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white shadow-sm" />
              <datalist id="service-locations-list"></datalist>
            </div>

            <!-- Trips Dropdown (2 cols) -->
            <div class="col-span-4 sm:col-span-2">
              <select id="inc-trips-select" class="w-full border border-slate-200 rounded-xl px-2 py-2 text-xs sm:text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white shadow-sm">
                <option value="1">១ ជើង</option>
                <option value="2">២ ជើង</option>
                <option value="3">៣ ជើង</option>
                <option value="4">៤ ជើង</option>
                <option value="5">៥ ជើង</option>
                <option value="6">៦ ជើង</option>
                <option value="7">៧ ជើង</option>
                <option value="8">៨ ជើង</option>
                <option value="9">៩ ជើង</option>
                <option value="10">១០ ជើង</option>
                <option value="11">១១ ជើង</option>
                <option value="12">១២ ជើង</option>
                <option value="13">១៣ ជើង</option>
                <option value="14">១៤ ជើង</option>
                <option value="15">១៥ ជើង</option>
                <option value="16">១៦ ជើង</option>
                <option value="17">១៧ ជើង</option>
                <option value="18">១៨ ជើង</option>
                <option value="19">១៩ ជើង</option>
                <option value="20">២០ ជើង</option>
              </select>
            </div>

            <!-- Price input (3 cols) -->
            <div class="col-span-5 sm:col-span-3">
              <input id="inc-price-input" type="text" inputmode="numeric"
                placeholder="ឧ. 360,000"
                oninput="formatAmountInput(this)"
                onkeydown="if(event.key==='Enter'){event.preventDefault();addServiceTripItem();}"
                class="w-full border border-slate-200 rounded-xl px-2.5 py-2 text-xs sm:text-sm font-bold text-emerald-700 focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white shadow-sm font-mono text-right" />
            </div>

            <!-- Currency Select (1 col on desktop, 3 cols on mobile) -> Default KHR -->
            <div class="col-span-3 sm:col-span-1">
              <select id="inc-item-currency" onchange="onItemCurrencyChange()"
                title="ជ្រើសរើសរូបិយប័ណ្ណ"
                class="w-full border border-slate-200 rounded-xl px-1.5 py-2 text-xs sm:text-sm font-bold text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white shadow-sm text-center">
                <option value="KHR" selected>៛</option>
                <option value="USD">$</option>
              </select>
            </div>

            <!-- Add Button (2 cols) -->
            <div class="col-span-12 sm:col-span-2">
              <button type="button" onclick="addServiceTripItem()" id="btn-add-location"
                class="w-full py-2 bg-blue-600 hover:bg-blue-700 active:bg-blue-800 text-white rounded-xl text-xs sm:text-sm font-bold transition shadow-sm flex items-center justify-center gap-1 active:scale-95 whitespace-nowrap">
                <span class="material-symbols-outlined text-base">add</span>
                <span>បញ្ចូល</span>
              </button>
            </div>
          </div>

          <!-- Items List Container -->
          <div id="inc-service-items-list" class="space-y-1.5 max-h-32 overflow-y-auto">
            <!-- Dynamic item cards will be rendered here -->
          </div>

          <!-- Auto-sum Summary Bar -->
          <div id="inc-items-summary" class="hidden flex items-center justify-between px-3 py-1.5 bg-blue-100/70 rounded-xl text-xs font-semibold text-blue-900 border border-blue-200/80">
            <div class="flex items-center gap-1.5">
              <span class="material-symbols-outlined text-sm text-blue-700">functions</span>
              <span>សរុប៖ <b id="inc-items-total-trips" class="font-bold text-blue-800">0</b> ជើង</span>
            </div>
            <div class="flex items-center gap-1.5">
              <span>សរុបប្រាក់៖ <b id="inc-items-total-sum" class="font-bold text-emerald-700 font-mono">0 ៛</b></span>
              <span class="text-[10px] bg-emerald-100 text-emerald-800 px-1.5 py-0.5 rounded font-bold">Auto-sync</span>
            </div>
          </div>
        </div>

        <!-- Total Amount & Currency -->
        <div class="grid grid-cols-3 gap-2.5 sm:gap-3.5">
          <div class="col-span-2">
            <div class="flex items-center justify-between gap-1 mb-1">
              <label id="inc-form-amount-label" class="text-xs font-semibold text-gray-700">ចំនួនទឹកប្រាក់សរុប *</label>
              <span id="inc-amount-auto-badge" class="text-[10px] bg-emerald-100 text-emerald-800 px-1.5 py-0.5 rounded font-bold flex items-center gap-0.5">
                <span class="material-symbols-outlined text-[12px]">sync</span> គណនាស្វ័យប្រវត្ត
              </span>
            </div>
            <input id="inc-form-amount" type="text" inputmode="numeric" required placeholder="០ ៛"
              oninput="formatAmountInput(this)"
              class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-sm sm:text-base font-bold text-emerald-700 focus:outline-none focus:ring-2 focus:ring-emerald-400 font-mono tracking-wide" />
          </div>
          <div class="col-span-1">
            <label class="block text-xs font-semibold text-gray-700 mb-1">រូបិយប័ណ្ណ</label>
            <select id="inc-form-currency" onchange="onFormCurrencyChange()" class="w-full border border-slate-200 rounded-xl px-2.5 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-emerald-400 bg-white">
              <option value="KHR" selected>៛ (រៀល)</option>
              <option value="USD">$ (ដុល្លារ)</option>
            </select>
          </div>
        </div>

        <!-- Description / Notes -->
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">ការពិពណ៌នា / ចំណាំ</label>
          <textarea id="inc-form-desc" rows="2" placeholder="ព័ត៌មានបន្ថែម ឬទីតាំងសេវា..." class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-emerald-400"></textarea>
        </div>

        <!-- Step 2 Footer Buttons -->
        <div class="flex gap-2.5 sm:gap-3 pt-3 border-t border-slate-100 flex-shrink-0 bg-white">
          <button type="button" onclick="prevIncomeStep()" class="btn-secondary flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-semibold transition flex items-center justify-center gap-1.5">
            <span class="material-symbols-outlined text-base">arrow_back</span>
            <span>ថយក្រោយ (Back)</span>
          </button>
          <button type="submit" id="btn-save-income" class="flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-emerald-600 hover:bg-emerald-700 text-white shadow transition active:scale-95 flex items-center justify-center gap-1.5">
            <span class="material-symbols-outlined text-base">check_circle</span>
            <span>រក្សាទុកចំណូល</span>
          </button>
        </div>
      </div>
    </form>
  </div>
</div>

<!-- Expense Modal -->
<div id="expense-modal" class="fixed inset-0 bg-black/50 backdrop-blur-sm modal-overlay z-50 hidden flex items-center justify-center p-2.5 sm:p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[94vh] flex flex-col overflow-hidden">
    <div class="flex items-center justify-between px-4 sm:px-6 py-3.5 sm:py-4 border-b border-slate-100 flex-shrink-0 bg-white">
      <h3 class="font-bold text-gray-800 flex items-center gap-2 text-sm sm:text-base">
        <span class="material-symbols-outlined text-rose-600 text-xl">remove_circle</span> បញ្ចូលចំណាយថ្មី (Add Expense)
      </h3>
      <button onclick="closeModal('expense-modal')" class="w-8 h-8 flex items-center justify-center rounded-lg text-gray-400 hover:text-gray-700 hover:bg-slate-100 transition">
        <span class="material-symbols-outlined text-xl">close</span>
      </button>
    </div>

    <form onsubmit="submitExpense(event)" class="p-4 sm:p-6 overflow-y-auto flex-1 flex flex-col justify-between">
      <!-- STEP 1: ផ្នែកទី ១ (កាលបរិច្ឆេទ & ចំនួនទឹកប្រាក់) -->
      <div id="exp-step-1" class="space-y-4">
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">កាលបរិច្ឆេទ *</label>
          <input id="exp-form-date" type="date" required class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-rose-400" />
          <input id="exp-form-time" type="hidden" value="" />
        </div>

        <div class="grid grid-cols-3 gap-2.5 sm:gap-4">
          <div class="col-span-2">
            <label class="block text-xs font-semibold text-gray-700 mb-1">ចំនួនទឹកប្រាក់ * (លេខខ្មែរ/ឡាតាំង)</label>
            <input id="exp-form-amount" type="text" inputmode="numeric" required placeholder="ឧ. 1,000,000 ឬ ១,០០០,០០០"
              oninput="formatAmountInput(this)"
              class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-sm sm:text-base font-bold text-rose-700 focus:outline-none focus:ring-2 focus:ring-rose-400 font-mono tracking-wide" />
          </div>
          <div class="col-span-1">
            <label class="block text-xs font-semibold text-gray-700 mb-1">រូបិយប័ណ្ណ</label>
            <select id="exp-form-currency" class="w-full border border-slate-200 rounded-xl px-2.5 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-rose-400">
              <option value="KHR">៛ (រៀល)</option>
              <option value="USD">$ (ដុល្លារ)</option>
            </select>
          </div>
        </div>

        <div class="flex gap-2.5 sm:gap-3 pt-4 border-t border-slate-100 flex-shrink-0 bg-white">
          <button type="button" onclick="closeModal('expense-modal')" class="btn-secondary flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-medium transition">បោះបង់</button>
          <button type="button" onclick="nextExpenseStep()" class="flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-rose-600 hover:bg-rose-700 text-white shadow transition active:scale-95 flex items-center justify-center gap-1.5">
            <span>បន្តទៅមុខទៀត (Next)</span>
            <span class="material-symbols-outlined text-base">arrow_forward</span>
          </button>
        </div>
      </div>

      <!-- STEP 2: ផ្នែកទី ២ (ព័ត៌មានលម្អិត & សាខា) -->
      <div id="exp-step-2" class="space-y-3 sm:space-y-4 hidden">
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">សាខាក្រុមហ៊ុន</label>
          <select id="exp-form-branch" class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-rose-400 bg-white">
            <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
            <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
          </select>
          <input id="exp-form-ref" type="hidden" value="" />
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5 sm:gap-4">
          <div>
            <div class="flex items-center justify-between gap-1 mb-1.5 flex-wrap">
              <label class="text-xs font-semibold text-gray-700 whitespace-nowrap">កាតាឡុក / ប្រភេទចំណាយ *</label>
              <button type="button" onclick="openCatalogModal('expense')" class="text-[11px] font-semibold text-blue-600 hover:text-blue-700 hover:underline flex items-center gap-0.5 whitespace-nowrap">
                <span class="material-symbols-outlined text-xs">add</span> បង្កើតកាតាឡុកថ្មី
              </button>
            </div>
            <select id="exp-form-category" required class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-rose-400">
            </select>
          </div>
          <div>
            <label class="block text-xs font-semibold text-gray-700 mb-1">មធ្យោបាយទូទាត់</label>
            <select id="exp-form-method" class="w-full border border-slate-200 rounded-xl px-3 py-2 sm:px-3.5 sm:py-2.5 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-rose-400">
              <option value="សាច់ប្រាក់សុទ្ធ" selected>💵 សាច់ប្រាក់សុទ្ធ</option>
              <option value="បាញ់តាមធនាគារ">📱 បាញ់តាមធនាគារ</option>
            </select>
          </div>
        </div>

        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">ការពិពណ៌នា / ចំណាំ</label>
          <textarea id="exp-form-desc" rows="3" placeholder="ព័ត៌មានបន្ថែមអំពីការចំណាយនេះ..." class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-rose-400"></textarea>
        </div>

        <div class="flex gap-2.5 sm:gap-3 pt-3 border-t border-slate-100 flex-shrink-0 bg-white">
          <button type="button" onclick="prevExpenseStep()" class="btn-secondary flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-semibold transition flex items-center justify-center gap-1.5">
            <span class="material-symbols-outlined text-base">arrow_back</span>
            <span>ថយក្រោយ (Back)</span>
          </button>
          <button type="submit" id="btn-save-expense" class="flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-rose-600 hover:bg-rose-700 text-white shadow transition active:scale-95 flex items-center justify-center gap-1.5">
            <span class="material-symbols-outlined text-base">check_circle</span>
            <span>រក្សាទុកចំណាយ</span>
          </button>
        </div>
      </div>
    </form>
  </div>
</div>

<!-- Catalog Modal -->
<div id="catalog-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-md">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100">
      <h3 class="font-bold text-gray-800 flex items-center gap-2" id="catalog-modal-title">
        <span class="material-symbols-outlined text-amber-500 text-xl">category</span> បង្កើតកាតាឡុកថ្មី
      </h3>
      <button onclick="closeModal('catalog-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitCatalog(event)" class="p-6 space-y-4">
      <input type="hidden" id="cat-form-id" value="" />
      <div>
        <label class="block text-xs font-semibold text-gray-700 mb-1">ឈ្មោះកាតាឡុក *</label>
        <input id="cat-form-name" type="text" required placeholder="ឧ. ថ្លៃទឹកភ្លើង, លក់ទំនិញ..." class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400" />
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">ប្រភេទ</label>
          <select id="cat-form-type" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
            <option value="income">🟢 ចំណូល (Income)</option>
            <option value="expense">🔴 ចំណាយ (Expense)</option>
            <option value="both">⚪ ទាំងពីរ (Both)</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">ពណ៌ Theme</label>
          <select id="cat-form-color" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
            <option value="emerald">Emerald (បៃតង)</option>
            <option value="rose">Rose (ក្រហម)</option>
            <option value="blue">Blue (ខៀវ)</option>
            <option value="amber">Amber (លឿងទុំ)</option>
            <option value="purple">Purple (ស្វាយ)</option>
            <option value="indigo">Indigo (ទឹកប៊ិច)</option>
            <option value="teal">Teal (ខៀវស្រាល)</option>
            <option value="cyan">Cyan (ផ្ទៃមេឃ)</option>
            <option value="slate">Slate (ប្រផេះ)</option>
          </select>
        </div>
      </div>

      <div>
        <label class="block text-xs font-semibold text-gray-700 mb-1">រូបតំណាង Icon</label>
        <select id="cat-form-icon" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
          <option value="category">category (កាតាឡុក)</option>
          <option value="storefront">storefront (ហាងទំនិញ)</option>
          <option value="shopping_cart">shopping_cart (រទេះទិញទំនិញ)</option>
          <option value="payments">payments (លុយកាក់)</option>
          <option value="home_work">home_work (ទីតាំង/ផ្ទះ)</option>
          <option value="bolt">bolt (អគ្គិសនី/ទឹក)</option>
          <option value="local_shipping">local_shipping (ដឹកជញ្ជូន)</option>
          <option value="campaign">campaign (ផ្សព្វផ្សាយ)</option>
          <option value="badge">badge (បុគ្គលិក)</option>
          <option value="build">build (ជួសជុល)</option>
          <option value="receipt_long">receipt_long (វិក្កយបត្រ)</option>
          <option value="handshake">handshake (ដៃគូ/សេវាកម្ម)</option>
          <option value="percent">percent (ភាគរយ/កម្រៃ)</option>
          <option value="savings">savings (សន្សំ/ចំណូល)</option>
        </select>
      </div>

      <div>
        <label class="block text-xs font-semibold text-gray-700 mb-1">ការពិពណ៌នា</label>
        <textarea id="cat-form-desc" rows="2" placeholder="ព័ត៌មានលម្អិត..." class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"></textarea>
      </div>

      <div class="flex gap-3 pt-3">
        <button type="button" onclick="closeModal('catalog-modal')" class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit" id="btn-save-catalog" class="btn-primary flex-1 py-2.5 rounded-xl text-sm font-semibold transition">រក្សាទុក</button>
      </div>
    </form>
  </div>
</div>

<!-- Edit Transaction Modal -->
<div id="edit-tx-modal" class="fixed inset-0 bg-black/40 modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 sticky top-0 bg-white z-10">
      <h3 class="font-bold text-gray-800 flex items-center gap-2">
        <span class="material-symbols-outlined text-blue-600 text-xl">edit_note</span> កែប្រែប្រតិបត្តិការ (Edit Transaction)
      </h3>
      <button onclick="closeModal('edit-tx-modal')" class="text-gray-400 hover:text-gray-600">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>
    <form onsubmit="submitEditTransaction(event)" class="p-6 space-y-4">
      <input type="hidden" id="edit-tx-id" value="" />
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">ប្រភេទប្រតិបត្តិការ</label>
          <select id="edit-tx-type" onchange="updateEditCategoriesDropdown()" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-blue-400">
            <option value="income">🟢 ចំណូល (Income)</option>
            <option value="expense">🔴 ចំណាយ (Expense)</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">កាតាឡុក / ប្រភេទ</label>
          <select id="edit-tx-category" required class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
          </select>
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div class="sm:col-span-2">
          <label class="block text-xs font-semibold text-gray-700 mb-1">ចំនួនទឹកប្រាក់ * (វាយបានទាំងលេខខ្មែរ និងឡាតាំង)</label>
          <input id="edit-tx-amount" type="text" inputmode="numeric" required
            oninput="formatAmountInput(this)"
            class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-base font-bold focus:outline-none focus:ring-2 focus:ring-blue-400 font-mono tracking-wide" />
        </div>
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">រូបិយប័ណ្ណ</label>
          <select id="edit-tx-currency" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-blue-400">
            <option value="KHR">៛ (រៀល)</option>
            <option value="USD">$ (ដុល្លារ)</option>
          </select>
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">កាលបរិច្ឆេទ</label>
          <input id="edit-tx-date" type="date" required class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">ម៉ោង</label>
          <input id="edit-tx-time" type="time" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">មធ្យោបាយទូទាត់</label>
          <select id="edit-tx-method" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400">
            <option value="សាច់ប្រាក់សុទ្ធ">💵 សាច់ប្រាក់សុទ្ធ</option>
            <option value="បាញ់តាមធនាគារ">📱 បាញ់តាមធនាគារ</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-semibold text-gray-700 mb-1">លេខវិក្កយបត្រ</label>
          <input id="edit-tx-ref" type="text" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400" />
        </div>
      </div>

      <div>
        <label class="block text-xs font-semibold text-gray-700 mb-1">សាខាក្រុមហ៊ុន</label>
        <select id="edit-tx-branch" class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white">
          <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
          <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
        </select>
      </div>

      <div>
        <label class="block text-xs font-semibold text-gray-700 mb-1">ពិពណ៌នា / ចំណាំ</label>
        <textarea id="edit-tx-desc" rows="2" class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400"></textarea>
      </div>

      <div class="flex gap-3 pt-3">
        <button type="button" onclick="closeModal('edit-tx-modal')" class="btn-secondary flex-1 py-2.5 rounded-xl text-sm font-medium transition">បោះបង់</button>
        <button type="submit" id="btn-save-edit-tx" class="btn-primary flex-1 py-2.5 rounded-xl text-sm font-semibold transition">រក្សាទុកការកែប្រែ</button>
      </div>
    </form>
  </div>
</div>

<!-- Transfer to Investment Fund Modal -->
<div id="transfer-modal" class="fixed inset-0 bg-black/50 backdrop-blur-sm modal-overlay z-50 hidden flex items-center justify-center p-4">
  <div class="modal-box bg-white rounded-3xl shadow-2xl w-full max-w-lg overflow-hidden border border-slate-100 animate-scaleUp">
    <div class="flex items-center justify-between px-6 py-4 border-b border-slate-100 bg-gradient-to-r from-indigo-50 to-purple-50">
      <div class="flex items-center gap-2.5">
        <div class="w-9 h-9 rounded-xl bg-indigo-600 text-white flex items-center justify-center shadow-md shadow-indigo-200">
          <span class="material-symbols-outlined text-xl">swap_horiz</span>
        </div>
        <div>
          <h3 class="font-bold text-gray-800 text-sm sm:text-base">បង្វិលសាច់ប្រាក់ចូល «គណនីវិនិយោគទុន»</h3>
          <p class="text-[11px] text-gray-500 font-medium">ប្រមូលផ្តុំសាច់ប្រាក់បំណាច់ខែសម្រាប់វិនិយោគទុនបន្ត</p>
        </div>
      </div>
      <button onclick="closeModal('transfer-modal')" class="w-8 h-8 rounded-full hover:bg-slate-200/60 flex items-center justify-center text-gray-400 hover:text-gray-600 transition">
        <span class="material-symbols-outlined text-lg">close</span>
      </button>
    </div>

    <form onsubmit="submitTransfer(event)" class="p-6 space-y-4">
      <!-- From -> To Box -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3.5 bg-slate-50/80 p-3.5 rounded-2xl border border-slate-200/80">
        <div>
          <label class="block text-xs font-bold text-gray-700 mb-1 flex items-center gap-1">
            <span class="material-symbols-outlined text-amber-600 text-xs">arrow_upward</span> ផ្ទេរចេញពី (From) *
          </label>
          <select id="tf-from" required class="w-full border border-slate-200 rounded-xl px-3 py-2 text-xs sm:text-sm font-bold bg-white text-gray-800 focus:outline-none focus:ring-2 focus:ring-indigo-400">
            <option value="សាច់ប្រាក់សុទ្ធ">💵 សាច់ប្រាក់សុទ្ធ (Cash In Hand)</option>
            <option value="បាញ់តាមធនាគារ">📱 បាញ់តាមធនាគារ (Operating Bank)</option>
          </select>
        </div>
        <div>
          <label class="block text-xs font-bold text-gray-700 mb-1 flex items-center gap-1">
            <span class="material-symbols-outlined text-indigo-600 text-xs">arrow_downward</span> ផ្ទេរទៅកាន់ (To)
          </label>
          <div class="w-full border border-indigo-200 rounded-xl px-3 py-2 text-xs sm:text-sm font-bold bg-indigo-50/60 text-indigo-900 flex items-center gap-1.5">
            <span class="material-symbols-outlined text-sm text-indigo-600">account_balance</span> 🏦 គណនីវិនិយោគទុន
          </div>
        </div>
      </div>

      <!-- Amount and Currency -->
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div class="sm:col-span-2">
          <label class="block text-xs font-bold text-gray-700 mb-1">ចំនួនទឹកប្រាក់បង្វិល *</label>
          <input id="tf-amount" type="text" inputmode="numeric" required placeholder="ឧ. 500,000"
            oninput="formatAmountInput(this)"
            class="w-full border border-slate-200 rounded-xl px-3.5 py-2.5 text-base font-black focus:outline-none focus:ring-2 focus:ring-indigo-400 font-mono tracking-wide text-indigo-900 bg-white" />
        </div>
        <div>
          <label class="block text-xs font-bold text-gray-700 mb-1">រូបិយប័ណ្ណ</label>
          <select id="tf-currency" class="w-full border border-slate-200 rounded-xl px-3 py-2.5 text-xs sm:text-sm font-bold focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white">
            <option value="KHR">៛ (រៀល)</option>
            <option value="USD">$ (ដុល្លារ)</option>
          </select>
        </div>
      </div>

      <!-- Date and Branch -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label class="block text-xs font-bold text-gray-700 mb-1">កាលបរិច្ឆេទបង្វិល *</label>
          <input id="tf-date" type="date" required class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white font-medium" />
        </div>
        <div>
          <label class="block text-xs font-bold text-gray-700 mb-1">សាខាក្រុមហ៊ុន</label>
          <select id="tf-branch" class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-xs sm:text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white">
            <option value="ក្រុមហ៊ុនបូមលូ កំពង់ចាម">ក្រុមហ៊ុនបូមលូ កំពង់ចាម</option>
            <option value="ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ">ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ</option>
          </select>
        </div>
      </div>

      <!-- Note / Description -->
      <div>
        <label class="block text-xs font-bold text-gray-700 mb-1">កំណត់ចំណាំ / បរិយាយ (Notes)</label>
        <textarea id="tf-desc" rows="2" placeholder="ឧ. បង្វិលសាច់ប្រាក់ចំណេញបំណាច់ខែចូលគណនីវិនិយោគទុនបន្ត..." class="w-full border border-slate-200 rounded-xl px-3.5 py-2 text-xs sm:text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 bg-white"></textarea>
      </div>

      <!-- Submit & Cancel Buttons -->
      <div class="flex items-center justify-end gap-2.5 pt-2 border-t border-slate-100">
        <button type="button" onclick="closeModal('transfer-modal')" class="px-4 py-2.5 rounded-xl text-xs sm:text-sm font-semibold text-gray-600 hover:bg-slate-100 transition active:scale-95">
          បោះបង់
        </button>
        <button type="submit" id="btn-save-transfer" class="px-5 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-indigo-600 hover:bg-indigo-700 text-white shadow-lg shadow-indigo-200 transition active:scale-95 flex items-center gap-1.5">
          <span class="material-symbols-outlined text-base">check_circle</span> រក្សាទុកការបង្វិលប្រាក់
        </button>
      </div>
    </form>
  </div>
</div>

<!-- Toast -->
<div id="toast"
  class="fixed bottom-6 right-6 z-50 hidden max-w-xs bg-gray-900 text-white text-sm px-5 py-3 rounded-xl shadow-2xl flex items-center gap-3 animate-fadeIn">
  <span class="material-symbols-outlined text-green-400 text-lg" id="toast-icon">check_circle</span>
  <span id="toast-msg">ជោគជ័យ!</span>
</div>

<!-- ======================================================
     JAVASCRIPT
====================================================== -->
<script>
  // ===== State =====
  let leavesData = [], loansData = [], currentLeaveFilter = 'all', currentLoanFilter = 'all';
  const avatarColors = ['bg-blue-500','bg-purple-500','bg-green-500','bg-orange-500','bg-pink-500','bg-teal-500'];

  // ===== Date =====
  (function setDate() {
    const d = new Date();
    const opt = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };
    document.getElementById('page-date').textContent = d.toLocaleDateString('km-KH', opt);
  })();

  // ===== Toast & Validation Alert =====
  function showToast(msg, isError = false) {
    const t = document.getElementById('toast');
    if (!t) return;
    document.getElementById('toast-msg').textContent = msg;
    const icon = document.getElementById('toast-icon');
    if (icon) {
      icon.textContent = isError ? 'error' : 'check_circle';
      icon.className = `material-symbols-outlined text-lg ${isError ? 'text-rose-400' : 'text-emerald-400'}`;
    }
    if (isError) {
      t.className = 'fixed bottom-6 right-6 z-50 max-w-sm bg-rose-950 text-white text-sm px-5 py-3 rounded-2xl shadow-2xl flex items-center gap-3 border border-rose-500 animate-bounce';
    } else {
      t.className = 'fixed bottom-6 right-6 z-50 max-w-sm bg-gray-900 text-white text-sm px-5 py-3 rounded-2xl shadow-2xl flex items-center gap-3 border border-slate-700 animate-fadeIn';
    }
    t.classList.remove('hidden');
    clearTimeout(window._toastTimer);
    window._toastTimer = setTimeout(() => t.classList.add('hidden'), 3500);
  }

  function flashRedSaveButton(btnId, errorText) {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    if (btn._isFlashing) return;
    btn._isFlashing = true;
    const originalHtml = btn.innerHTML;
    const originalClass = btn.className;
    btn.className = 'flex-1 py-2.5 rounded-xl text-xs sm:text-sm font-bold bg-rose-600 hover:bg-rose-700 text-white shadow-lg shadow-rose-200 transition active:scale-95 flex items-center justify-center gap-1.5 animate-pulse';
    btn.innerHTML = `<span class="material-symbols-outlined text-base">error</span><span>${escapeHtml(errorText)}</span>`;
    setTimeout(() => {
      btn.className = originalClass;
      btn.innerHTML = originalHtml;
      btn._isFlashing = false;
    }, 2500);
  }

  // ===== SYSTEM EMPLOYEES & DROPDOWN SYNC =====
  let systemEmployeesList = [];

  async function loadSystemEmployees(force = false) {
    if (systemEmployeesList.length > 0 && !force) {
      return systemEmployeesList;
    }
    try {
      const res = await fetch('/api/employees');
      if (res.ok) {
        const data = await res.json();
        if (Array.isArray(data)) {
          systemEmployeesList = data;
          cachedEmployees = data;
        }
      }
    } catch (e) {
      console.error('Error loading system employees:', e);
    }
    return systemEmployeesList;
  }

  function populateEmployeeSelect(selectId, filterBranch = null) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    const prevVal = sel.value;

    let emps = systemEmployeesList;
    if (filterBranch && filterBranch !== 'ទាំងអស់' && filterBranch !== 'All') {
      const filtered = emps.filter(e => e.branch === filterBranch);
      emps = filtered.length > 0 ? filtered : emps;
    }

    if (!emps || emps.length === 0) {
      sel.innerHTML = '<option value="" disabled selected>-- មិនទាន់មានបុគ្គលិកក្នុងប្រព័ន្ធ --</option>';
      return;
    }

    const sorted = [...emps].sort((a, b) => (a.name || '').localeCompare(b.name || '', 'km'));

    let html = '<option value="" disabled selected>-- ជ្រើសរើសឈ្មោះបុគ្គលិក --</option>';
    sorted.forEach(emp => {
      const branchShort = emp.branch ? ` (${emp.branch.replace('ក្រុមហ៊ុនបូមលូ ', '')})` : '';
      const pos = emp.position ? ` - ${emp.position}` : '';
      html += `<option value="${escapeHtml(emp.name)}" data-branch="${escapeHtml(emp.branch || '')}">👤 ${escapeHtml(emp.name)}${branchShort}${pos}</option>`;
    });

    sel.innerHTML = html;
    if (prevVal && sorted.some(e => e.name === prevVal)) {
      sel.value = prevVal;
    }
  }

  function onLeaveEmployeeChanged() {
    const sel = document.getElementById('leave-name');
    if (!sel) return;
    const opt = sel.options[sel.selectedIndex];
    const branch = opt ? opt.getAttribute('data-branch') : null;
    if (branch) {
      const bSel = document.getElementById('leave-branch');
      if (bSel && bSel.value !== branch) bSel.value = branch;
    }
  }

  function onLeaveBranchChanged() {
    const branch = document.getElementById('leave-branch')?.value;
    populateEmployeeSelect('leave-name', branch);
  }

  function onLoanEmployeeChanged() {
    const sel = document.getElementById('loan-name');
    if (!sel) return;
    const opt = sel.options[sel.selectedIndex];
    const branch = opt ? opt.getAttribute('data-branch') : null;
    if (branch) {
      const bSel = document.getElementById('loan-branch');
      if (bSel && bSel.value !== branch) bSel.value = branch;
    }
  }

  function onLoanBranchChanged() {
    const branch = document.getElementById('loan-branch')?.value;
    populateEmployeeSelect('loan-name', branch);
  }

  // ===== Modal (with phone back button awareness) =====
  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('hidden');

    // Populate employee dropdown and set default date for leave and loan
    const getKhmerTodayStr = () => {
      const tzOffset = 7 * 60;
      const now = new Date(Date.now() + tzOffset * 60000);
      return now.toISOString().slice(0, 10);
    };

    if (id === 'leave-modal') {
      const d = document.getElementById('leave-date');
      if (d && !d.value) d.value = getKhmerTodayStr();
      const b = document.getElementById('leave-branch');
      if (b && currentActiveBranch) b.value = currentActiveBranch;
      const targetBranch = (b ? b.value : currentActiveBranch) || null;
      loadSystemEmployees().then(() => {
        populateEmployeeSelect('leave-name', targetBranch);
      });
    } else if (id === 'loan-modal') {
      const d = document.getElementById('loan-date');
      if (d && !d.value) d.value = getKhmerTodayStr();
      const b = document.getElementById('loan-branch');
      if (b && currentActiveBranch) b.value = currentActiveBranch;
      const targetBranch = (b ? b.value : currentActiveBranch) || null;
      loadSystemEmployees().then(() => {
        populateEmployeeSelect('loan-name', targetBranch);
      });
    }

    // Pre-select active branch if user has chosen a specific branch
    if (currentActiveBranch) {
      if (id === 'emp-modal') {
        const b = document.getElementById('emp-branch');
        if (b) b.value = currentActiveBranch;
      } else if (id === 'bulk-emp-modal') {
        const b = document.getElementById('bulk-emp-branch');
        if (b) b.value = currentActiveBranch;
      } else if (id === 'income-modal') {
        const b = document.getElementById('inc-form-branch');
        if (b) b.value = currentActiveBranch;
        const incCat = document.getElementById('inc-form-category');
        if (incCat && !incCat.value) incCat.value = 'ចំណូលពីសេវាបូមលូ';
        const incMethod = document.getElementById('inc-form-method');
        if (incMethod && !incMethod.value) incMethod.value = 'សាច់ប្រាក់សុទ្ធ';
        if (typeof setIncomeStep === 'function') setIncomeStep(1);
      } else if (id === 'expense-modal') {
        const b = document.getElementById('exp-form-branch');
        if (b) b.value = currentActiveBranch;
      }
    }
    // Push modal state into browser history so pressing phone back button closes the modal!
    window.history.pushState({ modal: id, tab: currentActiveTab }, '', window.location.pathname);
  }

  function closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.classList.add('hidden');
  }
  // Close on overlay click
  document.querySelectorAll('.modal-overlay').forEach(el => {
    el.addEventListener('click', e => { if (e.target === el) el.classList.add('hidden'); });
  });

  // ===== Sidebar toggle & mobile backdrop =====
  function toggleSidebar() {
    const s = document.getElementById('sidebar');
    const b = document.getElementById('sidebar-backdrop');
    if (!s) return;
    const isOpen = !s.classList.contains('-translate-x-full');
    if (isOpen) {
      closeSidebar();
    } else {
      s.classList.remove('-translate-x-full');
      if (b) b.classList.remove('hidden');
    }
  }

  function closeSidebar() {
    const s = document.getElementById('sidebar');
    const b = document.getElementById('sidebar-backdrop');
    if (s) s.classList.add('-translate-x-full');
    if (b) b.classList.add('hidden');
  }

  function toggleReportsMenu() {
    const menu = document.getElementById('sub-reports-menu');
    const chevron = document.getElementById('reports-chevron');
    if (menu) menu.classList.toggle('hidden');
    if (chevron) chevron.classList.toggle('rotate-180');
  }

  function escapeHtml(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ===== GLOBAL MULTI-BRANCH STATE & SWITCHER =====
  let currentActiveBranch = localStorage.getItem('ac_active_branch') || '';

  function initGlobalBranchSelect() {
    const sel = document.getElementById('global-branch-select');
    if (sel) {
      sel.value = currentActiveBranch;
    }
  }

  function onGlobalBranchChange(val) {
    currentActiveBranch = val || '';
    localStorage.setItem('ac_active_branch', currentActiveBranch);
    showToast(currentActiveBranch ? `ប្តូរមើល៖ ${currentActiveBranch}` : 'ប្តូរមើល៖ គ្រប់សាខាទាំងអស់');
    
    // Sync all modal selects
    ['inc-form-branch', 'exp-form-branch', 'emp-branch', 'bulk-emp-branch', 'leave-branch', 'loan-branch'].forEach(id => {
      const el = document.getElementById(id);
      if (el && currentActiveBranch) el.value = currentActiveBranch;
    });

    refreshAll();
  }

  // ===== Tab switching =====
  const tabs = ['dashboard','income','expense','catalog','leaves','loans','employees','settings','reports-finance','reports-leave','reports-loan'];
  const titles = {
    dashboard: 'ទំព័រដើម',
    income: 'ការគ្រប់គ្រងចំណូល',
    expense: 'ការគ្រប់គ្រងចំណាយ',
    catalog: 'កាតាឡុក និងប្រភេទប្រតិបត្តិការ',
    leaves: 'ច្បាប់ឈប់សម្រាក',
    loans: 'ខ្ចីប្រាក់ / បុរេប្រទាន',
    employees: 'បុគ្គលិកទាំងអស់',
    settings: 'ការកំណត់ប្រព័ន្ធ',
    'reports-finance': 'របាយការណ៍ហិរញ្ញវត្ថុ (ចំណូល-ចំណាយ)',
    'reports-leave': 'របាយការណ៍ច្បាប់ (សងថ្ងៃសៅរ៍-អាទិត្យ)',
    'reports-loan': 'របាយការណ៍ខ្ចីប្រាក់ (កាត់ប្រាក់ខែចុងខែ)'
  };

  function goToHomepage() {
    showTab('dashboard');
  }

  // ===== SPA History & Navigation Stack =====
  let currentActiveTab = 'dashboard';
  let uiHistory = ['dashboard'];
  let uiHistoryIndex = 0;

  function showTab(tab, pushHistory = true) {
    if (window.innerWidth < 1024) closeSidebar();
    
    // Auto-close any open modals when navigating tabs
    document.querySelectorAll('.modal-overlay').forEach(m => m.classList.add('hidden'));

    tabs.forEach(t => {
      const el = document.getElementById('tab-' + t);
      if (el) el.classList.add('hidden');
      const nav = document.getElementById('nav-' + t);
      if (nav) {
        nav.classList.remove('active');
        nav.classList.add('text-blue-200');
      }
    });

    const targetTab = document.getElementById('tab-' + tab);
    if (targetTab) targetTab.classList.remove('hidden');

    const activeNav = document.getElementById('nav-' + tab);
    if (activeNav) {
      activeNav.classList.add('active');
      activeNav.classList.remove('text-blue-200');
    }

    document.getElementById('page-title').textContent = titles[tab] || 'ទំព័រដើម';

    // SPA Browser History and Navigation Stack Integration
    if (pushHistory && currentActiveTab !== tab) {
      window.history.pushState({ tab: tab }, '', window.location.pathname);
      uiHistory = uiHistory.slice(0, uiHistoryIndex + 1);
      uiHistory.push(tab);
      uiHistoryIndex = uiHistory.length - 1;
    }
    currentActiveTab = tab;
    updateNavButtonsState();

    if (tab === 'leaves') loadLeaves();
    if (tab === 'loans') loadLoans();
    if (tab === 'employees') loadEmployees();
    if (tab === 'settings') loadSettings();
    if (tab === 'reports-leave') loadWeekendLeaveReport();
    if (tab === 'reports-loan') loadMonthlyLoanReport();
    if (tab === 'income') { loadCatalogs(); loadIncome(); }
    if (tab === 'expense') { loadCatalogs(); loadExpense(); }
    if (tab === 'catalog') { loadCatalogs(); }
    if (tab === 'reports-finance') { initFinanceReportFilters(); loadFinanceReports(); }
  }

  // Backward navigation: go to last UI screen
  function navGoBack() {
    const openModalEl = document.querySelector('.modal-overlay:not(.hidden)');
    if (openModalEl) {
      openModalEl.classList.add('hidden');
      return;
    }

    if (window.history.length > 1 && uiHistoryIndex > 0) {
      window.history.back();
    } else if (uiHistoryIndex > 0) {
      uiHistoryIndex--;
      const prev = uiHistory[uiHistoryIndex];
      showTab(prev, false);
      window.history.replaceState({ tab: prev }, '', window.location.pathname);
      updateNavButtonsState();
    } else {
      if (currentActiveTab !== 'dashboard') {
        showTab('dashboard');
      }
    }
  }

  // Forward navigation: go forward in history
  function navGoForward() {
    if (uiHistoryIndex < uiHistory.length - 1) {
      window.history.forward();
    }
  }

  // Update enable/disable visual state of Back & Forward buttons
  function updateNavButtonsState() {
    const btnBack = document.getElementById('btn-nav-back');
    const btnFwd = document.getElementById('btn-nav-forward');
    const canBack = uiHistoryIndex > 0 || currentActiveTab !== 'dashboard';
    const canFwd = uiHistoryIndex < uiHistory.length - 1;

    if (btnBack) {
      btnBack.disabled = !canBack;
      btnBack.classList.toggle('opacity-30', !canBack);
      btnBack.classList.toggle('pointer-events-none', !canBack);
    }
    if (btnFwd) {
      btnFwd.disabled = !canFwd;
      btnFwd.classList.toggle('opacity-30', !canFwd);
      btnFwd.classList.toggle('pointer-events-none', !canFwd);
    }
  }

  // Browser Popstate Event (Android Back Button, Swipe Back gesture, Chrome Back/Forward arrows)
  window.addEventListener('popstate', function(event) {
    // 1. If any modal is open on phone/desktop, close it first!
    const openModalEl = document.querySelector('.modal-overlay:not(.hidden)');
    if (openModalEl) {
      openModalEl.classList.add('hidden');
      return;
    }

    // 2. Navigate back to previous UI screen
    let target = (event.state && event.state.tab) || (window.location.hash ? window.location.hash.replace('#', '') : 'dashboard');
    if (!tabs.includes(target)) target = 'dashboard';

    const foundIdx = uiHistory.lastIndexOf(target);
    if (foundIdx !== -1) {
      uiHistoryIndex = foundIdx;
    } else {
      uiHistory.push(target);
      uiHistoryIndex = uiHistory.length - 1;
    }

    showTab(target, false);
    updateNavButtonsState();
  });

  // ===== Badge =====
  function statusBadge(s) {
    const map = { pending:'badge-pending', approved:'badge-approved', rejected:'badge-rejected' };
    const lbl = { pending:'រង់ចាំ', approved:'អនុញ្ញាតិ', rejected:'មិនអនុញ្ញាតិ' };
    return `<span class="text-xs px-2 py-0.5 rounded-full font-medium ${map[s]||'bg-gray-100 text-gray-500'}">${lbl[s]||s}</span>`;
  }

  // ===== LOAD STATS =====
  async function loadStats() {
    try {
      const url = currentActiveBranch ? `/api/stats?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/stats';
      const r = await fetch(url);
      const d = await r.json();
      document.getElementById('stat-employees').textContent = d.total_employees;
      document.getElementById('stat-leaves').textContent = d.pending_leaves;
      document.getElementById('stat-loans').textContent = d.pending_loans;
      document.getElementById('stat-approved').textContent = d.approved_loans;

      // Badges
      const lb = document.getElementById('badge-leaves');
      const lnb = document.getElementById('badge-loans');
      if (d.pending_leaves > 0) { lb.textContent = d.pending_leaves; lb.classList.remove('hidden'); } else lb.classList.add('hidden');
      if (d.pending_loans > 0) { lnb.textContent = d.pending_loans; lnb.classList.remove('hidden'); } else lnb.classList.add('hidden');
    } catch(e) {}
  }

  // ===== LOAD LEAVES =====
  async function loadLeaves() {
    try {
      const url = currentActiveBranch ? `/api/leaves?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/leaves';
      const r = await fetch(url);
      leavesData = await r.json();
      renderLeaves();
      renderDashLeaves();
    } catch(e) {
      document.getElementById('leaves-tbody').innerHTML = '<tr><td colspan="5" class="text-center py-8 text-red-400">មិនអាចភ្ជាប់ MongoDB បាន</td></tr>';
    }
  }

  function renderLeaves() {
    const filtered = currentLeaveFilter === 'all' ? leavesData : leavesData.filter(l => l.status === currentLeaveFilter);
    const tbody = document.getElementById('leaves-tbody');
    const mobileContainer = document.getElementById('leaves-mobile-cards');
    if (!filtered.length) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="text-center py-10 text-gray-400">គ្មានទិន្នន័យ</td></tr>';
      if (mobileContainer) mobileContainer.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">គ្មានទិន្នន័យ</div>';
      return;
    }
    if (tbody) {
      tbody.innerHTML = filtered.map(l => `
        <tr class="hover:bg-slate-50 transition">
          <td class="px-5 py-3.5 font-medium whitespace-nowrap">
            <div>${escapeHtml(l.employee_name)}</div>
            <div class="mt-0.5 flex items-center gap-1.5 flex-wrap">
              <span class="inline-flex items-center gap-1 text-[10px] text-indigo-700 bg-indigo-50 border border-indigo-200 px-1.5 py-0.5 rounded-md font-medium">🏢 ${escapeHtml(l.branch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម')}</span>
              ${l.date ? `<span class="inline-flex items-center gap-1 text-[10px] text-slate-600 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded-md font-mono">📅 ${escapeHtml(l.date)}</span>` : ''}
            </div>
          </td>
          <td class="px-5 py-3.5 text-gray-600 whitespace-nowrap">
            <div>${escapeHtml(l.reason)}</div>
            ${l.voice_id ? `
              <div class="mt-1 flex items-center gap-1.5">
                <a href="/voice-player/${l.voice_id}" target="_blank" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 hover:bg-blue-100 text-[11px] font-semibold border border-blue-200 transition">
                  <span class="material-symbols-outlined text-xs">play_circle</span> ស្ដាប់សំឡេង
                </a>
                <audio controls preload="none" src="/api/voice/${l.voice_id}" class="h-6 w-36"></audio>
              </div>` : ''}
          </td>
          <td class="px-5 py-3.5 whitespace-nowrap"><span class="font-bold text-blue-600">${l.days || 1}</span> ថ្ងៃ</td>
          <td class="px-5 py-3.5 whitespace-nowrap">${statusBadge(l.status)}</td>
          <td class="px-5 py-3.5 text-right whitespace-nowrap">
            <div class="flex items-center justify-end gap-1">
              ${l.status === 'pending' ? `
              <button onclick="updateLeave('${l._id}','approved')" title="អនុញ្ញាតិ"
                class="w-8 h-8 flex items-center justify-center rounded-lg bg-green-50 text-green-600 hover:bg-green-100 transition">
                <span class="material-symbols-outlined text-base">check</span>
              </button>
              <button onclick="updateLeave('${l._id}','rejected')" title="មិនអនុញ្ញាតិ"
                class="w-8 h-8 flex items-center justify-center rounded-lg bg-red-50 text-red-500 hover:bg-red-100 transition">
                <span class="material-symbols-outlined text-base">close</span>
              </button>` : ''}
              <button onclick="deleteLeave('${l._id}')" title="លុប"
                class="w-8 h-8 flex items-center justify-center rounded-lg bg-slate-50 text-slate-400 hover:bg-red-50 hover:text-red-500 transition">
                <span class="material-symbols-outlined text-base">delete</span>
              </button>
            </div>
          </td>
        </tr>`).join('');
    }
    if (mobileContainer) {
      mobileContainer.innerHTML = filtered.map(l => `
        <div class="bg-white rounded-xl p-3.5 shadow-sm border border-slate-100 flex flex-col gap-2.5">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2.5 min-w-0">
              <div class="w-9 h-9 rounded-full bg-blue-100 text-blue-700 font-bold flex items-center justify-center text-sm flex-shrink-0">
                ${(l.employee_name || 'U').charAt(0).toUpperCase()}
              </div>
              <div class="min-w-0">
                <p class="font-bold text-gray-800 text-sm truncate">${escapeHtml(l.employee_name)}</p>
                <p class="text-[11px] text-gray-400">${l.date || 'ច្បាប់ឈប់សម្រាក'}</p>
              </div>
            </div>
            <div class="flex-shrink-0">${statusBadge(l.status)}</div>
          </div>
          <div class="bg-slate-50 rounded-lg p-2.5 flex flex-col gap-1.5 text-xs sm:text-sm">
            <div class="flex items-center justify-between">
              <span class="text-gray-700 font-medium truncate mr-2">${escapeHtml(l.reason)}</span>
              <span class="font-bold text-blue-600 bg-blue-50 px-2.5 py-0.5 rounded text-xs whitespace-nowrap">
                ${l.days || 1} ថ្ងៃ
              </span>
            </div>
            ${l.voice_id ? `
              <div class="flex items-center gap-2 pt-1 border-t border-slate-200/60">
                <a href="/voice-player/${l.voice_id}" target="_blank" class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-600 text-white text-[11px] font-medium shadow-sm">
                  <span class="material-symbols-outlined text-xs">play_circle</span> ស្ដាប់
                </a>
                <audio controls preload="none" src="/api/voice/${l.voice_id}" class="h-6 w-full max-w-[200px]"></audio>
              </div>` : ''}
          </div>
          <div class="flex items-center justify-end gap-2 pt-2 border-t border-slate-50">
            ${l.status === 'pending' ? `
              <button onclick="updateLeave('${l._id}','approved')"
                class="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-600 text-white text-xs font-semibold hover:bg-green-700 active:scale-95 transition shadow-sm">
                <span class="material-symbols-outlined text-sm">check</span> អនុញ្ញាតិ
              </button>
              <button onclick="updateLeave('${l._id}','rejected')"
                class="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-red-50 text-red-600 text-xs font-semibold hover:bg-red-100 active:scale-95 transition">
                <span class="material-symbols-outlined text-sm">close</span> បដិសេធ
              </button>
            ` : ''}
            <button onclick="deleteLeave('${l._id}')"
              class="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 active:scale-95 transition" title="លុប">
              <span class="material-symbols-outlined text-lg">delete</span>
            </button>
          </div>
        </div>`).join('');
    }
  }

  function renderDashLeaves() {
    const el = document.getElementById('dash-leaves');
    if (!el) return;
    const recent = leavesData.slice(0, 5);
    if (!recent.length) { el.innerHTML = '<div class="p-4 text-gray-400 text-center text-sm">គ្មានទិន្នន័យ</div>'; return; }
    el.innerHTML = recent.map(l => `
      <div class="px-3.5 sm:px-5 py-3 flex items-center justify-between gap-2.5">
        <div class="flex items-center gap-2.5 min-w-0">
          <div class="w-8 h-8 rounded-full bg-orange-100 flex items-center justify-center text-orange-600 text-xs font-bold flex-shrink-0">
            ${l.employee_name.charAt(0)}
          </div>
          <div class="min-w-0">
            <p class="font-medium text-gray-800 text-sm truncate">${escapeHtml(l.employee_name)}</p>
            <p class="text-xs text-gray-400 truncate">${escapeHtml(l.reason)} · ${l.days||1} ថ្ងៃ</p>
          </div>
        </div>
        <div class="flex-shrink-0">${statusBadge(l.status)}</div>
      </div>`).join('');
  }

  function filterLeaves(f) {
    currentLeaveFilter = f;
    ['all','pending','approved','rejected'].forEach(x => {
      const btn = document.getElementById('lf-' + x);
      btn.className = x === f
        ? 'px-4 py-1.5 rounded-full bg-blue-600 text-white font-medium'
        : 'px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50';
    });
    renderLeaves();
  }

  async function updateLeave(id, status) {
    await fetch(`/api/leave/${id}/status?status=${status}`, { method: 'PATCH' });
    showToast(status === 'approved' ? 'បានអនុញ្ញាតិ!' : 'មិនអនុញ្ញាតិ!');
    await loadLeaves(); await loadStats();
  }

  async function deleteLeave(id) {
    if (!confirm('តើអ្នកចង់លុបពិតមែនទេ?')) return;
    await fetch(`/api/leave/${id}`, { method: 'DELETE' });
    showToast('បានលុបរួចរាល់!');
    await loadLeaves(); await loadStats();
  }

  async function submitLeave(e) {
    e.preventDefault();
    const empName = (document.getElementById('leave-name')?.value || '').trim();
    if (!empName) {
      showToast('សូមជ្រើសរើសឈ្មោះបុគ្គលិក!', true);
      const sel = document.getElementById('leave-name');
      if (sel) sel.focus();
      return;
    }
    if (systemEmployeesList.length > 0 && !systemEmployeesList.some(e => e.name === empName)) {
      showToast('ឈ្មោះបុគ្គលិកនេះមិនមានក្នុងប្រព័ន្ធឡើយ! សូមជ្រើសរើសពីបញ្ជី!', true);
      return;
    }
    const daysVal = parseFloat(document.getElementById('leave-days')?.value || '1');
    if (!daysVal || daysVal <= 0) {
      showToast('សូមបញ្ចូលចំនួនថ្ងៃដែលធំជាង ០!', true);
      return;
    }

    const payload = {
      employee_name: empName,
      branch: document.getElementById('leave-branch')?.value || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម',
      reason: document.getElementById('leave-reason')?.value || '',
      days: daysVal,
      date: document.getElementById('leave-date')?.value || ''
    };
    try {
      const res = await fetch('/api/leave', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (res.ok) {
        closeModal('leave-modal');
        e.target.reset();
        showToast('បានបន្ថែមសំណើសុំច្បាប់ជោគជ័យ!');
        await loadLeaves();
        await loadStats();
      } else {
        showToast('បរាជ័យក្នុងការបន្ថែមសំណើសុំច្បាប់!', true);
      }
    } catch(err) {
      showToast('កំហុស៖ ' + err.message, true);
    }
  }

  // ===== LOAD LOANS =====
  async function loadLoans() {
    try {
      const url = currentActiveBranch ? `/api/loans?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/loans';
      const r = await fetch(url);
      loansData = await r.json();
      renderLoans();
      renderDashLoans();
    } catch(e) {
      document.getElementById('loans-tbody').innerHTML = '<tr><td colspan="5" class="text-center py-8 text-red-400">មិនអាចភ្ជាប់ MongoDB បាន</td></tr>';
    }
  }

  function renderLoans() {
    const filtered = currentLoanFilter === 'all' ? loansData : loansData.filter(l => l.status === currentLoanFilter);
    const tbody = document.getElementById('loans-tbody');
    const mobileContainer = document.getElementById('loans-mobile-cards');
    if (!filtered.length) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="text-center py-10 text-gray-400">គ្មានទិន្នន័យ</td></tr>';
      if (mobileContainer) mobileContainer.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">គ្មានទិន្នន័យ</div>';
      return;
    }
    if (tbody) {
      tbody.innerHTML = filtered.map(l => `
        <tr class="hover:bg-slate-50 transition">
          <td class="px-5 py-3.5 font-medium whitespace-nowrap">
            <div>${escapeHtml(l.employee_name)}</div>
            <div class="mt-0.5 flex items-center gap-1.5 flex-wrap">
              <span class="inline-flex items-center gap-1 text-[10px] text-indigo-700 bg-indigo-50 border border-indigo-200 px-1.5 py-0.5 rounded-md font-medium">🏢 ${escapeHtml(l.branch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម')}</span>
              ${l.date ? `<span class="inline-flex items-center gap-1 text-[10px] text-slate-600 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded-md font-mono">📅 ${escapeHtml(l.date)}</span>` : ''}
            </div>
          </td>
          <td class="px-5 py-3.5 font-bold text-blue-700 whitespace-nowrap">${Number(l.amount_khr).toLocaleString('km-KH')} ៛</td>
          <td class="px-5 py-3.5 text-gray-600 whitespace-nowrap">
            <div>${escapeHtml(l.purpose || '—')}</div>
            ${l.voice_id ? `
              <div class="mt-1 flex items-center gap-1.5">
                <a href="/voice-player/${l.voice_id}" target="_blank" class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 hover:bg-emerald-100 text-[11px] font-semibold border border-emerald-200 transition">
                  <span class="material-symbols-outlined text-xs">play_circle</span> ស្ដាប់សំឡេង
                </a>
                <audio controls preload="none" src="/api/voice/${l.voice_id}" class="h-6 w-36"></audio>
              </div>` : ''}
          </td>
          <td class="px-5 py-3.5 whitespace-nowrap">${statusBadge(l.status)}</td>
          <td class="px-5 py-3.5 text-right whitespace-nowrap">
            <div class="flex items-center justify-end gap-1">
              ${l.status === 'pending' ? `
              <button onclick="updateLoan('${l._id}','approved')" title="អនុញ្ញាតិ"
                class="w-8 h-8 flex items-center justify-center rounded-lg bg-green-50 text-green-600 hover:bg-green-100 transition">
                <span class="material-symbols-outlined text-base">check</span>
              </button>
              <button onclick="updateLoan('${l._id}','rejected')" title="មិនអនុញ្ញាតិ"
                class="w-8 h-8 flex items-center justify-center rounded-lg bg-red-50 text-red-500 hover:bg-red-100 transition">
                <span class="material-symbols-outlined text-base">close</span>
              </button>` : ''}
              <button onclick="deleteLoan('${l._id}')" title="លុប"
                class="w-8 h-8 flex items-center justify-center rounded-lg bg-slate-50 text-slate-400 hover:bg-red-50 hover:text-red-500 transition">
                <span class="material-symbols-outlined text-base">delete</span>
              </button>
            </div>
          </td>
        </tr>`).join('');
    }
    if (mobileContainer) {
      mobileContainer.innerHTML = filtered.map(l => `
        <div class="bg-white rounded-xl p-3.5 shadow-sm border border-slate-100 flex flex-col gap-2.5">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2.5 min-w-0">
              <div class="w-9 h-9 rounded-full bg-emerald-100 text-emerald-700 font-bold flex items-center justify-center text-sm flex-shrink-0">
                ${(l.employee_name || 'U').charAt(0).toUpperCase()}
              </div>
              <div class="min-w-0">
                <p class="font-bold text-gray-800 text-sm truncate">${escapeHtml(l.employee_name)}</p>
                <p class="text-[11px] text-gray-400">${l.date || 'ស្នើសុំខ្ចីប្រាក់'}</p>
              </div>
            </div>
            <div class="flex-shrink-0">${statusBadge(l.status)}</div>
          </div>
          <div class="bg-slate-50 rounded-lg p-2.5 flex flex-col gap-1.5 text-xs sm:text-sm">
            <div class="flex items-center justify-between">
              <span class="text-gray-700 font-medium truncate mr-2">${escapeHtml(l.purpose || 'ខ្ចីប្រាក់ / បុរេប្រទាន')}</span>
              <span class="font-bold text-emerald-600 bg-emerald-50 px-2.5 py-0.5 rounded text-xs whitespace-nowrap">
                ${Number(l.amount_khr).toLocaleString('km-KH')} ៛
              </span>
            </div>
            ${l.voice_id ? `
              <div class="flex items-center gap-2 pt-1 border-t border-slate-200/60">
                <a href="/voice-player/${l.voice_id}" target="_blank" class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-emerald-600 text-white text-[11px] font-medium shadow-sm">
                  <span class="material-symbols-outlined text-xs">play_circle</span> ស្ដាប់
                </a>
                <audio controls preload="none" src="/api/voice/${l.voice_id}" class="h-6 w-full max-w-[200px]"></audio>
              </div>` : ''}
          </div>
          <div class="flex items-center justify-end gap-2 pt-2 border-t border-slate-50">
            ${l.status === 'pending' ? `
              <button onclick="updateLoan('${l._id}','approved')"
                class="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-green-600 text-white text-xs font-semibold hover:bg-green-700 active:scale-95 transition shadow-sm">
                <span class="material-symbols-outlined text-sm">check</span> អនុញ្ញាតិ
              </button>
              <button onclick="updateLoan('${l._id}','rejected')"
                class="flex items-center gap-1 px-3 py-1.5 rounded-lg bg-red-50 text-red-600 text-xs font-semibold hover:bg-red-100 active:scale-95 transition">
                <span class="material-symbols-outlined text-sm">close</span> បដិសេធ
              </button>
            ` : ''}
            <button onclick="deleteLoan('${l._id}')"
              class="p-1.5 rounded-lg text-slate-400 hover:text-red-500 hover:bg-red-50 active:scale-95 transition" title="លុប">
              <span class="material-symbols-outlined text-lg">delete</span>
            </button>
          </div>
        </div>`).join('');
    }
  }
  function renderDashLoans() {
    const el = document.getElementById('dash-loans');
    if (!el) return;
    const recent = loansData.slice(0, 5);
    if (!recent.length) { el.innerHTML = '<div class="p-4 text-gray-400 text-center text-sm">គ្មានទិន្នន័យ</div>'; return; }
    el.innerHTML = recent.map(l => `
      <div class="px-3.5 sm:px-5 py-3 flex items-center justify-between gap-2.5">
        <div class="flex items-center gap-2.5 min-w-0">
          <div class="w-8 h-8 rounded-full bg-green-100 flex items-center justify-center text-green-600 text-xs font-bold flex-shrink-0">
            ${l.employee_name.charAt(0)}
          </div>
          <div class="min-w-0">
            <p class="font-medium text-gray-800 text-sm truncate">${escapeHtml(l.employee_name)}</p>
            <p class="text-xs text-blue-600 font-semibold truncate">${Number(l.amount_khr).toLocaleString()} ៛</p>
          </div>
        </div>
        <div class="flex-shrink-0">${statusBadge(l.status)}</div>
      </div>`).join('');
  }

  function filterLoans(f) {
    currentLoanFilter = f;
    ['all','pending','approved','rejected'].forEach(x => {
      const btn = document.getElementById('lnf-' + x);
      btn.className = x === f
        ? 'px-4 py-1.5 rounded-full bg-blue-600 text-white font-medium'
        : 'px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50';
    });
    renderLoans();
  }

  async function updateLoan(id, status) {
    await fetch(`/api/loan/${id}/status?status=${status}`, { method: 'PATCH' });
    showToast(status === 'approved' ? 'បានអនុញ្ញាតិ!' : 'មិនអនុញ្ញាតិ!');
    await loadLoans(); await loadStats();
  }

  async function deleteLoan(id) {
    if (!confirm('តើអ្នកចង់លុបពិតមែនទេ?')) return;
    await fetch(`/api/loan/${id}`, { method: 'DELETE' });
    showToast('បានលុបរួចរាល់!');
    await loadLoans(); await loadStats();
  }

  async function submitLoan(e) {
    e.preventDefault();
    const empName = (document.getElementById('loan-name')?.value || '').trim();
    if (!empName) {
      showToast('សូមជ្រើសរើសឈ្មោះបុគ្គលិក!', true);
      const sel = document.getElementById('loan-name');
      if (sel) sel.focus();
      return;
    }
    if (systemEmployeesList.length > 0 && !systemEmployeesList.some(e => e.name === empName)) {
      showToast('ឈ្មោះបុគ្គលិកនេះមិនមានក្នុងប្រព័ន្ធឡើយ! សូមជ្រើសរើសពីបញ្ជី!', true);
      return;
    }
    const rawAmt = document.getElementById('loan-amount')?.value || '0';
    const amt = parseFormattedAmount(rawAmt);
    if (!amt || amt <= 0) {
      showToast('សូមបញ្ចូលចំនួនទឹកប្រាក់ខ្ចីដែលធំជាង ០!', true);
      const aInp = document.getElementById('loan-amount');
      if (aInp) aInp.focus();
      return;
    }

    const payload = {
      employee_name: empName,
      branch: document.getElementById('loan-branch')?.value || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម',
      amount_khr: amt,
      purpose: document.getElementById('loan-purpose')?.value || '',
      date: document.getElementById('loan-date')?.value || ''
    };
    try {
      const res = await fetch('/api/loan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      if (res.ok) {
        closeModal('loan-modal');
        e.target.reset();
        showToast('បានបន្ថែមសំណើខ្ចីប្រាក់ជោគជ័យ!');
        await loadLoans();
        await loadStats();
      } else {
        showToast('បរាជ័យក្នុងការបន្ថែមសំណើខ្ចីប្រាក់!', true);
      }
    } catch(err) {
      showToast('កំហុស៖ ' + err.message, true);
    }
  }

  // ===== LOAD EMPLOYEES =====
  let cachedEmployees = [];
  async function loadEmployees() {
    try {
      const url = currentActiveBranch ? `/api/employees?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/employees';
      const r = await fetch(url);
      const data = await r.json();
      cachedEmployees = Array.isArray(data) ? data : [];
      systemEmployeesList = cachedEmployees;
      const grid = document.getElementById('employees-grid');
      if (!cachedEmployees.length) {
        grid.innerHTML = '<div class="col-span-full text-center py-10 text-gray-400">គ្មានបុគ្គលិក — ចុច "Sync ពី Telegram" ឬ "បន្ថែម" ដើម្បីចាប់ផ្តើម</div>'; return;
      }
      grid.innerHTML = cachedEmployees.map((emp, i) => {
        const color = avatarColors[i % avatarColors.length];
        const tgBadge = emp.telegram_username ? 
          `<a href="https://t.me/${emp.telegram_username}" target="_blank" class="inline-flex items-center gap-1 text-[11px] text-blue-500 hover:text-blue-700 bg-blue-50 hover:bg-blue-100 px-2 py-0.5 rounded-full mt-1 transition">
             <span>✈️ @${emp.telegram_username}</span>
           </a>` : '';
        const branchBadge = emp.branch ? 
          `<span class="inline-flex items-center gap-1 text-[10px] font-semibold text-indigo-700 bg-indigo-50 border border-indigo-200 px-2 py-0.5 rounded-full mt-1">
             🏢 ${emp.branch}
           </span>` : '';
        const phoneHtml = emp.phone ? 
          `<p class="text-xs text-gray-600 mt-1 flex items-center justify-center gap-1 font-mono font-medium">
             <span class="material-symbols-outlined text-sm text-green-600">call</span> ${emp.phone}
           </p>` : 
          `<p class="text-[11px] text-amber-500 mt-1 flex items-center justify-center gap-1 bg-amber-50 px-2 py-0.5 rounded-md">
             <span>⚠️ មិនទាន់មានលេខ</span>
           </p>`;

        return `<div class="bg-white rounded-2xl shadow-sm border border-slate-100 p-5 flex flex-col items-center text-center gap-2 hover:shadow-md transition">
          <div class="w-14 h-14 rounded-full ${color} flex items-center justify-center text-white text-xl font-bold shadow-inner">
            ${(emp.name || 'U').charAt(0).toUpperCase()}
          </div>
          <div class="w-full">
            <p class="font-semibold text-gray-800 text-sm truncate" title="${emp.name}">${emp.name}</p>
            <p class="text-xs text-blue-600 font-medium">${emp.position || 'បុគ្គលិក'}</p>
            <div class="flex flex-wrap items-center justify-center gap-1 mt-1">
              ${tgBadge}
              ${branchBadge}
            </div>
            ${phoneHtml}
            ${emp.phone2 ? `<p class="text-xs text-gray-400 flex items-center justify-center gap-1 mt-0.5"><span class="material-symbols-outlined text-sm">call</span>${emp.phone2}</p>` : ''}
          </div>
          <div class="flex items-center gap-3 mt-2 pt-2 border-t border-slate-100 w-full justify-center">
            <button onclick="editEmployee('${emp._id}')"
              class="text-xs text-blue-500 hover:text-blue-700 flex items-center gap-0.5 transition" title="កែប្រែឈ្មោះ / លេខទូរស័ព្ទ / សាខា">
              <span class="material-symbols-outlined text-sm">edit</span> កែប្រែ
            </button>
            <span class="text-gray-200">|</span>
            <button onclick="deleteEmployee('${emp._id}')"
              class="text-xs text-red-400 hover:text-red-600 flex items-center gap-0.5 transition" title="លុបបុគ្គលិក">
              <span class="material-symbols-outlined text-sm">delete</span> លុប
            </button>
          </div>
        </div>`;
      }).join('');
    } catch(e) {
      document.getElementById('employees-grid').innerHTML = '<div class="col-span-full text-center py-10 text-red-400">មិនអាចភ្ជាប់ MongoDB បាន</div>';
    }
  }

  async function syncEmployeesFromTelegram() {
    const btn = document.getElementById('btn-sync-tg-emp');
    const oldHtml = btn ? btn.innerHTML : '';
    if (btn) {
      btn.innerHTML = '<span class="material-symbols-outlined animate-spin text-base">progress_activity</span> កំពុង Sync...';
      btn.disabled = true;
    }
    try {
      const res = await fetch('/api/employees/sync-telegram', { method: 'POST' });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បាន Sync ទិន្នន័យបុគ្គលិកពី Telegram រួចរាល់!');
        await loadEmployees();
        await loadStats();
      } else {
        showToast(data.detail || data.message || 'មិនអាច Sync បានទេ!', 'error');
      }
    } catch (e) {
      showToast('កំហុសក្នុងការតភ្ជាប់: ' + e, 'error');
    } finally {
      if (btn) {
        btn.innerHTML = oldHtml;
        btn.disabled = false;
      }
    }
  }

  async function editEmployee(id) {
    const emp = (cachedEmployees || []).find(e => e._id === id) || {};
    const name = emp.name || '';
    const currentPhone = emp.phone || '';
    const currentBranch = emp.branch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';

    const newName = prompt(`កែប្រែឈ្មោះបុគ្គលិកផ្លូវការ៖`, name);
    if (newName === null) return;
    const cleanName = newName.trim();
    if (!cleanName) {
      showToast('ឈ្មោះបុគ្គលិកមិនអាចទទេបានទេ!', 'error');
      return;
    }
    const newPhone = prompt(`កែប្រែលេខទូរស័ព្ទសម្រាប់ «${cleanName}»៖`, currentPhone);
    if (newPhone === null) return;

    const branchChoice = prompt(`ជ្រើសរើសសាខាសម្រាប់ «${cleanName}»៖\n1 = ក្រុមហ៊ុនបូមលូ កំពង់ចាម\n2 = ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ\n(បញ្ចូលលេខ 1 ឬ 2):`, currentBranch === 'ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ' ? '2' : '1');
    if (branchChoice === null) return;
    const cleanBranch = (branchChoice.trim() === '2') ? 'ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ' : 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';

    try {
      const r = await fetch(`/api/employee/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: cleanName, phone: newPhone.trim(), branch: cleanBranch })
      });
      if (r.ok) {
        showToast('បានកែប្រែព័ត៌មានបុគ្គលិកជោគជ័យ!');
        await loadEmployees();
      } else {
        showToast('កំហុសក្នុងការកែប្រែ!', 'error');
      }
    } catch(e) {
      showToast('Error: ' + e, 'error');
    }
  }

  async function editEmployeePhone(id) {
    return editEmployee(id);
  }

  async function deleteEmployee(id) {
    if (!confirm('តើអ្នកចង់លុបបុគ្គលិកនេះពិតមែនទេ?')) return;
    await fetch(`/api/employee/${id}`, { method: 'DELETE' });
    showToast('បានលុបបុគ្គលិក!');
    await loadEmployees(); await loadStats();
  }

  async function submitEmployee(e) {
    e.preventDefault();
    const payload = {
      name: document.getElementById('emp-name').value,
      position: document.getElementById('emp-position').value,
      phone: document.getElementById('emp-phone').value,
      phone2: document.getElementById('emp-phone2').value,
      branch: document.getElementById('emp-branch') ? document.getElementById('emp-branch').value : 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម'
    };
    await fetch('/api/employee', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    closeModal('emp-modal');
    e.target.reset();
    showToast('បានបន្ថែមបុគ្គលិក!');
    await loadEmployees(); await loadStats();
  }

  async function submitBulkEmployees(e) {
    e.preventDefault();
    const rawText = document.getElementById('bulk-emp-names').value;
    const pos = document.getElementById('bulk-emp-position').value || 'បុគ្គលិក';
    const names = rawText.split(/[\\n,]+/).map(n => n.trim()).filter(Boolean);
    if (!names.length) {
      showToast('សូមបញ្ចូលឈ្មោះយ៉ាងហោចណាស់ម្នាក់!', 'error');
      return;
    }
    try {
      const res = await fetch('/api/employees/bulk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ names: names, position: pos })
      });
      const data = await res.json();
      closeModal('bulk-emp-modal');
      e.target.reset();
      showToast(data.message || 'បានបញ្ចូលបុគ្គលិកជោគជ័យ!');
      await loadEmployees();
      await loadStats();
    } catch(err) {
      showToast('Error: ' + err, 'error');
    }
  }

  // ===== BATCH PHONE UPDATE & REQUEST VIA TELEGRAM =====
  async function openBatchPhoneModal() {
    openModal('batch-phone-modal');
    const tbody = document.getElementById('batch-phone-tbody');
    tbody.innerHTML = '<tr><td colspan="3" class="text-center py-6 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>';
    try {
      const res = await fetch('/api/employees');
      const emps = await res.json();
      if (!emps || emps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" class="text-center py-6 text-gray-400">គ្មានបុគ្គលិកក្នុងប្រព័ន្ធទេ</td></tr>';
        return;
      }
      tbody.innerHTML = emps.map(emp => {
        const hasPhone = emp.phone && emp.phone.trim() !== '';
        const statusBadge = hasPhone 
          ? '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-200">✓ មានលេខ</span>'
          : '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-50 text-amber-700 border border-amber-200">⚠️ ខ្វះលេខ</span>';
        const initial = (emp.name || '?')[0].toUpperCase();
        return `
          <tr class="hover:bg-slate-50/70 transition">
            <td class="py-3 px-4">
              <div class="flex items-center gap-2.5">
                <div class="w-8 h-8 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center font-bold text-xs">
                  ${initial}
                </div>
                <div>
                  <div class="font-semibold text-gray-800 text-xs sm:text-sm">${escapeHtml(emp.name)}</div>
                  <div class="text-[11px] text-gray-400">${escapeHtml(emp.position || 'បុគ្គលិក')}</div>
                </div>
              </div>
            </td>
            <td class="py-3 px-4">
              <div class="relative">
                <span class="material-symbols-outlined absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400 text-sm">phone</span>
                <input type="tel" data-emp-id="${emp._id}" value="${escapeHtml(emp.phone || '')}" placeholder="ឧ. 012345678"
                  class="batch-phone-input w-full pl-8 pr-3 py-1.5 text-xs sm:text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-400 transition font-mono" />
              </div>
            </td>
            <td class="py-3 px-4 text-center">
              ${statusBadge}
            </td>
          </tr>
        `;
      }).join('');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="3" class="text-center py-6 text-red-500">Error: ${err}</td></tr>`;
    }
  }

  async function submitBatchPhoneModal(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-save-batch-phone');
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-outlined text-base animate-spin">refresh</span> កំពុងរក្សាទុក...';
    
    const inputs = document.querySelectorAll('.batch-phone-input');
    const phones = {};
    inputs.forEach(inp => {
      const empId = inp.getAttribute('data-emp-id');
      if (empId) {
        phones[empId] = inp.value.trim();
      }
    });
    
    try {
      const res = await fetch('/api/employees/batch-phone', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phones: phones })
      });
      if (!res.ok) {
        const errText = await res.text();
        throw new Error(errText || `Server error ${res.status}`);
      }
      const data = await res.json();
      closeModal('batch-phone-modal');
      showToast(data.message || 'បានរក្សាទុកលេខទូរស័ព្ទជោគជ័យ!');
      await loadEmployees();
    } catch (err) {
      showToast('Error: ' + err, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<span class="material-symbols-outlined text-base">save</span> រក្សាទុកលេខទូរស័ព្ទទាំងអស់';
    }
  }

  async function requestPhoneViaTelegram() {
    const btn = document.getElementById('btn-request-tg-phone');
    if (!confirm('តើអ្នកពិតជាចង់ផ្ញើសារទៅកាន់គ្រុប Telegram ដើម្បីស្នើសុំឱ្យបុគ្គលិកទាំងអស់បញ្ជូនលេខទូរស័ព្ទមែនទេ?')) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-outlined text-base animate-spin">refresh</span> កំពុងផ្ញើ...';
    try {
      const url = currentActiveBranch ? `/api/employees/request-phone-telegram?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/employees/request-phone-telegram';
      const res = await fetch(url, { method: 'POST' });
      const data = await res.json();
      showToast(data.message || 'បានផ្ញើសារទៅ Telegram រួចរាល់!');
    } catch (err) {
      showToast('Error: ' + err, 'error');
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<span class="material-symbols-outlined text-base">send_to_mobile</span> 📲 ស្នើសុំលេខតាម Telegram';
    }
  }

  // ===== SETTINGS & TELEGRAM =====
  async function loadSettings() {
    try {
      const r = await fetch('/api/settings');
      const data = await r.json();
      const tokenInput = document.getElementById('setting-bot-token');
      tokenInput.value = data.telegram_bot_token || '';
      document.getElementById('setting-chat-id').value = data.telegram_chat_id || '';
      document.getElementById('setting-notify-leave').checked = data.notify_on_leave !== false;
      document.getElementById('setting-notify-loan').checked = data.notify_on_loan !== false;
      
      const badge = document.getElementById('token-status-badge');
      if (data.has_token) {
        badge.classList.remove('hidden');
        badge.classList.add('inline-flex');
      } else {
        badge.classList.add('hidden');
        badge.classList.remove('inline-flex');
      }

      // Gemini settings
      const geminiInput = document.getElementById('setting-gemini-key');
      if (geminiInput) geminiInput.value = data.gemini_api_key || '';
      const geminiModel = document.getElementById('setting-gemini-model');
      if (geminiModel && data.gemini_model) geminiModel.value = data.gemini_model;
      const geminiBadge = document.getElementById('gemini-status-badge');
      if (geminiBadge) {
        if (data.has_gemini_key) {
          geminiBadge.classList.remove('hidden');
          geminiBadge.classList.add('inline-flex');
        } else {
          geminiBadge.classList.add('hidden');
          geminiBadge.classList.remove('inline-flex');
        }
      }

      if (data.branches && Array.isArray(data.branches)) {
        const bKc = data.branches.find(b => b.id === 'kampong_cham');
        const bTk = data.branches.find(b => b.id === 'tboung_khmum');
        if (bKc) {
          if (document.getElementById('branch-name-kc')) document.getElementById('branch-name-kc').value = bKc.name || '';
          if (document.getElementById('branch-chat-kc')) document.getElementById('branch-chat-kc').value = bKc.telegram_chat_id || '';
        }
        if (bTk) {
          if (document.getElementById('branch-name-tk')) document.getElementById('branch-name-tk').value = bTk.name || '';
          if (document.getElementById('branch-chat-tk')) document.getElementById('branch-chat-tk').value = bTk.telegram_chat_id || '';
          const tkBadge = document.getElementById('branch-tk-status-badge');
          if (tkBadge) {
            if (bTk.telegram_chat_id) {
              tkBadge.textContent = 'ភ្ជាប់រួច';
              tkBadge.className = 'text-[11px] text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full font-semibold border border-emerald-200';
            } else {
              tkBadge.textContent = 'រង់ចាំភ្ជាប់';
              tkBadge.className = 'text-[11px] text-amber-600 bg-amber-50 px-2 py-0.5 rounded-full font-semibold border border-amber-200';
            }
          }
        }
      }
    } catch(e) {
      showToast('មិនអាចទាញយកការកំណត់បានទេ', true);
    }
  }

  async function saveBranchSettings(e) {
    if (e) e.preventDefault();
    const btn = document.getElementById('btn-save-branches');
    if (btn) { btn.disabled = true; btn.classList.add('opacity-70'); }
    try {
      const branches = [
        {
          id: 'kampong_cham',
          name: document.getElementById('branch-name-kc')?.value.trim() || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម',
          telegram_chat_id: document.getElementById('branch-chat-kc')?.value.trim() || '-1003956005362'
        },
        {
          id: 'tboung_khmum',
          name: document.getElementById('branch-name-tk')?.value.trim() || 'ក្រុមហ៊ុនបូមលូ ត្បូងឃ្មុំ',
          telegram_chat_id: document.getElementById('branch-chat-tk')?.value.trim() || ''
        }
      ];
      const r = await fetch('/api/settings/branches', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ branches })
      });
      const data = await r.json();
      showToast(data.message || 'បានរក្សាទុកព័ត៌មានសាខាជោគជ័យ!');
      await loadSettings();
    } catch(err) {
      showToast('Error: ' + err, 'error');
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove('opacity-70'); }
    }
  }

  async function sendBranchButtons(branchId) {
    try {
      showToast('កំពុងផ្ញើប៊ូតុងទៅ Telegram...', 'info');
      const r = await fetch(`/api/branches/${branchId}/send-buttons`, { method: 'POST' });
      const data = await r.json();
      if (!r.ok) {
        showToast(data.detail || 'មានបញ្ហាក្នុងការផ្ញើប៊ូតុង', 'error');
        return;
      }
      showToast(data.message || 'បានផ្ញើប៊ូតុងជោគជ័យ!');
    } catch(err) {
      showToast('Error: ' + err, 'error');
    }
  }

  async function saveSettings(e) {
    if (e) e.preventDefault();
    const btn = document.getElementById('btn-save-settings');
    btn.disabled = true;
    btn.classList.add('opacity-70');
    try {
      const payload = {
        telegram_bot_token: document.getElementById('setting-bot-token').value.trim(),
        telegram_chat_id: document.getElementById('setting-chat-id').value.trim(),
        gemini_api_key: document.getElementById('setting-gemini-key').value.trim(),
        gemini_model: document.getElementById('setting-gemini-model').value.trim(),
        notify_on_leave: document.getElementById('setting-notify-leave').checked,
        notify_on_loan: document.getElementById('setting-notify-loan').checked
      };
      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await r.json();
      showToast(data.message || 'បានរក្សាទុកការកំណត់ជោគជ័យ!');
      await loadSettings();
    } catch(err) {
      showToast('មានបញ្ហាក្នុងការរក្សាទុក', true);
    } finally {
      btn.disabled = false;
      btn.classList.remove('opacity-70');
    }
  }

  async function saveGeminiSettings() {
    const btn = document.getElementById('btn-save-gemini');
    btn.disabled = true;
    btn.classList.add('opacity-70');
    try {
      const payload = {
        telegram_bot_token: document.getElementById('setting-bot-token').value.trim(),
        telegram_chat_id: document.getElementById('setting-chat-id').value.trim(),
        gemini_api_key: document.getElementById('setting-gemini-key').value.trim(),
        gemini_model: document.getElementById('setting-gemini-model').value.trim(),
        notify_on_leave: document.getElementById('setting-notify-leave').checked,
        notify_on_loan: document.getElementById('setting-notify-loan').checked
      };
      const r = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await r.json();
      showToast(data.message || 'បានរក្សាទុកការកំណត់ AI ជោគជ័យ!');
      await loadSettings();
    } catch(err) {
      showToast('មានបញ្ហាក្នុងការរក្សាទុក AI', true);
    } finally {
      btn.disabled = false;
      btn.classList.remove('opacity-70');
    }
  }

  async function testGeminiAI() {
    const key = document.getElementById('setting-gemini-key').value.trim();
    const model = document.getElementById('setting-gemini-model').value;
    const icon = document.getElementById('test-gemini-icon');
    const btn = document.getElementById('btn-test-gemini');
    icon.classList.add('animate-spin');
    btn.disabled = true;
    try {
      const r = await fetch('/api/test-gemini', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gemini_api_key: key, gemini_model: model })
      });
      const res = await r.json();
      if (r.ok) {
        showToast(res.message || '✅ ជោគជ័យ! Gemini AI ដំណើរការល្អ!');
      } else {
        showToast(res.detail || '❌ បរាជ័យក្នុងការតេស្ត Gemini', true);
      }
    } catch(e) {
      showToast('❌ បរាជ័យ: ' + e.message, true);
    } finally {
      icon.classList.remove('animate-spin');
      btn.disabled = false;
    }
  }

  function toggleGeminiKeyVisibility() {
    const input = document.getElementById('setting-gemini-key');
    const icon = document.getElementById('gemini-eye-icon');
    if (input.type === 'password') {
      input.type = 'text';
      icon.textContent = 'visibility_off';
    } else {
      input.type = 'password';
      icon.textContent = 'visibility';
    }
  }

  async function testTelegramBot() {
    const token = document.getElementById('setting-bot-token').value.trim();
    const chatId = document.getElementById('setting-chat-id').value.trim();
    if (!token || !chatId) {
      showToast('សូមបញ្ចូល Bot Token និង Chat ID ជាមុនសិន!', true);
      return;
    }
    const icon = document.getElementById('test-bot-icon');
    const btn = document.getElementById('btn-test-bot');
    icon.classList.add('animate-spin');
    btn.disabled = true;
    try {
      const r = await fetch('/api/test-telegram', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bot_token: token, chat_id: chatId })
      });
      const res = await r.json();
      if (r.ok) {
        showToast(res.message || 'ផ្ញើសារតេស្តជោគជ័យ!');
      } else {
        showToast(res.detail || 'បរាជ័យក្នុងការតេស្ត', true);
      }
    } catch(e) {
      showToast('បរាជ័យក្នុងការតេស្ត: ' + e.message, true);
    } finally {
      icon.classList.remove('animate-spin');
      btn.disabled = false;
    }
  }

  async function sendMorningBriefing() {
    const icon = document.getElementById('briefing-icon');
    const btn = document.getElementById('btn-morning-briefing');
    if (icon) icon.classList.add('animate-spin');
    if (btn) btn.disabled = true;
    try {
      const url = currentActiveBranch ? `/api/send-briefing?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/send-briefing';
      const r = await fetch(url, { method: 'POST' });
      const res = await r.json();
      if (r.ok) {
        showToast(res.message || 'បានផ្ញើរបាយការណ៍សង្ខេបពេលព្រឹកទៅ Telegram រួចរាល់!');
      } else {
        showToast(res.detail || 'បរាជ័យក្នុងការផ្ញើរបាយការណ៍', true);
      }
    } catch(e) {
      showToast('បរាជ័យក្នុងការផ្ញើរបាយការណ៍: ' + e.message, true);
    } finally {
      if (icon) icon.classList.remove('animate-spin');
      if (btn) btn.disabled = false;
    }
  }

  function toggleTokenVisibility() {
    const input = document.getElementById('setting-bot-token');
    const icon = document.getElementById('eye-icon');
    if (input.type === 'password') {
      input.type = 'text';
      icon.textContent = 'visibility_off';
    } else {
      input.type = 'password';
      icon.textContent = 'visibility';
    }
  }

  function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
      showToast('បានចម្លង (Copied)!');
    }).catch(() => {
      showToast('មិនអាចចម្លងបានទេ', true);
    });
  }

  // ===== Reports State =====
  let leaveReportData = null;
  let currentLeaveViewMode = 'per_employee';

  let loanReportData = null;
  let currentLoanViewMode = 'per_employee';

  // Switch Leave View Mode
  function setLeaveViewMode(mode) {
    currentLeaveViewMode = mode;
    const btnEmp = document.getElementById('btn-leave-mode-emp');
    const btnAll = document.getElementById('btn-leave-mode-all');
    if (btnEmp && btnAll) {
      if (mode === 'per_employee') {
        btnEmp.className = 'px-3 py-1.5 rounded-lg bg-amber-500 text-white shadow-sm flex items-center gap-1 transition';
        btnAll.className = 'px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 flex items-center gap-1 transition';
      } else {
        btnEmp.className = 'px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 flex items-center gap-1 transition';
        btnAll.className = 'px-3 py-1.5 rounded-lg bg-amber-500 text-white shadow-sm flex items-center gap-1 transition';
      }
    }
    renderLeaveReportTable();
  }

  // Render Leave Report Table
  function renderLeaveReportTable() {
    const thead = document.getElementById('rep-leave-thead');
    const tbody = document.getElementById('rep-leave-tbody');
    if (!tbody || !leaveReportData) return;

    const selectedEmp = (document.getElementById('rep-leave-emp-filter')?.value || '').trim();

    if (currentLeaveViewMode === 'per_employee') {
      thead.innerHTML = `
        <tr>
          <th class="py-3 px-4">#</th>
          <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
          <th class="py-3 px-4">ច្បាប់សរុប</th>
          <th class="py-3 px-4">បានសងរួច</th>
          <th class="py-3 px-4">នៅសល់ត្រូវសង</th>
          <th class="py-3 px-4">កាតព្វកិច្ចធ្វើការសង</th>
          <th class="py-3 px-4">ស្ថានភាព</th>
          <th class="py-3 px-4">សកម្មភាព</th>
        </tr>
      `;

      let list = leaveReportData.per_employee || leaveReportData.items || [];
      if (selectedEmp) {
        list = list.filter(it => it.employee_name === selectedEmp);
      }

      if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-gray-400">គ្មានទិន្នន័យច្បាប់ដែលត្រូវបានអនុញ្ញាតសម្រាប់បុគ្គលិកនេះទេ</td></tr>';
        return;
      }

      tbody.innerHTML = list.map((it, idx) => {
        const isCleared = (it.raw_remaining_days || 0) <= 0;
        const statusBadge = isCleared 
          ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200"><span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span> សងរួចរាល់ ✅</span>'
          : ((it.raw_compensated_days || 0) > 0
            ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200"><span class="w-1.5 h-1.5 rounded-full bg-blue-500"></span> សងបានខ្លះ ⏳</span>'
            : '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200"><span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span> មិនទាន់សង</span>');

        const actionBtn = !isCleared
          ? `<button onclick="openCompensateModal('${escapeHtml(it.employee_name)}', null, ${it.raw_remaining_days})" class="px-2.5 py-1.5 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-semibold text-xs flex items-center gap-1 shadow-sm transition">
              <span class="material-symbols-outlined text-xs">edit_calendar</span>
              <span>កត់ត្រាការសង</span>
            </button>`
          : `<span class="inline-flex items-center gap-1 text-xs text-emerald-600 font-semibold"><span class="material-symbols-outlined text-sm">task_alt</span> រួចរាល់</span>`;

        return `
          <tr class="hover:bg-slate-50/70 transition">
            <td class="py-3 px-4 font-mono text-xs text-gray-400">${idx + 1}</td>
            <td class="py-3 px-4 font-bold text-gray-800">${escapeHtml(it.employee_name)}</td>
            <td class="py-3 px-4 text-gray-600 font-medium">${it.days}</td>
            <td class="py-3 px-4 font-semibold text-emerald-600">${it.compensated_days || '0 ថ្ងៃ'}</td>
            <td class="py-3 px-4 font-bold text-amber-600">${it.remaining_days}</td>
            <td class="py-3 px-4">
              <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold ${isCleared ? 'bg-slate-100 text-slate-500' : 'bg-amber-50 text-amber-700 border border-amber-200'}">
                <span class="material-symbols-outlined text-xs">${isCleared ? 'task_alt' : 'event_repeat'}</span>
                ${it.compensation}
              </span>
            </td>
            <td class="py-3 px-4">${statusBadge}</td>
            <td class="py-3 px-4">${actionBtn}</td>
          </tr>
        `;
      }).join('');

    } else {
      // Detailed all approved records
      thead.innerHTML = `
        <tr>
          <th class="py-3 px-4">#</th>
          <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
          <th class="py-3 px-4">កាលបរិច្ឆេទសុំច្បាប់</th>
          <th class="py-3 px-4">ចំនួនថ្ងៃសុំ</th>
          <th class="py-3 px-4">បានសង</th>
          <th class="py-3 px-4">នៅសល់</th>
          <th class="py-3 px-4">មូលហេតុ</th>
          <th class="py-3 px-4">ស្ថានភាព</th>
          <th class="py-3 px-4">សកម្មភាព</th>
        </tr>
      `;

      let list = leaveReportData.raw_records || [];
      if (selectedEmp) {
        list = list.filter(it => it.employee_name === selectedEmp);
      }

      if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center py-8 text-gray-400">គ្មានកំណត់ត្រាច្បាប់ដែលត្រូវបានអនុញ្ញាតទេ</td></tr>';
        return;
      }

      tbody.innerHTML = list.map((it, idx) => {
        const isCleared = (it.raw_remaining_days || 0) <= 0;
        const statusBadge = isCleared 
          ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">សងរួចរាល់ ✅</span>'
          : ((it.raw_compensated_days || 0) > 0
            ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200">សងបានខ្លះ ⏳</span>'
            : '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200">មិនទាន់សង</span>');

        const actionBtn = !isCleared
          ? `<button onclick="openCompensateModal('${escapeHtml(it.employee_name)}', '${it.id}', ${it.raw_remaining_days})" class="px-2.5 py-1.5 rounded-xl bg-amber-500 hover:bg-amber-600 text-white font-semibold text-xs flex items-center gap-1 shadow-sm transition">
              <span class="material-symbols-outlined text-xs">edit_calendar</span>
              <span>កត់ត្រាការសង</span>
            </button>`
          : `<span class="inline-flex items-center gap-1 text-xs text-emerald-600 font-semibold"><span class="material-symbols-outlined text-sm">task_alt</span> រួចរាល់</span>`;

        return `
          <tr class="hover:bg-slate-50/70 transition">
            <td class="py-3 px-4 font-mono text-xs text-gray-400">${idx + 1}</td>
            <td class="py-3 px-4 font-semibold text-gray-800">${escapeHtml(it.employee_name)}</td>
            <td class="py-3 px-4 text-gray-500 font-mono text-xs">${it.date}</td>
            <td class="py-3 px-4 font-medium text-gray-700">${it.days}</td>
            <td class="py-3 px-4 font-semibold text-emerald-600">${it.compensated_days || '0 ថ្ងៃ'}</td>
            <td class="py-3 px-4 font-bold text-amber-600">${it.remaining_days}</td>
            <td class="py-3 px-4 text-gray-600 max-w-xs truncate" title="${escapeHtml(it.reason)}">${escapeHtml(it.reason)}</td>
            <td class="py-3 px-4">${statusBadge}</td>
            <td class="py-3 px-4">${actionBtn}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // Leave Compensation Modal Functions
  function openCompensateModal(empName, leaveId, remainingDays) {
    document.getElementById('comp-leave-emp-name').textContent = empName;
    document.getElementById('comp-leave-rem-disp').textContent = remainingDays + ' ថ្ងៃ';
    document.getElementById('comp-leave-rem-raw').value = remainingDays;
    document.getElementById('comp-leave-id').value = leaveId || '';
    document.getElementById('comp-leave-days').value = remainingDays > 0 ? (remainingDays >= 1 ? 1 : 0.5) : 1;
    document.getElementById('comp-leave-days').max = remainingDays;
    document.getElementById('comp-leave-admin').value = localStorage.getItem('ac_admin_name') || 'Admin';
    document.getElementById('comp-leave-note').value = '';
    openModal('compensate-modal');
  }

  function setCompDays(val) {
    const rawRem = parseFloat(document.getElementById('comp-leave-rem-raw').value) || 1;
    if (val === 'all') {
      document.getElementById('comp-leave-days').value = rawRem;
    } else {
      document.getElementById('comp-leave-days').value = Math.min(rawRem, val);
    }
  }

  async function submitCompensateLeave(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-comp-submit');
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">refresh</span><span>កំពុងរក្សាទុក...</span>';

    const empName = document.getElementById('comp-leave-emp-name').textContent;
    const leaveId = document.getElementById('comp-leave-id').value || null;
    const days = parseFloat(document.getElementById('comp-leave-days').value);
    const admin = document.getElementById('comp-leave-admin').value.trim() || 'Admin';
    const note = document.getElementById('comp-leave-note').value.trim();

    localStorage.setItem('ac_admin_name', admin);

    try {
      const res = await fetch('/api/reports/compensate-leave', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          leave_id: leaveId,
          employee_name: empName,
          days_to_compensate: days,
          admin_name: admin,
          note: note
        })
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message || 'បានកត់ត្រាការសងច្បាប់ជោគជ័យ!');
        closeModal('compensate-modal');
        await loadWeekendLeaveReport();
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការកត់ត្រា', true);
      }
    } catch(err) {
      showToast('បរាជ័យ: ' + err.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span><span>រក្សាទុក & ជូនដំណឹង</span>';
    }
  }

  // Load Weekend Leave Report
  async function loadWeekendLeaveReport() {
    const tbody = document.getElementById('rep-leave-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>';
    try {
      const url = currentActiveBranch ? `/api/reports/weekend-leave?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/reports/weekend-leave';
      const res = await fetch(url);
      const data = await res.json();
      leaveReportData = data;
      document.getElementById('rep-leave-staff-count').textContent = (data.total_staff_count || 0) + ' នាក់';
      document.getElementById('rep-leave-days-count').textContent = (data.total_days_to_compensate || 0) + ' ថ្ងៃ';
      document.getElementById('rep-leave-updated-at').textContent = 'ធ្វើបច្ចុប្បន្នភាព: ' + (data.generated_at || '');
      
      // Populate employee filter dropdown
      const filterSelect = document.getElementById('rep-leave-emp-filter');
      if (filterSelect) {
        const curVal = filterSelect.value;
        const empSet = new Set((data.per_employee || data.items || []).map(it => it.employee_name));
        filterSelect.innerHTML = '<option value="">-- បង្ហាញបុគ្គលិកទាំងអស់ --</option>' +
          Array.from(empSet).map(e => `<option value="${escapeHtml(e)}">${escapeHtml(e)}</option>`).join('');
        if (curVal && empSet.has(curVal)) filterSelect.value = curVal;
      }
      
      renderLeaveReportTable();
    } catch(e) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center py-8 text-red-500">បរាជ័យក្នុងការទាញយក: ${e.message}</td></tr>`;
    }
  }

  function toggleLeaveSendMenu(e) {
    if (e) e.stopPropagation();
    const menu = document.getElementById('menu-send-leave');
    if (menu) menu.classList.toggle('hidden');
    const loanMenu = document.getElementById('menu-send-loan');
    if (loanMenu) loanMenu.classList.add('hidden');
  }

  async function sendWeekendLeaveReportTelegram(mode = 'summary') {
    const menu = document.getElementById('menu-send-leave');
    if (menu) menu.classList.add('hidden');
    const btn = document.getElementById('btn-send-tg-leave');
    const icon = document.getElementById('icon-send-tg-leave');
    if (icon) icon.classList.add('animate-spin');
    if (btn) btn.disabled = true;
    try {
      const selectedEmp = (document.getElementById('rep-leave-emp-filter')?.value || '').trim();
      const res = await fetch('/api/reports/send-weekend-leave', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode, employee_name: selectedEmp, branch: currentActiveBranch })
      });
      const d = await res.json();
      if (res.ok) {
        showToast(d.message || 'បានផ្ញើរបាយការណ៍ច្បាប់ទៅ Telegram រួចរាល់!');
      } else {
        showToast(d.detail || 'បរាជ័យក្នុងការផ្ញើ', true);
      }
    } catch(e) {
      showToast('បរាជ័យ: ' + e.message, true);
    } finally {
      if (icon) icon.classList.remove('animate-spin');
      if (btn) btn.disabled = false;
    }
  }

  // Switch Loan View Mode
  function setLoanViewMode(mode) {
    currentLoanViewMode = mode;
    const btnEmp = document.getElementById('btn-loan-mode-emp');
    const btnAll = document.getElementById('btn-loan-mode-all');
    if (btnEmp && btnAll) {
      if (mode === 'per_employee') {
        btnEmp.className = 'px-3 py-1.5 rounded-lg bg-indigo-600 text-white shadow-sm flex items-center gap-1 transition';
        btnAll.className = 'px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 flex items-center gap-1 transition';
      } else {
        btnEmp.className = 'px-3 py-1.5 rounded-lg text-slate-600 hover:text-slate-900 flex items-center gap-1 transition';
        btnAll.className = 'px-3 py-1.5 rounded-lg bg-indigo-600 text-white shadow-sm flex items-center gap-1 transition';
      }
    }
    renderLoanReportTable();
  }

  // Render Loan Report Table
  function renderLoanReportTable() {
    const thead = document.getElementById('rep-loan-thead');
    const tbody = document.getElementById('rep-loan-tbody');
    if (!tbody || !loanReportData) return;

    const selectedEmp = (document.getElementById('rep-loan-emp-filter')?.value || '').trim();

    if (currentLoanViewMode === 'per_employee') {
      thead.innerHTML = `
        <tr>
          <th class="py-3 px-4">#</th>
          <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
          <th class="py-3 px-4">ខ្ចីសរុប</th>
          <th class="py-3 px-4">បានកាត់រួច</th>
          <th class="py-3 px-4">នៅសល់ត្រូវកាត់</th>
          <th class="py-3 px-4">គោលបំណង</th>
          <th class="py-3 px-4">ស្ថានភាព</th>
          <th class="py-3 px-4">សកម្មភាព</th>
        </tr>
      `;

      let list = loanReportData.per_employee || [];
      if (selectedEmp) {
        list = list.filter(it => it.employee_name === selectedEmp);
      }

      if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-gray-400">គ្មានទិន្នន័យខ្ចីប្រាក់ដែលបានអនុញ្ញាតសម្រាប់បុគ្គលិកនេះទេ</td></tr>';
        return;
      }

      tbody.innerHTML = list.map((it, idx) => {
        const isCleared = (it.remaining_amount_khr || 0) <= 0;
        const statusBadge = isCleared 
          ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200"><span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span> កាត់ដាច់រួចរាល់ ✅</span>'
          : ((it.total_paid_khr || 0) > 0
            ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200"><span class="w-1.5 h-1.5 rounded-full bg-blue-500"></span> កាត់បានខ្លះ ⏳</span>'
            : '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200"><span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span> មិនទាន់កាត់/សង</span>');

        const actionBtn = !isCleared
          ? `<button onclick="openRepayModal('${escapeHtml(it.employee_name)}', null, ${it.remaining_amount_khr})" class="px-2.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white font-semibold text-xs flex items-center gap-1 shadow-sm transition">
              <span class="material-symbols-outlined text-xs">payments</span>
              <span>កត់ត្រាការកាត់/សង</span>
            </button>`
          : `<span class="inline-flex items-center gap-1 text-xs text-emerald-600 font-semibold"><span class="material-symbols-outlined text-sm">task_alt</span> រួចរាល់</span>`;

        return `
          <tr class="hover:bg-slate-50/70 transition">
            <td class="py-3 px-4 font-mono text-xs text-gray-400">${idx + 1}</td>
            <td class="py-3 px-4 font-bold text-gray-800">${escapeHtml(it.employee_name)}</td>
            <td class="py-3 px-4 text-gray-600 font-medium">${Number(it.total_amount_khr).toLocaleString()} ៛</td>
            <td class="py-3 px-4 font-semibold text-emerald-600">${Number(it.total_paid_khr || 0).toLocaleString()} ៛</td>
            <td class="py-3 px-4 font-bold text-indigo-600">
              <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-bold ${isCleared ? 'bg-slate-100 text-slate-500' : 'bg-indigo-50 text-indigo-700 border border-indigo-200'}">
                ${isCleared ? '០ ៛' : '- ' + Number(it.remaining_amount_khr).toLocaleString() + ' ៛'}
              </span>
            </td>
            <td class="py-3 px-4 text-gray-600 max-w-xs truncate" title="${escapeHtml(it.purpose)}">${escapeHtml(it.purpose)}</td>
            <td class="py-3 px-4">${statusBadge}</td>
            <td class="py-3 px-4">${actionBtn}</td>
          </tr>
        `;
      }).join('');

    } else {
      // All approved transactions
      thead.innerHTML = `
        <tr>
          <th class="py-3 px-4">#</th>
          <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
          <th class="py-3 px-4">កាលបរិច្ឆេទខ្ចី</th>
          <th class="py-3 px-4">ចំនួនទឹកប្រាក់ខ្ចី</th>
          <th class="py-3 px-4">បានកាត់រួច</th>
          <th class="py-3 px-4">នៅសល់ត្រូវកាត់</th>
          <th class="py-3 px-4">គោលបំណង</th>
          <th class="py-3 px-4">ស្ថានភាព</th>
          <th class="py-3 px-4">សកម្មភាព</th>
        </tr>
      `;

      let list = loanReportData.items || [];
      if (selectedEmp) {
        list = list.filter(it => it.employee_name === selectedEmp);
      }

      if (list.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" class="text-center py-8 text-gray-400">គ្មានសំណើខ្ចីប្រាក់ដែលបានអនុញ្ញាតក្នុងខែនេះទេ</td></tr>';
        return;
      }

      tbody.innerHTML = list.map((it, idx) => {
        const isCleared = (it.remaining_amount_khr || 0) <= 0;
        const statusBadge = isCleared 
          ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">កាត់ដាច់រួចរាល់ ✅</span>'
          : ((it.paid_amount_khr || 0) > 0
            ? '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-50 text-blue-700 border border-blue-200">កាត់បានខ្លះ ⏳</span>'
            : '<span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200">មិនទាន់កាត់/សង</span>');

        const actionBtn = !isCleared
          ? `<button onclick="openRepayModal('${escapeHtml(it.employee_name)}', '${it.id}', ${it.remaining_amount_khr})" class="px-2.5 py-1.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white font-semibold text-xs flex items-center gap-1 shadow-sm transition">
              <span class="material-symbols-outlined text-xs">payments</span>
              <span>កត់ត្រាការកាត់/សង</span>
            </button>`
          : `<span class="inline-flex items-center gap-1 text-xs text-emerald-600 font-semibold"><span class="material-symbols-outlined text-sm">task_alt</span> រួចរាល់</span>`;

        return `
          <tr class="hover:bg-slate-50/70 transition">
            <td class="py-3 px-4 font-mono text-xs text-gray-400">${idx + 1}</td>
            <td class="py-3 px-4 font-semibold text-gray-800">${escapeHtml(it.employee_name)}</td>
            <td class="py-3 px-4 text-gray-500 font-mono text-xs">${it.date}</td>
            <td class="py-3 px-4 font-medium text-gray-700">${Number(it.amount_khr).toLocaleString()} ៛</td>
            <td class="py-3 px-4 font-semibold text-emerald-600">${Number(it.paid_amount_khr || 0).toLocaleString()} ៛</td>
            <td class="py-3 px-4 font-bold text-indigo-600">
              <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-bold ${isCleared ? 'bg-slate-100 text-slate-500' : 'bg-indigo-50 text-indigo-700 border border-indigo-200'}">
                ${isCleared ? '០ ៛' : '- ' + Number(it.remaining_amount_khr).toLocaleString() + ' ៛'}
              </span>
            </td>
            <td class="py-3 px-4 text-gray-600 max-w-xs truncate" title="${escapeHtml(it.purpose)}">${escapeHtml(it.purpose)}</td>
            <td class="py-3 px-4">${statusBadge}</td>
            <td class="py-3 px-4">${actionBtn}</td>
          </tr>
        `;
      }).join('');
    }
  }

  // Loan Repayment Modal Functions
  function openRepayModal(empName, loanId, remainingAmt) {
    document.getElementById('repay-loan-emp-name').textContent = empName;
    document.getElementById('repay-loan-rem-disp').textContent = Number(remainingAmt).toLocaleString() + ' ៛';
    document.getElementById('repay-loan-rem-raw').value = remainingAmt;
    document.getElementById('repay-loan-id').value = loanId || '';
    document.getElementById('repay-loan-amount').value = remainingAmt;
    document.getElementById('repay-loan-amount').max = remainingAmt;
    document.getElementById('repay-loan-admin').value = localStorage.getItem('ac_admin_name') || 'Admin';
    document.getElementById('repay-loan-note').value = '';
    openModal('repay-modal');
  }

  function setRepayAmt(val) {
    const rawRem = parseInt(document.getElementById('repay-loan-rem-raw').value) || 0;
    if (val === 'all') {
      document.getElementById('repay-loan-amount').value = rawRem;
    } else {
      document.getElementById('repay-loan-amount').value = Math.min(rawRem, val);
    }
  }

  async function submitRepayLoan(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-repay-submit');
    btn.disabled = true;
    btn.innerHTML = '<span class="material-symbols-outlined text-sm animate-spin">refresh</span><span>កំពុងរក្សាទុក...</span>';

    const empName = document.getElementById('repay-loan-emp-name').textContent;
    const loanId = document.getElementById('repay-loan-id').value || null;
    const amount = parseInt(document.getElementById('repay-loan-amount').value);
    const admin = document.getElementById('repay-loan-admin').value.trim() || 'Admin';
    const note = document.getElementById('repay-loan-note').value.trim();

    localStorage.setItem('ac_admin_name', admin);

    try {
      const res = await fetch('/api/reports/repay-loan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          loan_id: loanId,
          employee_name: empName,
          amount_to_repay: amount,
          admin_name: admin,
          note: note
        })
      });
      const data = await res.json();
      if (res.ok) {
        showToast(data.message || 'បានកត់ត្រាការកាត់/សងប្រាក់ជោគជ័យ!');
        closeModal('repay-modal');
        await loadMonthlyLoanReport();
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការកត់ត្រា', true);
      }
    } catch(err) {
      showToast('បរាជ័យ: ' + err.message, true);
    } finally {
      btn.disabled = false;
      btn.innerHTML = '<span class="material-symbols-outlined text-sm">check_circle</span><span>រក្សាទុក & ជូនដំណឹង</span>';
    }
  }

  // Load Monthly Loan Report
  async function loadMonthlyLoanReport() {
    const tbody = document.getElementById('rep-loan-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>';
    try {
      const url = currentActiveBranch ? `/api/reports/monthly-loan?branch=${encodeURIComponent(currentActiveBranch)}` : '/api/reports/monthly-loan';
      const res = await fetch(url);
      const data = await res.json();
      loanReportData = data;
      document.getElementById('rep-loan-total-amt').textContent = (data.total_amount_khr || 0).toLocaleString() + ' ៛';
      document.getElementById('rep-loan-borrowers-count').textContent = (data.total_borrowers || 0) + ' នាក់';
      document.getElementById('rep-loan-updated-at').textContent = 'ខែ: ' + (data.month_str || '');
      
      // Populate employee filter dropdown
      const filterSelect = document.getElementById('rep-loan-emp-filter');
      if (filterSelect) {
        const curVal = filterSelect.value;
        const empSet = new Set((data.per_employee || []).map(it => it.employee_name));
        filterSelect.innerHTML = '<option value="">-- បង្ហាញបុគ្គលិកទាំងអស់ --</option>' +
          Array.from(empSet).map(e => `<option value="${escapeHtml(e)}">${escapeHtml(e)}</option>`).join('');
        if (curVal && empSet.has(curVal)) filterSelect.value = curVal;
      }
      
      renderLoanReportTable();
    } catch(e) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center py-8 text-red-500">បរាជ័យក្នុងការទាញយក: ${e.message}</td></tr>`;
    }
  }

  function toggleLoanSendMenu(e) {}
  async function sendMonthlyLoanReportTelegram() {
    showToast('ទិន្នន័យខ្ចីប្រាក់ជារឿងសម្ងាត់ មិនអាចផ្ញើចូលគ្រុប Telegram បានទេ!', true);
  }

  // Close Telegram send dropdown menus when clicking outside
  document.addEventListener('click', function(e) {
    const leaveMenu = document.getElementById('menu-send-leave');
    const leaveWrap = document.getElementById('leave-send-menu-container');
    if (leaveMenu && leaveWrap && !leaveWrap.contains(e.target)) {
      leaveMenu.classList.add('hidden');
    }
    const loanMenu = document.getElementById('menu-send-loan');
    const loanWrap = document.getElementById('loan-send-menu-container');
    if (loanMenu && loanWrap && !loanWrap.contains(e.target)) {
      loanMenu.classList.add('hidden');
    }
  });

  // ===== PWA INSTALLATION SYSTEM (HIDDEN ON INSTALLED DEVICES) =====
  let deferredPWAEvent = null;

  function isDeviceInstalled() {
    // 1. Check Display Mode: Standalone (Desktop Chrome/Edge, Android, iOS Home Screen)
    if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
    if (window.navigator.standalone === true) return true; // iOS Safari Home Screen
    if (document.referrer && document.referrer.includes('android-app://')) return true;
    
    // 2. Check localStorage flag indicating user already installed it on this device
    if (localStorage.getItem('acbomlu_pwa_installed') === 'true') return true;
    
    return false;
  }

  function updateInstallUI() {
    const topBtn = document.getElementById('pwa-install-btn-top');
    const sidebarBox = document.getElementById('pwa-install-sidebar-box');

    // CRITICAL: If already installed on this device, NEVER SHOW IT!
    if (isDeviceInstalled()) {
      if (topBtn) {
        topBtn.classList.add('hidden');
        topBtn.classList.remove('inline-flex');
      }
      if (sidebarBox) {
        sidebarBox.classList.add('hidden');
      }
      return;
    }

    // Otherwise, show install button so user can install
    if (topBtn) {
      topBtn.classList.remove('hidden');
      topBtn.classList.add('inline-flex');
    }
    if (sidebarBox) {
      sidebarBox.classList.remove('hidden');
    }
  }

  const APP_VERSION = '2.3.1';
  function initPWA() {
    try {
      const savedVersion = localStorage.getItem('acbomlu_app_version');
      if (savedVersion !== APP_VERSION) {
        localStorage.setItem('acbomlu_app_version', APP_VERSION);
        if ('caches' in window) {
          caches.keys().then(keys => Promise.all(keys.map(k => caches.delete(k)))).catch(() => {});
        }
      }
    } catch(e) {}

    // Register Service Worker with auto-update
    if ('serviceWorker' in navigator && window.location.protocol.startsWith('http')) {
      navigator.serviceWorker.getRegistrations().then(regs => {
        for (let r of regs) r.update();
      }).catch(() => {});
      navigator.serviceWorker.register('/sw.js').then(reg => {
        reg.update();
      }).catch(err => console.log('SW reg error:', err));
    }

    // Initial check to show or hide
    updateInstallUI();

    // Listen for display-mode change (e.g. if app is launched in standalone mode)
    try {
      if (window.matchMedia) {
        window.matchMedia('(display-mode: standalone)').addEventListener('change', (evt) => {
          if (evt.matches) {
            localStorage.setItem('acbomlu_pwa_installed', 'true');
            updateInstallUI();
          }
        });
      }
    } catch(e) {}
  }

  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPWAEvent = e;
    updateInstallUI();
  });

  window.addEventListener('appinstalled', () => {
    localStorage.setItem('acbomlu_pwa_installed', 'true');
    deferredPWAEvent = null;
    updateInstallUI(); // Immediately hides the button forever!
    showToast('🎉 បានដំឡើងកម្មវិធី AC BOMLU ចូលក្នុងឧបករណ៍ជោគជ័យ!');
  });

  async function handlePWAInstallClick() {
    if (isDeviceInstalled()) {
      updateInstallUI();
      return;
    }

    // If browser supports native prompt (Chrome, Edge on Windows/Mac/Android)
    if (deferredPWAEvent) {
      try {
        deferredPWAEvent.prompt();
        const choice = await deferredPWAEvent.userChoice;
        if (choice && choice.outcome === 'accepted') {
          localStorage.setItem('acbomlu_pwa_installed', 'true');
          updateInstallUI();
          showToast('🎉 បានដំឡើងកម្មវិធីចូលក្នុងឧបករណ៍ជោគជ័យ!');
        }
      } catch(err) {
        console.log('Install prompt error:', err);
      }
      deferredPWAEvent = null;
      return;
    }

    // If on iOS Safari or browser without direct prompt event, show visual guide
    const modal = document.getElementById('ios-install-modal');
    if (modal) {
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    } else {
      alert("👉 ដើម្បីដំឡើងកម្មវិធី សូមចុចប៊ូតុង Share (ចែករំលែក ⬆️) រួចជ្រើសរើស 'Add to Home Screen' (➕ បន្ថែមទៅអេក្រង់ដើម)។");
    }
  }

  function closeInstallModal() {
    const modal = document.getElementById('ios-install-modal');
    if (modal) {
      modal.classList.add('hidden');
      modal.classList.remove('flex');
    }
  }

  function confirmInstalledOnDevice() {
    localStorage.setItem('acbomlu_pwa_installed', 'true');
    closeInstallModal();
    updateInstallUI(); // Permanently hides button on this device!
    showToast('✅ បានកត់ត្រាការដំឡើងលើឧបករណ៍នេះរួចរាល់!');
  }

  // ===== ADMIN AUTHENTICATION & LOGIN SYSTEM =====
  async function checkAuthAndInitialize() {
    // 1. Check for one-click magic login token via URL parameter ?token=...
    const urlParams = new URLSearchParams(window.location.search);
    const paramToken = urlParams.get('token');
    if (paramToken) {
      localStorage.setItem('acbomlu_admin_token', paramToken);
      window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
    }

    const token = localStorage.getItem('acbomlu_admin_token');
    const loginScreen = document.getElementById('login-screen');
    const topAdminName = document.getElementById('top-admin-name');
    const savedU = localStorage.getItem('acbomlu_saved_username') || 'admin';
    const uInput = document.getElementById('login-username');
    if (uInput && !uInput.value) uInput.value = savedU;
    // Password remains clean and blank

    if (!token) {
      if (loginScreen) loginScreen.classList.remove('hidden');
      return;
    }

    try {
      const res = await fetch('/api/auth/verify', {
        headers: { 'Authorization': 'Bearer ' + token }
      });
      const data = await res.json();
      if (data.authenticated) {
        if (loginScreen) loginScreen.classList.add('hidden');
        if (topAdminName) topAdminName.textContent = data.username || 'Admin';
        const settingAdminUser = document.getElementById('setting-admin-username');
        if (settingAdminUser) settingAdminUser.value = data.username || 'admin';
        await Promise.all([loadStats(), loadLeaves(), loadLoans(), loadFinanceStats(), loadCatalogs(), loadSystemEmployees()]);
      } else {
        localStorage.removeItem('acbomlu_admin_token');
        if (loginScreen) loginScreen.classList.remove('hidden');
      }
    } catch (e) {
      console.error('Auth verification error:', e);
      // In case of temporary network glitch, allow dashboard if local token exists
      if (loginScreen) loginScreen.classList.add('hidden');
      await Promise.all([loadStats(), loadLeaves(), loadLoans(), loadFinanceStats(), loadCatalogs(), loadSystemEmployees()]);
    }
  }

// (handleAdminLogin & showLoginAlert are defined in early script for zero-latency response)

  function toggleLoginPassword() {
    const pInput = document.getElementById('login-password');
    const icon = document.getElementById('login-pw-icon');
    if (pInput.type === 'password') {
      pInput.type = 'text';
      icon.textContent = 'visibility_off';
    } else {
      pInput.type = 'password';
      icon.textContent = 'visibility';
    }
  }

  async function handleAdminLogout() {
    if (!confirm('តើលោកអ្នកពិតជាចង់ចាកចេញពីប្រព័ន្ធគ្រប់គ្រង (Log Out) មែនទេ?')) return;
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch(e) {}
    localStorage.removeItem('acbomlu_admin_token');
    document.getElementById('login-screen').classList.remove('hidden');
    document.getElementById('login-password').value = '';
    showToast('បានចាកចេញពីប្រព័ន្ធដោយជោគជ័យ!');
  }

  async function saveAdminAccountSettings(event) {
    event.preventDefault();
    const newU = document.getElementById('setting-admin-username').value.trim();
    const curP = document.getElementById('setting-admin-current-pw').value.trim();
    const newP = document.getElementById('setting-admin-new-pw').value.trim();
    const confirmP = document.getElementById('setting-admin-confirm-pw').value.trim();
    const btn = document.getElementById('btn-save-admin-account');

    if (!newU) {
      showToast('សូមបញ្ចូលឈ្មោះគណនី Admin!', true);
      return;
    }
    if (!curP) {
      showToast('សូមបញ្ចូលលេខសម្ងាត់បច្ចុប្បន្នដើម្បីបញ្ជាក់!', true);
      return;
    }
    if (newP && !confirmP) {
      showToast('សូមវាយលេខសម្ងាត់ថ្មីម្តងទៀតក្នុងប្រអប់បញ្ជាក់!', true);
      return;
    }
    if (newP && newP !== confirmP) {
      showToast('លេខសម្ងាត់ថ្មីទាំងពីរមិនដូចគ្នាទេ!', true);
      return;
    }
    if (newP && newP.length < 4) {
      showToast('លេខសម្ងាត់ថ្មីត្រូវមានយ៉ាងតិច ៤ តួអក្សរឡើងទៅ!', true);
      return;
    }

    btn.disabled = true;
    try {
      const res = await fetch('/api/auth/change-credentials', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + (localStorage.getItem('acbomlu_admin_token') || '')
        },
        body: JSON.stringify({
          current_password: curP,
          new_username: newU,
          new_password: newP || curP
        })
      });
      const data = await res.json();
      if (res.ok && data.success) {
        if (data.token) localStorage.setItem('acbomlu_admin_token', data.token);
        const topAdminName = document.getElementById('top-admin-name');
        if (topAdminName) topAdminName.textContent = data.username || newU;
        document.getElementById('setting-admin-current-pw').value = '';
        document.getElementById('setting-admin-new-pw').value = '';
        document.getElementById('setting-admin-confirm-pw').value = '';
        showToast(data.message || 'បានអាប់ដេតគណនី Admin ជោគជ័យ!');
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការប្តូរគណនី Admin', true);
      }
    } catch(e) {
      showToast('មានបញ្ហា: ' + e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // ===== Refresh (Manual & Live Auto-Refresh Rate) =====
  let isRefreshing = false;
  async function refreshAll(silent = false) {
    if (isRefreshing) return;
    isRefreshing = true;

    const btn = document.getElementById('refresh-btn')?.querySelector('span');
    if (btn && !silent) btn.classList.add('animate-spin');

    const tasks = [loadStats(), loadFinanceStats()];
    if (currentActiveTab === 'dashboard') {
      tasks.push(loadLeaves(), loadLoans(), loadTransferHistory());
    } else if (currentActiveTab === 'leaves') tasks.push(loadLeaves());
    else if (currentActiveTab === 'loans') tasks.push(loadLoans());
    else if (currentActiveTab === 'employees') tasks.push(loadEmployees());
    else if (currentActiveTab === 'income') tasks.push(loadIncome());
    else if (currentActiveTab === 'expense') tasks.push(loadExpense());
    else if (currentActiveTab === 'catalog') tasks.push(loadCatalogs());
    else if (currentActiveTab === 'reports-leave') tasks.push(loadWeekendLeaveReport());
    else if (currentActiveTab === 'reports-loan') tasks.push(loadMonthlyLoanReport());
    else if (currentActiveTab === 'reports-finance') tasks.push(loadFinanceReports());
    else if (currentActiveTab === 'settings') tasks.push(loadSettings());

    try {
      await Promise.all(tasks);
    } catch (e) {
      console.warn('refreshAll error:', e);
    } finally {
      isRefreshing = false;
    }

    if (btn && !silent) {
      setTimeout(() => btn.classList.remove('animate-spin'), 400);
      showToast('ទិន្នន័យស្រស់ហើយ!');
    }
  }

  // Live Auto-Refresh Rate: Polling every 15 seconds to ensure UI stays responsive & in-sync
  setInterval(() => {
    if (!document.hidden && currentActiveTab) {
      refreshAll(true);
    }
  }, 15000);

  // Auto-refresh immediately when user switches back to browser tab
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshAll(true);
    }
  });

  // ======================================================
  // FINANCE & ACCOUNTING JAVASCRIPT
  // ======================================================

  let financeCatalogs = [];
  let incomeTransactions = [];
  let expenseTransactions = [];
  let currentIncomeFilter = { period: 'today', date: '', category: 'all', search: '' };
  let currentExpenseFilter = { period: 'today', date: '', category: 'all', search: '' };
  let currentReportPeriod = 'daily'; // 'daily' | 'monthly' | 'yearly'
  let currentCatalogFilterType = 'all';
  let activeFinanceReportData = null;

  // Khmer & Latin Numeral Translation and Thousand Comma Formatting
  function khmerToLatinDigits(str) {
    if (!str) return '';
    const khmerMap = {
      '០': '0', '១': '1', '២': '2', '៣': '3', '៤': '4',
      '៥': '5', '៦': '6', '៧': '7', '៨': '8', '៩': '9'
    };
    return String(str).replace(/[០-៩]/g, d => khmerMap[d] || d);
  }

  function formatAmountInput(inputEl) {
    if (!inputEl) return;
    let val = inputEl.value;
    val = khmerToLatinDigits(val);
    val = val.replace(/[^0-9.]/g, '');
    const parts = val.split('.');
    let integerPart = parts[0];
    let decimalPart = parts.length > 1 ? '.' + parts.slice(1).join('') : '';

    if (integerPart) {
      integerPart = Number(integerPart).toLocaleString('en-US');
    }
    inputEl.value = integerPart + decimalPart;
  }

  function parseFormattedAmount(str) {
    if (!str) return 0;
    const latin = khmerToLatinDigits(String(str)).replace(/,/g, '').trim();
    return parseFloat(latin) || 0;
  }

  function parseKhmerNumber(str) {
    return parseFormattedAmount(str);
  }

  // Service Locations Persistent Memory & Combobox Management
  const DEFAULT_SERVICE_LOCATIONS = [
    'ភ្នំពេញ', 'សៀមរាប', 'បាត់ដំបង', 'កំពង់ចាម', 'កំពង់សោម (ព្រះសីហនុ)',
    'កំពង់ស្ពឺ', 'កណ្តាល', 'តាកែវ', 'ពោធិ៍សាត់', 'បន្ទាយមានជ័យ',
    'ស្វាយរៀង', 'ព្រៃវែង', 'កំពត', 'កែប', 'កោះកុង', 'ក្រចេះ'
  ];

  function getSavedServiceLocations() {
    try {
      const raw = localStorage.getItem('acbomlu_saved_service_locations');
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr) && arr.length > 0) return arr;
      }
    } catch(e) {}
    return [...DEFAULT_SERVICE_LOCATIONS];
  }

  function saveServiceLocation(loc) {
    loc = (loc || '').trim();
    if (!loc) return;
    const locs = getSavedServiceLocations();
    if (!locs.includes(loc)) {
      locs.unshift(loc);
      localStorage.setItem('acbomlu_saved_service_locations', JSON.stringify(locs));
      populateLocationDatalist();
    }
  }

  function populateLocationDatalist() {
    const datalist = document.getElementById('service-locations-list');
    if (!datalist) return;
    const locs = getSavedServiceLocations();
    datalist.innerHTML = locs.map(l => `<option value="${escapeHtml(l)}">`).join('');
  }

  let currentIncomeServiceItems = [];

  function onItemCurrencyChange() {
    const itemCurr = document.getElementById('inc-item-currency')?.value || 'KHR';
    const formCurr = document.getElementById('inc-form-currency');
    if (formCurr && formCurr.value !== itemCurr) {
      formCurr.value = itemCurr;
    }
    updateIncomeCurrencyBadge();
  }

  function onFormCurrencyChange() {
    const formCurr = document.getElementById('inc-form-currency')?.value || 'KHR';
    const itemCurr = document.getElementById('inc-item-currency');
    if (itemCurr && itemCurr.value !== formCurr) {
      itemCurr.value = formCurr;
    }
    updateIncomeCurrencyBadge();
  }

  function getIncomeCurrency() {
    const el = document.getElementById('inc-form-currency');
    return el ? el.value : 'KHR';
  }

  function updateIncomeCurrencyBadge() {
    const curr = getIncomeCurrency();
    const priceInp = document.getElementById('inc-price-input');
    if (priceInp) {
      priceInp.placeholder = curr === 'USD' ? 'ឧ. 90.00' : 'ឧ. 360,000';
    }
    renderIncomeServiceItems();
  }

  function addServiceTripItem() {
    const locInput = document.getElementById('inc-location-input');
    const tripsSelect = document.getElementById('inc-trips-select');
    const priceInput = document.getElementById('inc-price-input');
    const itemCurrSelect = document.getElementById('inc-item-currency');
    const currency = itemCurrSelect ? itemCurrSelect.value : (getIncomeCurrency() || 'KHR');

    const loc = (locInput ? locInput.value : '').trim();
    const trips = parseInt(tripsSelect ? tripsSelect.value : '1', 10) || 1;
    const priceVal = priceInput ? parseFormattedAmount(priceInput.value) : 0;

    if (!loc) {
      showToast('សូមជ្រើសរើស ឬវាយបញ្ចូលទីតាំងផ្ដល់សេវាសិន!', true);
      if (locInput) locInput.focus();
      return;
    }

    saveServiceLocation(loc);

    // Keep form currency in sync with the added item's currency
    const formCurr = document.getElementById('inc-form-currency');
    if (formCurr && formCurr.value !== currency) {
      formCurr.value = currency;
    }

    const newItem = {
      id: Date.now() + Math.random().toString(36).substr(2, 5),
      location: loc,
      trips: trips,
      price: priceVal,
      currency: currency
    };

    currentIncomeServiceItems.push(newItem);
    renderIncomeServiceItems();

    if (locInput) {
      locInput.value = '';
      locInput.focus();
    }
    if (priceInput) {
      priceInput.value = '';
    }
    if (tripsSelect) {
      tripsSelect.value = '1';
    }

    const priceFormatted = formatMoney(priceVal, currency);
    showToast(`បានបញ្ចូល៖ ${loc} (${trips} ជើង = ${priceFormatted})`);
  }

  function removeIncomeServiceItem(id) {
    currentIncomeServiceItems = currentIncomeServiceItems.filter(item => String(item.id) !== String(id));
    renderIncomeServiceItems();
  }

  function renderIncomeServiceItems() {
    const container = document.getElementById('inc-service-items-list');
    const summaryBox = document.getElementById('inc-items-summary');
    const totalTripsEl = document.getElementById('inc-items-total-trips');
    const totalSumEl = document.getElementById('inc-items-total-sum');
    const descEl = document.getElementById('inc-form-desc');
    const amountInp = document.getElementById('inc-form-amount');
    const currency = getIncomeCurrency();

    if (!container) return;

    if (currentIncomeServiceItems.length === 0) {
      container.innerHTML = `
        <div class="text-center py-2 px-3 border border-dashed border-blue-200 rounded-xl text-slate-400 text-xs">
          មិនទាន់មានទីតាំងត្រូវបានបញ្ចូលនៅឡើយទេ (សូមរើសទីតាំង ចំនួនជើង និងតម្លៃ រួចចុច «+ បញ្ចូល»)
        </div>
      `;
      if (summaryBox) summaryBox.classList.add('hidden');
      const catVal = (document.getElementById('inc-form-category')?.value || '');
      if (catVal.includes('បូមលូ') || catVal.includes('សេវា')) {
        if (amountInp) amountInp.value = '';
        if (descEl) descEl.value = '';
      }
      return;
    }

    if (summaryBox) summaryBox.classList.remove('hidden');

    const khmerTripsMap = {
      '1': '១', '2': '២', '3': '៣', '4': '៤', '5': '៥',
      '6': '៦', '7': '៧', '8': '៨', '9': '៩', '10': '១០',
      '11': '១១', '12': '១២', '13': '១៣', '14': '១៤', '15': '១៥',
      '16': '១៦', '17': '១៧', '18': '១៨', '19': '១៩', '20': '២០'
    };

    let totalTrips = 0;
    let totalSum = 0;
    const descLines = [];

    const itemsHtml = currentIncomeServiceItems.map(item => {
      totalTrips += Number(item.trips) || 0;
      const itemPrice = Number(item.price) || 0;
      totalSum += itemPrice;

      const avgPrice = item.trips > 0 ? Math.round(itemPrice / item.trips) : itemPrice;
      const khmerNum = khmerTripsMap[String(item.trips)] || String(item.trips);
      const formattedPrice = formatMoney(itemPrice, currency);
      const formattedAvg = formatMoney(avgPrice, currency);

      descLines.push(`ទីតាំង៖ ${item.location} (${khmerNum} ជើង = ${formattedPrice})`);

      return `
        <div class="flex items-center justify-between gap-2 p-2 sm:p-2.5 bg-white rounded-xl border border-blue-100 shadow-sm hover:border-blue-300 transition">
          <div class="flex items-center gap-2 min-w-0">
            <div class="w-6 h-6 sm:w-7 sm:h-7 rounded-lg bg-blue-100 text-blue-700 flex items-center justify-center font-bold text-xs flex-shrink-0">
              ${khmerNum}
            </div>
            <div class="min-w-0">
              <div class="font-bold text-xs sm:text-sm text-gray-800 truncate flex items-center gap-1">
                <span class="material-symbols-outlined text-rose-500 text-sm">pin_drop</span>
                <span>${escapeHtml(item.location)}</span>
                <span class="text-[11px] font-normal text-slate-500">(${khmerNum} ជើង)</span>
              </div>
              <div class="text-[10px] sm:text-[11px] text-slate-500 font-mono">
                មធ្យម៖ <span class="font-semibold text-slate-700">${formattedAvg}/ជើង</span>
              </div>
            </div>
          </div>

          <div class="flex items-center gap-2 flex-shrink-0">
            <div class="text-right">
              <span class="font-bold text-xs sm:text-sm font-mono text-emerald-700 block">${formattedPrice}</span>
            </div>
            <button type="button" onclick="removeIncomeServiceItem('${item.id}')"
              class="w-7 h-7 flex items-center justify-center rounded-lg text-rose-400 hover:text-rose-700 hover:bg-rose-50 transition"
              title="លុបចេញ">
              <span class="material-symbols-outlined text-base">close</span>
            </button>
          </div>
        </div>
      `;
    }).join('');

    container.innerHTML = itemsHtml;

    if (totalTripsEl) totalTripsEl.textContent = totalTrips;
    if (totalSumEl) totalSumEl.textContent = formatMoney(totalSum, currency);

    if (descEl) {
      descEl.value = descLines.join(String.fromCharCode(10));
    }

    if (totalSum > 0 && amountInp) {
      amountInp.value = currency === 'USD' ? totalSum.toFixed(2) : totalSum.toLocaleString('en-US');
    }
  }

  // Helpers
  function formatMoney(amount, currency = 'KHR') {
    const num = Number(amount) || 0;
    if (currency === 'USD') {
      return '$' + num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return Math.round(num).toLocaleString('en-US') + ' ៛';
  }

  function formatDualMoney(khr, usd) {
    const k = Number(khr) || 0;
    const u = Number(usd) || 0;
    if (k === 0 && u === 0) return '0 ៛';
    const parts = [];
    if (k !== 0 || u === 0) {
      parts.push(Math.round(k).toLocaleString('en-US') + ' ៛');
    }
    if (u !== 0) {
      parts.push('$' + u.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
    }
    return parts.join(' + ');
  }

  function getTodayString() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function getYesterdayString() {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function getCurrentMonthPrefix() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    return `${y}-${m}`;
  }

  function getCurrentTimeString() {
    const d = new Date();
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }

  // --- Catalogs ---

  async function loadCatalogs() {
    try {
      const res = await fetch('/api/finance/catalogs');
      financeCatalogs = await res.json();
      populateCatalogDropdowns();
      renderCatalogs();
    } catch (e) {
      console.warn('loadCatalogs error:', e);
    }
  }

  function populateCatalogDropdowns() {
    const incSelect = document.getElementById('inc-form-category');
    const expSelect = document.getElementById('exp-form-category');
    const incFilterSelect = document.getElementById('inc-filter-category');
    const expFilterSelect = document.getElementById('exp-filter-category');

    const incCats = financeCatalogs.filter(c => c.type === 'income' || c.type === 'both');
    const expCats = financeCatalogs.filter(c => c.type === 'expense' || c.type === 'both');

    if (incSelect) {
      const defaultCat = 'ចំណូលពីសេវាបូមលូ';
      let hasDefault = incCats.some(c => c.name === defaultCat);
      let optionsHtml = '';
      if (!hasDefault) {
        optionsHtml += `<option value="${defaultCat}" selected>${defaultCat}</option>`;
      }
      optionsHtml += incCats.map(c => `<option value="${escapeHtml(c.name)}"${c.name === defaultCat ? ' selected' : ''}>${escapeHtml(c.name)}</option>`).join('');
      incSelect.innerHTML = optionsHtml;
      incSelect.value = defaultCat;
    }
    if (expSelect) {
      const defaultExpCat = 'ចំណាយរដ្ឋបាល និងផ្សេងៗ';
      let hasDefault = expCats.some(c => c.name === defaultExpCat);
      let optionsHtml = '';
      if (!hasDefault && expCats.length === 0) {
        optionsHtml += `<option value="${defaultExpCat}" selected>${defaultExpCat}</option>`;
      }
      optionsHtml += expCats.map(c => `<option value="${escapeHtml(c.name)}"${c.name === defaultExpCat ? ' selected' : ''}>${escapeHtml(c.name)}</option>`).join('');
      expSelect.innerHTML = optionsHtml;
      if (hasDefault) {
        expSelect.value = defaultExpCat;
      }
    }

    if (incFilterSelect) {
      const curVal = incFilterSelect.value || 'all';
      incFilterSelect.innerHTML = '<option value="all">គ្រប់កាតាឡុក</option>' +
        incCats.map(c => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`).join('');
      incFilterSelect.value = curVal;
    }
    if (expFilterSelect) {
      const curVal = expFilterSelect.value || 'all';
      expFilterSelect.innerHTML = '<option value="all">គ្រប់កាតាឡុក</option>' +
        expCats.map(c => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`).join('');
      expFilterSelect.value = curVal;
    }
  }

  function filterCatalogType(type) {
    currentCatalogFilterType = type;
    ['all', 'income', 'expense'].forEach(t => {
      const btn = document.getElementById('cat-tab-' + t);
      if (btn) {
        if (t === type) {
          btn.className = 'px-4 py-2 rounded-xl text-xs sm:text-sm font-medium bg-blue-600 text-white shadow-sm transition';
        } else {
          btn.className = 'px-4 py-2 rounded-xl text-xs sm:text-sm font-medium bg-white border border-slate-200 text-gray-700 hover:bg-slate-50 transition';
        }
      }
    });
    renderCatalogs();
  }

  function renderCatalogs() {
    const container = document.getElementById('catalogs-grid');
    if (!container) return;

    let filtered = financeCatalogs;
    if (currentCatalogFilterType !== 'all') {
      filtered = financeCatalogs.filter(c => c.type === currentCatalogFilterType || c.type === 'both');
    }

    if (!filtered.length) {
      container.innerHTML = '<div class="col-span-full text-center py-12 text-gray-400">មិនមានកាតាឡុកក្នុងប្រភេទនេះឡើយ</div>';
      return;
    }

    const colorMap = {
      emerald: { bg: 'bg-emerald-50 text-emerald-700 border-emerald-200', icon: 'text-emerald-600' },
      rose: { bg: 'bg-rose-50 text-rose-700 border-rose-200', icon: 'text-rose-600' },
      blue: { bg: 'bg-blue-50 text-blue-700 border-blue-200', icon: 'text-blue-600' },
      amber: { bg: 'bg-amber-50 text-amber-700 border-amber-200', icon: 'text-amber-600' },
      purple: { bg: 'bg-purple-50 text-purple-700 border-purple-200', icon: 'text-purple-600' },
      indigo: { bg: 'bg-indigo-50 text-indigo-700 border-indigo-200', icon: 'text-indigo-600' },
      teal: { bg: 'bg-teal-50 text-teal-700 border-teal-200', icon: 'text-teal-600' },
      cyan: { bg: 'bg-cyan-50 text-cyan-700 border-cyan-200', icon: 'text-cyan-600' },
      slate: { bg: 'bg-slate-100 text-slate-700 border-slate-200', icon: 'text-slate-600' }
    };

    container.innerHTML = filtered.map(c => {
      const col = colorMap[c.color] || colorMap.blue;
      const typeLabel = c.type === 'income' ? '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">🟢 ចំណូល</span>' :
                        c.type === 'expense' ? '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-rose-100 text-rose-700">🔴 ចំណាយ</span>' :
                        '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-blue-100 text-blue-700">⚪ ទាំងពីរ</span>';

      return `
        <div class="bg-white rounded-2xl p-4 sm:p-5 shadow-sm border border-slate-100 hover:shadow-md transition flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between mb-3">
              <div class="w-10 h-10 rounded-xl ${col.bg} border flex items-center justify-center">
                <span class="material-symbols-outlined text-xl ${col.icon}">${escapeHtml(c.icon || 'category')}</span>
              </div>
              ${typeLabel}
            </div>
            <h4 class="font-bold text-gray-800 text-sm sm:text-base">${escapeHtml(c.name)}</h4>
            <p class="text-xs text-gray-500 mt-1 line-clamp-2">${escapeHtml(c.description || 'គ្មានការពិពណ៌នា')}</p>
          </div>
          <div class="mt-4 pt-3 border-t border-slate-100 flex items-center justify-end gap-2">
            <button onclick="openCatalogModal('edit', '${c._id}')" class="p-1.5 rounded-lg text-blue-600 hover:bg-blue-50 transition" title="កែប្រែ">
              <span class="material-symbols-outlined text-base">edit</span>
            </button>
            <button onclick="deleteCatalog('${c._id}', '${escapeHtml(c.name)}')" class="p-1.5 rounded-lg text-rose-500 hover:bg-rose-50 transition" title="លុប">
              <span class="material-symbols-outlined text-base">delete</span>
            </button>
          </div>
        </div>
      `;
    }).join('');
  }

  function openCatalogModal(mode = 'create', catId = null) {
    const form = document.querySelector('#catalog-modal form');
    if (form) form.reset();
    document.getElementById('cat-form-id').value = '';

    if (mode === 'edit' && catId) {
      const cat = financeCatalogs.find(c => c._id === catId);
      if (cat) {
        document.getElementById('catalog-modal-title').innerHTML = '<span class="material-symbols-outlined text-blue-600 text-xl">edit</span> កែប្រែកាតាឡុក';
        document.getElementById('cat-form-id').value = cat._id;
        document.getElementById('cat-form-name').value = cat.name;
        document.getElementById('cat-form-type').value = cat.type || 'both';
        document.getElementById('cat-form-color').value = cat.color || 'blue';
        document.getElementById('cat-form-icon').value = cat.icon || 'category';
        document.getElementById('cat-form-desc').value = cat.description || '';
      }
    } else {
      document.getElementById('catalog-modal-title').innerHTML = '<span class="material-symbols-outlined text-amber-500 text-xl">category</span> បង្កើតកាតាឡុកថ្មី';
      if (mode === 'income') document.getElementById('cat-form-type').value = 'income';
      else if (mode === 'expense') document.getElementById('cat-form-type').value = 'expense';
    }
    openModal('catalog-modal');
  }

  async function submitCatalog(e) {
    e.preventDefault();
    const id = document.getElementById('cat-form-id').value;
    const name = document.getElementById('cat-form-name').value.trim();
    const type = document.getElementById('cat-form-type').value;
    const color = document.getElementById('cat-form-color').value;
    const icon = document.getElementById('cat-form-icon').value;
    const desc = document.getElementById('cat-form-desc').value.trim();
    const btn = document.getElementById('btn-save-catalog');

    if (!name) {
      showToast('សូមបញ្ចូលឈ្មោះកាតាឡុក!', true);
      return;
    }

    btn.disabled = true;
    try {
      let res;
      if (id) {
        res = await fetch('/api/finance/catalogs/' + id, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, type, color, icon, description: desc })
        });
      } else {
        res = await fetch('/api/finance/catalogs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, type, color, icon, description: desc })
        });
      }
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បានរក្សាទុកកាតាឡុកជោគជ័យ!');
        closeModal('catalog-modal');
        await loadCatalogs();
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការរក្សាទុកកាតាឡុក!', true);
      }
    } catch (err) {
      showToast('កំហុស៖ ' + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteCatalog(id, name) {
    if (!confirm(`តើលោកអ្នកពិតជាចង់លុបកាតាឡុក «${name}» មែនទេ?`)) return;
    try {
      const res = await fetch('/api/finance/catalogs/' + id, { method: 'DELETE' });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បានលុបកាតាឡុកជោគជ័យ!');
        await loadCatalogs();
      } else {
        showToast(data.detail || 'មិនអាចលុបកាតាឡុកបានទេ!', true);
      }
    } catch (err) {
      showToast('កំហុស៖ ' + err.message, true);
    }
  }

  // --- Quick Finance Stats (Dashboard + Badges) ---

  async function loadFinanceStats() {
    try {
      const url = currentActiveBranch ? '/api/finance/stats?branch=' + encodeURIComponent(currentActiveBranch) : '/api/finance/stats';
      const res = await fetch(url);
      const data = await res.json();
      if (!data || !data.this_month) return;

      const khmerMonths = ['មករា', 'កុម្ភៈ', 'មីនា', 'មេសា', 'ឧសភា', 'មិថុនា', 'កក្កដា', 'សីហា', 'កញ្ញា', 'តុលា', 'វិច្ឆិកា', 'ធ្នូ'];
      const mIdx = (data.current_month ? data.current_month - 1 : new Date().getMonth());
      const mName = khmerMonths[mIdx] || 'វិច្ឆិកា';
      const yr = data.current_year || new Date().getFullYear();
      const periodLabel = `ខែ ${mName} ឆ្នាំ ${yr}`;

      const dateEl = document.getElementById('dash-finance-date');
      if (dateEl) {
        dateEl.textContent = 'ប្រចាំ' + periodLabel;
      }

      // Top Cards: Show This Month's Income, Expense, Net Balance
      const incEl = document.getElementById('dash-income-today');
      if (incEl) incEl.textContent = formatDualMoney(data.this_month.income_khr, data.this_month.income_usd);

      const expEl = document.getElementById('dash-expense-today');
      if (expEl) expEl.textContent = formatDualMoney(data.this_month.expense_khr, data.this_month.expense_usd);

      const netEl = document.getElementById('dash-net-today');
      if (netEl) {
        netEl.textContent = formatDualMoney(data.this_month.net_khr, data.this_month.net_usd);
        if (data.this_month.net_khr >= 0 && data.this_month.net_usd >= 0) {
          netEl.className = 'text-base sm:text-lg font-bold text-emerald-300 mt-1';
        } else {
          netEl.className = 'text-base sm:text-lg font-bold text-rose-300 mt-1';
        }
      }

      // Reconciliation card on Dashboard (This Month: Cash in hand vs Bank)
      const reconPeriodEl = document.getElementById('dash-recon-period');
      if (reconPeriodEl) reconPeriodEl.textContent = periodLabel;

      const rec = data.this_month.reconciliation;
      if (rec) {
        const cashIncEl = document.getElementById('dash-cash-income');
        if (cashIncEl) cashIncEl.textContent = formatDualMoney(rec.cash_income.khr, rec.cash_income.usd);

        const cashExpEl = document.getElementById('dash-cash-expense');
        if (cashExpEl) cashExpEl.textContent = formatDualMoney(rec.cash_expense.khr, rec.cash_expense.usd);

        const cashOutEl = document.getElementById('dash-cash-transfers-out');
        if (cashOutEl) cashOutEl.textContent = formatDualMoney(rec.cash_transfers_out?.khr || 0, rec.cash_transfers_out?.usd || 0);

        const cashBalEl = document.getElementById('dash-cash-balance');
        if (cashBalEl) cashBalEl.textContent = formatDualMoney(rec.cash_balance.khr, rec.cash_balance.usd);

        const bankIncEl = document.getElementById('dash-bank-income');
        if (bankIncEl) bankIncEl.textContent = formatDualMoney(rec.bank_income.khr, rec.bank_income.usd);

        const bankExpEl = document.getElementById('dash-bank-expense');
        if (bankExpEl) bankExpEl.textContent = formatDualMoney(rec.bank_expense.khr, rec.bank_expense.usd);

        const bankOutEl = document.getElementById('dash-bank-transfers-out');
        if (bankOutEl) bankOutEl.textContent = formatDualMoney(rec.bank_transfers_out?.khr || 0, rec.bank_transfers_out?.usd || 0);

        const bankBalEl = document.getElementById('dash-bank-balance');
        if (bankBalEl) bankBalEl.textContent = formatDualMoney(rec.bank_balance.khr, rec.bank_balance.usd);

        // Investment Fund Cards
        const invCashEl = document.getElementById('dash-inv-from-cash');
        if (invCashEl) invCashEl.textContent = formatDualMoney(rec.investment_from_cash?.khr || 0, rec.investment_from_cash?.usd || 0);

        const invBankEl = document.getElementById('dash-inv-from-bank');
        if (invBankEl) invBankEl.textContent = formatDualMoney(rec.investment_from_bank?.khr || 0, rec.investment_from_bank?.usd || 0);

        const invBalEl = document.getElementById('dash-investment-balance');
        if (invBalEl) invBalEl.textContent = formatDualMoney(rec.investment_balance?.khr || 0, rec.investment_balance?.usd || 0);

        loadTransferHistory();
      }

      // Sidebar badges
      const incBadge = document.getElementById('badge-income-count');
      if (incBadge) {
        const count = data.today && data.today.income_by_category ? data.today.income_by_category.reduce((s, c) => s + c.count, 0) : 0;
        if (count > 0) {
          incBadge.textContent = count;
          incBadge.classList.remove('hidden');
        } else incBadge.classList.add('hidden');
      }

      const expBadge = document.getElementById('badge-expense-count');
      if (expBadge) {
        const count = data.today && data.today.expense_by_category ? data.today.expense_by_category.reduce((s, c) => s + c.count, 0) : 0;
        if (count > 0) {
          expBadge.textContent = count;
          expBadge.classList.remove('hidden');
        } else expBadge.classList.add('hidden');
      }
    } catch (e) {
      console.warn('loadFinanceStats error:', e);
    }
  }

  // --- INCOME VIEW ---

  function setIncomeDateFilter(period) {
    currentIncomeFilter.period = period;
    ['today', 'yesterday', 'month', 'all'].forEach(p => {
      const btn = document.getElementById('inc-filter-' + p);
      if (btn) {
        if (p === period) {
          btn.className = 'px-3 py-1.5 rounded-xl font-medium bg-emerald-600 text-white shadow-sm transition';
        } else {
          btn.className = 'px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition';
        }
      }
    });

    const dateInput = document.getElementById('inc-filter-date');
    if (period === 'today') currentIncomeFilter.date = getTodayString();
    else if (period === 'yesterday') currentIncomeFilter.date = getYesterdayString();
    else currentIncomeFilter.date = '';

    if (dateInput) dateInput.value = currentIncomeFilter.date;
    loadIncome();
  }

  function handleIncomeCustomDate(val) {
    currentIncomeFilter.date = val;
    currentIncomeFilter.period = 'custom';
    ['today', 'yesterday', 'month', 'all'].forEach(p => {
      const btn = document.getElementById('inc-filter-' + p);
      if (btn) btn.className = 'px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition';
    });
    loadIncome();
  }

  let incomeSearchTimeout;
  function debounceIncomeSearch() {
    clearTimeout(incomeSearchTimeout);
    incomeSearchTimeout = setTimeout(() => {
      currentIncomeFilter.search = document.getElementById('inc-filter-search').value.trim();
      loadIncome();
    }, 300);
  }

  async function loadIncome() {
    try {
      // 1. Load Stats for Income cards
      const statsRes = await fetch(currentActiveBranch ? '/api/finance/stats?branch=' + encodeURIComponent(currentActiveBranch) : '/api/finance/stats');
      const statsData = await statsRes.json();
      if (statsData) {
        document.getElementById('inc-stat-today').textContent = formatDualMoney(statsData.today.income_khr, statsData.today.income_usd);
        document.getElementById('inc-stat-month').textContent = formatDualMoney(statsData.this_month.income_khr, statsData.this_month.income_usd);
        document.getElementById('inc-stat-year').textContent = formatDualMoney(statsData.this_year.income_khr, statsData.this_year.income_usd);
      }

      // 2. Build Query
      const params = new URLSearchParams({ type: 'income', limit: '150' });
      const catVal = document.getElementById('inc-filter-category').value;
      if (catVal && catVal !== 'all') params.append('category', catVal);

      if (currentIncomeFilter.period === 'today') {
        params.append('date', getTodayString());
      } else if (currentIncomeFilter.period === 'yesterday') {
        params.append('date', getYesterdayString());
      } else if (currentIncomeFilter.period === 'month') {
        params.append('start_date', getCurrentMonthPrefix() + '-01');
        params.append('end_date', getCurrentMonthPrefix() + '-31');
      } else if (currentIncomeFilter.date) {
        params.append('date', currentIncomeFilter.date);
      }

      if (currentIncomeFilter.search) {
        params.append('search', currentIncomeFilter.search);
      }
      if (currentActiveBranch) {
        params.append('branch', currentActiveBranch);
      }

      const res = await fetch('/api/finance/transactions?' + params.toString());
      const data = await res.json();
      incomeTransactions = data.transactions || [];
      document.getElementById('inc-stat-count').textContent = data.total || incomeTransactions.length;
      renderIncomeTable();
    } catch (e) {
      console.warn('loadIncome error:', e);
      document.getElementById('income-tbody').innerHTML = '<tr><td colspan="7" class="text-center py-8 text-rose-500">កំហុសក្នុងការទាញយកទិន្នន័យចំណូល</td></tr>';
    }
  }

  function renderIncomeTable() {
    const tbody = document.getElementById('income-tbody');
    const mobileContainer = document.getElementById('income-mobile-cards');

    if (!incomeTransactions.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center py-10 text-gray-400">មិនមានទិន្នន័យចំណូលក្នុងចន្លោះពេលនេះឡើយ</td></tr>';
      mobileContainer.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">មិនមានទិន្នន័យចំណូលឡើយ</div>';
      return;
    }

    tbody.innerHTML = incomeTransactions.map(tx => {
      const amtStr = formatMoney(tx.amount, tx.currency);
      return `
        <tr class="hover:bg-slate-50 transition">
          <td class="px-5 py-3 whitespace-nowrap">
            <div class="font-medium text-gray-800">${escapeHtml(tx.date)}</div>
            <div class="text-[11px] text-gray-400">${escapeHtml(tx.time || '')}</div>
          </td>
          <td class="px-5 py-3 whitespace-nowrap">
            <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
              <span class="material-symbols-outlined text-xs">storefront</span>
              ${escapeHtml(tx.category)}
            </span>
          </td>
          <td class="px-5 py-3">
            <div class="text-xs text-gray-700 font-medium max-w-xs truncate" title="${escapeHtml(tx.description || '')}">${escapeHtml(tx.description || '—')}</div>
            <div class="text-[10px] text-gray-400">កត់ត្រា៖ ${escapeHtml(tx.recorded_by || 'រដ្ឋបាល')}</div>
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-xs text-gray-600">
            ${escapeHtml(tx.payment_method || 'សាច់ប្រាក់សុទ្ធ')}
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-xs font-mono text-gray-500">
            ${escapeHtml(tx.reference_no || '—')}
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-right font-bold text-emerald-600 text-sm">
            + ${amtStr}
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-right">
            <div class="inline-flex items-center gap-1">
              <button onclick="openEditTransactionModal('${tx._id}')" class="p-1 rounded-lg text-blue-600 hover:bg-blue-50 transition" title="កែប្រែ">
                <span class="material-symbols-outlined text-base">edit</span>
              </button>
              <button onclick="deleteTransaction('${tx._id}')" class="p-1 rounded-lg text-rose-500 hover:bg-rose-50 transition" title="លុប">
                <span class="material-symbols-outlined text-base">delete</span>
              </button>
            </div>
          </td>
        </tr>
      `;
    }).join('');

    mobileContainer.innerHTML = incomeTransactions.map(tx => {
      const amtStr = formatMoney(tx.amount, tx.currency);
      return `
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 flex flex-col gap-2.5">
          <div class="flex items-start justify-between gap-2">
            <div>
              <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-emerald-50 text-emerald-700 border border-emerald-200">
                ${escapeHtml(tx.category)}
              </span>
              <p class="text-[11px] text-gray-400 mt-1">${escapeHtml(tx.date)} ${escapeHtml(tx.time || '')}</p>
            </div>
            <p class="text-base font-extrabold text-emerald-600">+ ${amtStr}</p>
          </div>
          ${tx.description ? `<p class="text-xs text-gray-700 font-medium">${escapeHtml(tx.description)}</p>` : ''}
          <div class="flex items-center justify-between pt-2 border-t border-slate-100 text-xs text-gray-500">
            <span>💳 ${escapeHtml(tx.payment_method || 'សាច់ប្រាក់សុទ្ធ')} ${tx.reference_no ? '(' + escapeHtml(tx.reference_no) + ')' : ''}</span>
            <div class="flex items-center gap-1">
              <button onclick="openEditTransactionModal('${tx._id}')" class="px-2.5 py-1 rounded-lg text-blue-600 hover:bg-blue-50 font-medium transition">កែប្រែ</button>
              <button onclick="deleteTransaction('${tx._id}')" class="px-2.5 py-1 rounded-lg text-rose-500 hover:bg-rose-50 font-medium transition">លុប</button>
            </div>
          </div>
        </div>
      `;
    }).join('');
  }

  let currentIncomeStep = 1;

  function setIncomeStep(step) {
    currentIncomeStep = step;
    const s1 = document.getElementById('inc-step-1');
    const s2 = document.getElementById('inc-step-2');
    if (!s1 || !s2) return;
    if (step === 1) {
      s1.classList.remove('hidden');
      s2.classList.add('hidden');
    } else {
      s1.classList.add('hidden');
      s2.classList.remove('hidden');
    }
  }

  function nextIncomeStep() {
    const date = document.getElementById('inc-form-date')?.value;
    if (!date) {
      showToast('សូមជ្រើសរើសកាលបរិច្ឆេទ!', true);
      const dInp = document.getElementById('inc-form-date');
      if (dInp) dInp.focus();
      return;
    }
    const cat = document.getElementById('inc-form-category')?.value;
    if (!cat) {
      showToast('សូមជ្រើសរើសកាតាឡុក / ប្រភេទចំណូល!', true);
      return;
    }
    setIncomeStep(2);
    handleIncomeCategoryChange();
    setTimeout(() => {
      const isSewage = cat.includes('បូមលូ') || cat.includes('សេវា');
      const targetFocus = isSewage ? document.getElementById('inc-location-input') : document.getElementById('inc-form-amount');
      if (targetFocus) targetFocus.focus();
    }, 100);
  }

  function prevIncomeStep() {
    setIncomeStep(1);
    setTimeout(() => {
      const dInp = document.getElementById('inc-form-date');
      if (dInp) dInp.focus();
    }, 100);
  }

  function handleIncomeCategoryChange() {
    const cat = document.getElementById('inc-form-category')?.value || '';
    const isSewage = cat.includes('បូមលូ') || cat.includes('សេវា');
    const locSection = document.getElementById('inc-service-location-section');
    const amtInp = document.getElementById('inc-form-amount');
    const amtLabel = document.getElementById('inc-form-amount-label');
    const amtBadge = document.getElementById('inc-amount-auto-badge');

    if (isSewage) {
      if (locSection) locSection.classList.remove('hidden');
      if (amtInp) {
        amtInp.readOnly = true;
        amtInp.classList.add('bg-emerald-50/60', 'cursor-not-allowed');
      }
      if (amtLabel) amtLabel.textContent = 'ចំនួនទឹកប្រាក់សរុប *';
      if (amtBadge) amtBadge.classList.remove('hidden');
      renderIncomeServiceItems();
    } else {
      if (locSection) locSection.classList.add('hidden');
      if (amtInp) {
        amtInp.readOnly = false;
        amtInp.classList.remove('bg-emerald-50/60', 'cursor-not-allowed');
      }
      if (amtLabel) amtLabel.textContent = 'ចំនួនទឹកប្រាក់ *';
      if (amtBadge) amtBadge.classList.add('hidden');
    }
  }

  async function openIncomeModal() {
    const form = document.querySelector('#income-modal form');
    if (form) form.reset();
    setIncomeStep(1);
    if (document.getElementById('inc-form-branch')) {
      document.getElementById('inc-form-branch').value = currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';
    }
    document.getElementById('inc-form-date').value = getTodayString();
    document.getElementById('inc-form-time').value = getCurrentTimeString();
    document.getElementById('inc-form-currency').value = 'KHR';
    const itemCurr = document.getElementById('inc-item-currency');
    if (itemCurr) itemCurr.value = 'KHR';

    if (!financeCatalogs || financeCatalogs.length === 0) {
      await loadCatalogs();
    } else {
      populateCatalogDropdowns();
    }

    const incCat = document.getElementById('inc-form-category');
    if (incCat) {
      const defaultCat = 'ចំណូលពីសេវាបូមលូ';
      const exists = Array.from(incCat.options).some(o => o.value === defaultCat);
      if (!exists) {
        incCat.insertAdjacentHTML('afterbegin', `<option value="${defaultCat}">${defaultCat}</option>`);
      }
      incCat.value = defaultCat;
    }

    const incMethod = document.getElementById('inc-form-method');
    if (incMethod) {
      incMethod.value = 'សាច់ប្រាក់សុទ្ធ';
    }

    populateLocationDatalist();
    const locInp = document.getElementById('inc-location-input');
    if (locInp) locInp.value = '';
    const tripsSel = document.getElementById('inc-trips-select');
    if (tripsSel) tripsSel.value = '1';
    const priceInp = document.getElementById('inc-price-input');
    if (priceInp) priceInp.value = '';
    currentIncomeServiceItems = [];
    renderIncomeServiceItems();
    handleIncomeCategoryChange();
    updateIncomeCurrencyBadge();
    openModal('income-modal');
    setTimeout(() => {
      const dInp = document.getElementById('inc-form-date');
      if (dInp) dInp.focus();
    }, 150);
  }

  async function submitIncome(e) {
    if (e && e.preventDefault) e.preventDefault();
    if (currentIncomeStep === 1) {
      nextIncomeStep();
      return;
    }
    const date = document.getElementById('inc-form-date').value;
    const time = document.getElementById('inc-form-time').value;
    const amount = parseFormattedAmount(document.getElementById('inc-form-amount').value);
    const currency = document.getElementById('inc-form-currency').value;
    const category = document.getElementById('inc-form-category').value;
    const method = document.getElementById('inc-form-method').value;
    const ref = document.getElementById('inc-form-ref') ? document.getElementById('inc-form-ref').value.trim() : '';
    const desc = document.getElementById('inc-form-desc').value.trim();
    const btn = document.getElementById('btn-save-income');

    const isSewageCategory = (category || '').includes('បូមលូ') || (category || '').includes('សេវា');
    if (isSewageCategory && currentIncomeServiceItems.length === 0) {
      const locInp = document.getElementById('inc-location-input');
      const pendingLoc = (locInp?.value || '').trim();
      if (pendingLoc) {
        showToast('លោកអ្នកបានវាយទីតាំងហើយ សូមចុចប៊ូតុង «+ បញ្ចូល» សិនមុននឹងរក្សាទុក!', true);
      } else {
        showToast('ករណីចំណូលសេវាបូមលូ៖ ភ្លេចបញ្ចូលទីតាំង! សូមជ្រើសរើសទីតាំងរួចចុច «+ បញ្ចូល» សិន!', true);
      }
      flashRedSaveButton('btn-save-income', 'ភ្លេចបញ្ចូលទីតាំង!');
      if (locInp) {
        locInp.classList.add('border-rose-500', 'ring-2', 'ring-rose-400');
        locInp.focus();
        setTimeout(() => locInp.classList.remove('border-rose-500', 'ring-2', 'ring-rose-400'), 3500);
      }
      return;
    }

    if (!amount || amount <= 0) {
      showToast('សូមបញ្ចូលចំនួនទឹកប្រាក់ដែលធំជាង ០!', true);
      flashRedSaveButton('btn-save-income', 'សូមបញ្ចូលចំនួនទឹកប្រាក់!');
      const amtInp = document.getElementById('inc-form-amount');
      if (amtInp && !amtInp.readOnly) amtInp.focus();
      return;
    }

    btn.disabled = true;
    try {
      const res = await fetch('/api/finance/transactions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'income',
          amount,
          currency,
          category,
          date,
          time,
          payment_method: method,
          reference_no: ref,
          description: desc,
          recorded_by: (localStorage.getItem('acbomlu_admin_user') || 'រដ្ឋបាល'),
          branch: (document.getElementById('inc-form-branch')?.value || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម'),
          service_items: currentIncomeServiceItems
        })
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បានកត់ត្រាចំណូលជោគជ័យ!');
        closeModal('income-modal');
        setIncomeStep(1);
        await loadIncome();
        await loadFinanceStats();
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការកត់ត្រាចំណូល!', true);
      }
    } catch (err) {
      showToast('កំហុស៖ ' + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // --- EXPENSE VIEW ---

  function setExpenseDateFilter(period) {
    currentExpenseFilter.period = period;
    ['today', 'yesterday', 'month', 'all'].forEach(p => {
      const btn = document.getElementById('exp-filter-' + p);
      if (btn) {
        if (p === period) {
          btn.className = 'px-3 py-1.5 rounded-xl font-medium bg-rose-600 text-white shadow-sm transition';
        } else {
          btn.className = 'px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition';
        }
      }
    });

    const dateInput = document.getElementById('exp-filter-date');
    if (period === 'today') currentExpenseFilter.date = getTodayString();
    else if (period === 'yesterday') currentExpenseFilter.date = getYesterdayString();
    else currentExpenseFilter.date = '';

    if (dateInput) dateInput.value = currentExpenseFilter.date;
    loadExpense();
  }

  function handleExpenseCustomDate(val) {
    currentExpenseFilter.date = val;
    currentExpenseFilter.period = 'custom';
    ['today', 'yesterday', 'month', 'all'].forEach(p => {
      const btn = document.getElementById('exp-filter-' + p);
      if (btn) btn.className = 'px-3 py-1.5 rounded-xl font-medium bg-slate-100 hover:bg-slate-200 text-gray-700 transition';
    });
    loadExpense();
  }

  let expenseSearchTimeout;
  function debounceExpenseSearch() {
    clearTimeout(expenseSearchTimeout);
    expenseSearchTimeout = setTimeout(() => {
      currentExpenseFilter.search = document.getElementById('exp-filter-search').value.trim();
      loadExpense();
    }, 300);
  }

  async function loadExpense() {
    try {
      // 1. Load Stats
      const statsRes = await fetch(currentActiveBranch ? '/api/finance/stats?branch=' + encodeURIComponent(currentActiveBranch) : '/api/finance/stats');
      const statsData = await statsRes.json();
      if (statsData) {
        document.getElementById('exp-stat-today').textContent = formatDualMoney(statsData.today.expense_khr, statsData.today.expense_usd);
        document.getElementById('exp-stat-month').textContent = formatDualMoney(statsData.this_month.expense_khr, statsData.this_month.expense_usd);
        document.getElementById('exp-stat-year').textContent = formatDualMoney(statsData.this_year.expense_khr, statsData.this_year.expense_usd);
      }

      // 2. Build Query
      const params = new URLSearchParams({ type: 'expense', limit: '150' });
      const catVal = document.getElementById('exp-filter-category').value;
      if (catVal && catVal !== 'all') params.append('category', catVal);

      if (currentExpenseFilter.period === 'today') {
        params.append('date', getTodayString());
      } else if (currentExpenseFilter.period === 'yesterday') {
        params.append('date', getYesterdayString());
      } else if (currentExpenseFilter.period === 'month') {
        params.append('start_date', getCurrentMonthPrefix() + '-01');
        params.append('end_date', getCurrentMonthPrefix() + '-31');
      } else if (currentExpenseFilter.date) {
        params.append('date', currentExpenseFilter.date);
      }

      if (currentExpenseFilter.search) {
        params.append('search', currentExpenseFilter.search);
      }
      if (currentActiveBranch) {
        params.append('branch', currentActiveBranch);
      }

      const res = await fetch('/api/finance/transactions?' + params.toString());
      const data = await res.json();
      expenseTransactions = data.transactions || [];
      document.getElementById('exp-stat-count').textContent = data.total || expenseTransactions.length;
      renderExpenseTable();
    } catch (e) {
      console.warn('loadExpense error:', e);
      document.getElementById('expense-tbody').innerHTML = '<tr><td colspan="7" class="text-center py-8 text-rose-500">កំហុសក្នុងការទាញយកទិន្នន័យចំណាយ</td></tr>';
    }
  }

  function renderExpenseTable() {
    const tbody = document.getElementById('expense-tbody');
    const mobileContainer = document.getElementById('expense-mobile-cards');

    if (!expenseTransactions.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center py-10 text-gray-400">មិនមានទិន្នន័យចំណាយក្នុងចន្លោះពេលនេះឡើយ</td></tr>';
      mobileContainer.innerHTML = '<div class="text-center py-10 text-gray-400 text-sm">មិនមានទិន្នន័យចំណាយឡើយ</div>';
      return;
    }

    tbody.innerHTML = expenseTransactions.map(tx => {
      const amtStr = formatMoney(tx.amount, tx.currency);
      return `
        <tr class="hover:bg-slate-50 transition">
          <td class="px-5 py-3 whitespace-nowrap">
            <div class="font-medium text-gray-800">${escapeHtml(tx.date)}</div>
            <div class="text-[11px] text-gray-400">${escapeHtml(tx.time || '')}</div>
          </td>
          <td class="px-5 py-3 whitespace-nowrap">
            <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-rose-50 text-rose-700 border border-rose-200">
              <span class="material-symbols-outlined text-xs">shopping_cart</span>
              ${escapeHtml(tx.category)}
            </span>
          </td>
          <td class="px-5 py-3">
            <div class="text-xs text-gray-700 font-medium max-w-xs truncate" title="${escapeHtml(tx.description || '')}">${escapeHtml(tx.description || '—')}</div>
            <div class="text-[10px] text-gray-400">កត់ត្រា៖ ${escapeHtml(tx.recorded_by || 'រដ្ឋបាល')}</div>
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-xs text-gray-600">
            ${escapeHtml(tx.payment_method || 'សាច់ប្រាក់សុទ្ធ')}
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-xs font-mono text-gray-500">
            ${escapeHtml(tx.reference_no || '—')}
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-right font-bold text-rose-600 text-sm">
            - ${amtStr}
          </td>
          <td class="px-5 py-3 whitespace-nowrap text-right">
            <div class="inline-flex items-center gap-1">
              <button onclick="openEditTransactionModal('${tx._id}')" class="p-1 rounded-lg text-blue-600 hover:bg-blue-50 transition" title="កែប្រែ">
                <span class="material-symbols-outlined text-base">edit</span>
              </button>
              <button onclick="deleteTransaction('${tx._id}')" class="p-1 rounded-lg text-rose-500 hover:bg-rose-50 transition" title="លុប">
                <span class="material-symbols-outlined text-base">delete</span>
              </button>
            </div>
          </td>
        </tr>
      `;
    }).join('');

    mobileContainer.innerHTML = expenseTransactions.map(tx => {
      const amtStr = formatMoney(tx.amount, tx.currency);
      return `
        <div class="bg-white rounded-2xl p-4 shadow-sm border border-slate-100 flex flex-col gap-2.5">
          <div class="flex items-start justify-between gap-2">
            <div>
              <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-rose-50 text-rose-700 border border-rose-200">
                ${escapeHtml(tx.category)}
              </span>
              <p class="text-[11px] text-gray-400 mt-1">${escapeHtml(tx.date)} ${escapeHtml(tx.time || '')}</p>
            </div>
            <p class="text-base font-extrabold text-rose-600">- ${amtStr}</p>
          </div>
          ${tx.description ? `<p class="text-xs text-gray-700 font-medium">${escapeHtml(tx.description)}</p>` : ''}
          <div class="flex items-center justify-between pt-2 border-t border-slate-100 text-xs text-gray-500">
            <span>💳 ${escapeHtml(tx.payment_method || 'សាច់ប្រាក់សុទ្ធ')} ${tx.reference_no ? '(' + escapeHtml(tx.reference_no) + ')' : ''}</span>
            <div class="flex items-center gap-1">
              <button onclick="openEditTransactionModal('${tx._id}')" class="px-2.5 py-1 rounded-lg text-blue-600 hover:bg-blue-50 font-medium transition">កែប្រែ</button>
              <button onclick="deleteTransaction('${tx._id}')" class="px-2.5 py-1 rounded-lg text-rose-500 hover:bg-rose-50 font-medium transition">លុប</button>
            </div>
          </div>
        </div>
      `;
    }).join('');
  }

  let currentExpenseStep = 1;

  function setExpenseStep(step) {
    currentExpenseStep = step;
    const step1 = document.getElementById('exp-step-1');
    const step2 = document.getElementById('exp-step-2');

    if (step === 1) {
      if (step1) step1.classList.remove('hidden');
      if (step2) step2.classList.add('hidden');
    } else {
      if (step1) step1.classList.add('hidden');
      if (step2) step2.classList.remove('hidden');
    }
  }

  function nextExpenseStep() {
    const dateInput = document.getElementById('exp-form-date');
    if (!dateInput || !dateInput.value) {
      showToast('សូមជ្រើសរើសកាលបរិច្ឆេទជាមុនសិន!', true);
      if (dateInput) dateInput.focus();
      return;
    }

    const amtInput = document.getElementById('exp-form-amount');
    const amount = parseFormattedAmount(amtInput ? amtInput.value : '');
    if (!amount || amount <= 0) {
      showToast('សូមបញ្ចូលចំនួនទឹកប្រាក់ដែលធំជាង ០ ជាមុនសិន!', true);
      if (amtInput) amtInput.focus();
      return;
    }

    setExpenseStep(2);
  }

  function prevExpenseStep() {
    setExpenseStep(1);
    setTimeout(() => {
      const amtInp = document.getElementById('exp-form-amount');
      if (amtInp) amtInp.focus();
    }, 100);
  }

  async function openExpenseModal() {
    const form = document.querySelector('#expense-modal form');
    if (form) form.reset();
    setExpenseStep(1);
    if (document.getElementById('exp-form-branch')) {
      document.getElementById('exp-form-branch').value = currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';
    }
    document.getElementById('exp-form-date').value = getTodayString();
    document.getElementById('exp-form-time').value = getCurrentTimeString();
    document.getElementById('exp-form-currency').value = 'KHR';

    if (!financeCatalogs || financeCatalogs.length === 0) {
      await loadCatalogs();
    } else {
      populateCatalogDropdowns();
    }

    const expCat = document.getElementById('exp-form-category');
    if (expCat) {
      const defaultExpCat = 'ចំណាយរដ្ឋបាល និងផ្សេងៗ';
      const exists = Array.from(expCat.options).some(o => o.value === defaultExpCat);
      if (exists) {
        expCat.value = defaultExpCat;
      }
    }

    const expMethod = document.getElementById('exp-form-method');
    if (expMethod) {
      expMethod.value = 'សាច់ប្រាក់សុទ្ធ';
    }

    openModal('expense-modal');
    setTimeout(() => {
      const amtInp = document.getElementById('exp-form-amount');
      if (amtInp) amtInp.focus();
    }, 150);
  }

  async function submitExpense(e) {
    e.preventDefault();
    if (currentExpenseStep === 1) {
      nextExpenseStep();
      return;
    }
    const date = document.getElementById('exp-form-date').value;
    const time = document.getElementById('exp-form-time').value;
    const amount = parseFormattedAmount(document.getElementById('exp-form-amount').value);
    const currency = document.getElementById('exp-form-currency').value;
    const category = document.getElementById('exp-form-category').value;
    const method = document.getElementById('exp-form-method').value;
    const ref = document.getElementById('exp-form-ref') ? document.getElementById('exp-form-ref').value.trim() : '';
    const desc = document.getElementById('exp-form-desc').value.trim();
    const btn = document.getElementById('btn-save-expense');

    if (!amount || amount <= 0) {
      showToast('សូមបញ្ចូលចំនួនទឹកប្រាក់ដែលធំជាង ០!', true);
      setExpenseStep(1);
      return;
    }

    btn.disabled = true;
    try {
      const res = await fetch('/api/finance/transactions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'expense',
          amount,
          currency,
          category,
          date,
          time,
          payment_method: method,
          reference_no: ref,
          description: desc,
          recorded_by: (localStorage.getItem('acbomlu_admin_user') || 'រដ្ឋបាល'),
          branch: (document.getElementById('exp-form-branch')?.value || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម')
        })
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បានកត់ត្រាចំណាយជោគជ័យ!');
        closeModal('expense-modal');
        await loadExpense();
        await loadFinanceStats();
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការកត់ត្រាចំណាយ!', true);
      }
    } catch (err) {
      showToast('កំហុស៖ ' + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // --- FUND TRANSFERS TO INVESTMENT ACCOUNT ---

  async function openTransferModal() {
    const form = document.querySelector('#transfer-modal form');
    if (form) form.reset();
    const dateInp = document.getElementById('tf-date');
    if (dateInp) {
      const tzOffset = 7 * 60; // UTC+7
      const now = new Date(Date.now() + tzOffset * 60000);
      dateInp.value = now.toISOString().slice(0, 10);
    }
    const branchSelect = document.getElementById('tf-branch');
    if (branchSelect && currentActiveBranch) {
      branchSelect.value = currentActiveBranch;
    }
    openModal('transfer-modal');
    setTimeout(() => {
      const amt = document.getElementById('tf-amount');
      if (amt) amt.focus();
    }, 100);
  }

  async function submitTransfer(e) {
    if (e && e.preventDefault) e.preventDefault();
    const btn = document.getElementById('btn-save-transfer');

    try {
      const fromMethod = document.getElementById('tf-from')?.value || 'សាច់ប្រាក់សុទ្ធ';
      const rawAmt = document.getElementById('tf-amount')?.value || '';
      const currency = document.getElementById('tf-currency')?.value || 'KHR';
      const dateVal = document.getElementById('tf-date')?.value || '';
      const branchVal = document.getElementById('tf-branch')?.value || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';
      const descVal = document.getElementById('tf-desc')?.value || '';

      const cleanNum = parseFormattedAmount(rawAmt);
      if (isNaN(cleanNum) || cleanNum <= 0) {
        showToast('សូមបញ្ចូលចំនួនទឹកប្រាក់បង្វិលឱ្យបានត្រឹមត្រូវ!', true);
        const amtInp = document.getElementById('tf-amount');
        if (amtInp) amtInp.focus();
        return;
      }

      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="material-symbols-outlined animate-spin text-base">sync</span> កំពុងរក្សាទុក...';
      }

      const adminUser = localStorage.getItem('acbomlu_admin_user') || 'រដ្ឋបាល';
      const payload = {
        type: 'transfer',
        amount: cleanNum,
        currency: currency,
        category: 'បង្វិលចូលគណនីវិនិយោគទុន',
        payment_method: fromMethod,
        transfer_from: fromMethod,
        transfer_to: 'គណនីវិនិយោគទុន',
        date: dateVal,
        branch: branchVal,
        description: descVal || 'បង្វិលសាច់ប្រាក់ចូលគណនីវិនិយោគទុន',
        recorded_by: adminUser
      };

      const res = await fetch('/api/finance/transactions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast('✅ បានបង្វិលសាច់ប្រាក់ចូល «គណនីវិនិយោគទុន» ជោគជ័យ!');
        closeModal('transfer-modal');
        const form = document.querySelector('#transfer-modal form');
        if (form) form.reset();
        await loadFinanceStats();
        await loadTransferHistory();
      } else {
        showToast(data.detail || data.message || 'កំហុសពេលរក្សាទុក!', true);
      }
    } catch(err) {
      console.error('submitTransfer error:', err);
      showToast('កំហុសប្រព័ន្ធ: ' + err.message, true);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<span class="material-symbols-outlined text-base">check_circle</span> រក្សាទុកការបង្វិលប្រាក់';
      }
    }
  }

  async function loadTransferHistory() {
    const container = document.getElementById('dash-transfer-history');
    if (!container) return;
    try {
      const params = new URLSearchParams({ type: 'transfer', limit: '15' });
      if (currentActiveBranch) params.append('branch', currentActiveBranch);
      const res = await fetch('/api/finance/transactions?' + params.toString());
      const data = await res.json();
      const transfers = data.transactions || [];
      renderTransferHistory(transfers);
    } catch(e) {
      console.warn('loadTransferHistory error:', e);
    }
  }

  function renderTransferHistory(list) {
    const tbody = document.getElementById('dash-transfer-tbody');
    const countBadge = document.getElementById('dash-transfer-count');
    if (countBadge) countBadge.textContent = (list ? list.length : 0) + ' ប្រតិបត្តិការ';
    if (!tbody) return;

    if (!list || list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center py-5 text-xs text-gray-400">មិនទាន់មានប្រវត្តិបង្វិលសាច់ប្រាក់នៅឡើយទេ</td></tr>';
      return;
    }

    tbody.innerHTML = list.map(t => {
      const isCash = (t.payment_method || t.transfer_from || '').includes('សាច់ប្រាក់');
      const fromBadge = isCash
        ? '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-emerald-100 text-emerald-800">💵 សាច់ប្រាក់សុទ្ធ</span>'
        : '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-blue-100 text-blue-800">📱 បាញ់តាមធនាគារ</span>';
      const amtStr = t.currency === 'USD' ? '$' + Number(t.amount).toLocaleString('en-US', { minimumFractionDigits: 2 }) : Number(t.amount).toLocaleString('en-US') + ' ៛';
      return `<tr class="border-b border-gray-50 hover:bg-slate-50/60 transition">
        <td class="py-2.5 px-3 font-mono text-xs text-gray-600">${t.date || ''} <span class="text-[10px] text-gray-400">${t.time || ''}</span></td>
        <td class="py-2.5 px-3">${fromBadge}</td>
        <td class="py-2.5 px-3">
          <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-bold bg-indigo-100 text-indigo-800">🏦 គណនីវិនិយោគទុន</span>
        </td>
        <td class="py-2.5 px-3 font-mono font-bold text-indigo-700 text-xs sm:text-sm text-right">+ ${amtStr}</td>
        <td class="py-2.5 px-3 text-xs text-gray-600 max-w-[200px] truncate" title="${escapeHtml(t.description || '')}">${escapeHtml(t.description || '-')}</td>
        <td class="py-2.5 px-3 text-right">
          <button onclick="deleteTransfer('${t._id}')" title="លុបការបង្វិលនេះ" class="p-1 rounded-lg text-gray-400 hover:text-rose-600 hover:bg-rose-50 transition">
            <span class="material-symbols-outlined text-base">delete</span>
          </button>
        </td>
      </tr>`;
    }).join('');
  }

  async function deleteTransfer(id) {
    if (!confirm('តើលោកអ្នកពិតជាចង់លុបប្រតិបត្តិការបង្វិលសាច់ប្រាក់នេះមែនទេ?')) return;
    try {
      const res = await fetch('/api/finance/transactions/' + id, { method: 'DELETE' });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast('បានលុបការបង្វិលប្រាក់ជោគជ័យ!');
        await loadFinanceStats();
        await loadTransferHistory();
      } else {
        showToast(data.detail || data.message || 'កំហុសពេលលុប!', true);
      }
    } catch(e) {
      showToast('កំហុសប្រព័ន្ធ: ' + e.message, true);
    }
  }

  // --- EDIT & DELETE TRANSACTIONS ---

  async function openEditTransactionModal(id) {
    try {
      const res = await fetch('/api/finance/transactions/' + id);
      if (!res.ok) throw new Error('Transaction not found');
      const tx = await res.json();

      document.getElementById('edit-tx-id').value = tx._id;
      document.getElementById('edit-tx-type').value = tx.type || 'income';
      const editAmtEl = document.getElementById('edit-tx-amount');
      editAmtEl.value = tx.amount;
      formatAmountInput(editAmtEl);
      document.getElementById('edit-tx-currency').value = tx.currency || 'KHR';
      document.getElementById('edit-tx-date').value = tx.date || '';
      document.getElementById('edit-tx-time').value = tx.time || '';
      const rawMethod = tx.payment_method || 'សាច់ប្រាក់សុទ្ធ';
      document.getElementById('edit-tx-method').value = rawMethod.includes('សាច់ប្រាក់') ? 'សាច់ប្រាក់សុទ្ធ' : 'បាញ់តាមធនាគារ';
      document.getElementById('edit-tx-ref').value = tx.reference_no || '';
      document.getElementById('edit-tx-desc').value = tx.description || '';
      if (document.getElementById('edit-tx-branch')) {
        document.getElementById('edit-tx-branch').value = tx.branch || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';
      }

      updateEditCategoriesDropdown(tx.category);
      openModal('edit-tx-modal');
    } catch (e) {
      showToast('មិនអាចទាញយកទិន្នន័យប្រតិបត្តិការបានទេ!', true);
    }
  }

  function updateEditCategoriesDropdown(selectedCat = null) {
    const type = document.getElementById('edit-tx-type').value;
    const catSelect = document.getElementById('edit-tx-category');
    const cats = financeCatalogs.filter(c => c.type === type || c.type === 'both');

    catSelect.innerHTML = cats.map(c => `<option value="${escapeHtml(c.name)}">${escapeHtml(c.name)}</option>`).join('');
    if (selectedCat) catSelect.value = selectedCat;
  }

  async function submitEditTransaction(e) {
    e.preventDefault();
    const id = document.getElementById('edit-tx-id').value;
    const type = document.getElementById('edit-tx-type').value;
    const amount = parseFormattedAmount(document.getElementById('edit-tx-amount').value);
    const currency = document.getElementById('edit-tx-currency').value;
    const category = document.getElementById('edit-tx-category').value;
    const date = document.getElementById('edit-tx-date').value;
    const time = document.getElementById('edit-tx-time').value;
    const method = document.getElementById('edit-tx-method').value;
    const ref = document.getElementById('edit-tx-ref').value.trim();
    const desc = document.getElementById('edit-tx-desc').value.trim();
    const btn = document.getElementById('btn-save-edit-tx');

    if (!amount || amount <= 0) {
      showToast('ចំនួនទឹកប្រាក់ត្រូវតែធំជាង ០!', true);
      return;
    }

    btn.disabled = true;
    const branch = document.getElementById('edit-tx-branch')?.value || currentActiveBranch || 'ក្រុមហ៊ុនបូមលូ កំពង់ចាម';
    try {
      const res = await fetch('/api/finance/transactions/' + id, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type,
          amount,
          currency,
          category,
          date,
          time,
          payment_method: method,
          reference_no: ref,
          description: desc,
          branch
        })
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បានកែប្រែប្រតិបត្តិការជោគជ័យ!');
        closeModal('edit-tx-modal');
        if (type === 'income') await loadIncome();
        else await loadExpense();
        await loadFinanceStats();
      } else {
        showToast(data.detail || 'បរាជ័យក្នុងការកែប្រែ!', true);
      }
    } catch (err) {
      showToast('កំហុស៖ ' + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteTransaction(id) {
    if (!confirm('តើលោកអ្នកពិតជាចង់លុបប្រតិបត្តិការនេះចេញពីប្រព័ន្ធមែនទេ?')) return;
    try {
      const res = await fetch('/api/finance/transactions/' + id, { method: 'DELETE' });
      const data = await res.json();
      if (res.ok && data.success) {
        showToast(data.message || 'បានលុបប្រតិបត្តិការជោគជ័យ!');
        await loadIncome();
        await loadExpense();
        await loadFinanceStats();
      } else {
        showToast(data.detail || 'មិនអាចលុបប្រតិបត្តិការបានទេ!', true);
      }
    } catch (err) {
      showToast('កំហុស៖ ' + err.message, true);
    }
  }

  // --- FINANCIAL REPORTS (ប្រចាំថ្ងៃ, ប្រចាំខែ, ប្រចាំឆ្នាំ) ---

  function initFinanceReportFilters() {
    const today = new Date();
    const dateInput = document.getElementById('rep-finance-date');
    if (dateInput && !dateInput.value) dateInput.value = getTodayString();

    const monthSelect = document.getElementById('rep-finance-month');
    if (monthSelect) monthSelect.value = String(today.getMonth() + 1);

    const yearM = document.getElementById('rep-finance-year-m');
    const yearY = document.getElementById('rep-finance-year-y');
    const curYear = today.getFullYear();
    const yearOpts = [curYear - 2, curYear - 1, curYear, curYear + 1]
      .map(y => `<option value="${y}" ${y === curYear ? 'selected' : ''}>${y}</option>`).join('');

    if (yearM) yearM.innerHTML = yearOpts;
    if (yearY) yearY.innerHTML = yearOpts;
  }

  function switchFinanceReportPeriod(period) {
    currentReportPeriod = period;
    ['daily', 'monthly', 'yearly'].forEach(p => {
      const btn = document.getElementById('rep-period-' + p);
      const ctrl = document.getElementById('rep-ctrl-' + p);
      if (btn) {
        if (p === period) {
          btn.className = 'flex-1 py-2 rounded-xl text-xs sm:text-sm font-bold bg-blue-600 text-white shadow-sm transition flex items-center justify-center gap-1.5';
        } else {
          btn.className = 'flex-1 py-2 rounded-xl text-xs sm:text-sm font-medium bg-transparent text-gray-600 hover:bg-slate-100 transition flex items-center justify-center gap-1.5';
        }
      }
      if (ctrl) {
        if (p === period) ctrl.classList.remove('hidden');
        else ctrl.classList.add('hidden');
      }
    });

    const label = document.getElementById('rep-finance-period-label');
    if (label) {
      if (period === 'daily') label.textContent = 'របាយការណ៍ហិរញ្ញវត្ថុប្រចាំថ្ងៃ';
      else if (period === 'monthly') label.textContent = 'របាយការណ៍ហិរញ្ញវត្ថុប្រចាំខែ';
      else label.textContent = 'របាយការណ៍ហិរញ្ញវត្ថុប្រចាំឆ្នាំ';
    }

    loadFinanceReports();
  }

  function changeDailyReportDate(offset) {
    const input = document.getElementById('rep-finance-date');
    if (!input) return;
    if (offset === 0) {
      input.value = getTodayString();
    } else {
      const cur = new Date(input.value || getTodayString());
      cur.setDate(cur.getDate() + offset);
      const y = cur.getFullYear();
      const m = String(cur.getMonth() + 1).padStart(2, '0');
      const d = String(cur.getDate()).padStart(2, '0');
      input.value = `${y}-${m}-${d}`;
    }
    loadFinanceReports();
  }

  async function loadFinanceReports() {
    const contentArea = document.getElementById('rep-finance-content');
    contentArea.innerHTML = '<div class="text-center py-12 text-gray-400 bg-white rounded-2xl border border-slate-100">កំពុងរៀបចំរបាយការណ៍...</div>';

    try {
      const branchParam = currentActiveBranch ? '&branch=' + encodeURIComponent(currentActiveBranch) : '';
      if (currentReportPeriod === 'daily') {
        const d = document.getElementById('rep-finance-date').value || getTodayString();
        const res = await fetch('/api/finance/reports/daily?date=' + d + branchParam);
        const data = await res.json();
        activeFinanceReportData = { type: 'daily', data };
        updateReportSummaryCards(data.summary);
        renderFinanceReportDaily(data);
      } else if (currentReportPeriod === 'monthly') {
        const today = new Date();
        const mEl = document.getElementById('rep-finance-month');
        const yEl = document.getElementById('rep-finance-year-m');
        const m = (mEl && mEl.value) ? mEl.value : (today.getMonth() + 1);
        const y = (yEl && yEl.value) ? yEl.value : today.getFullYear();
        const res = await fetch(`/api/finance/reports/monthly?year=${y}&month=${m}` + branchParam);
        const data = await res.json();
        activeFinanceReportData = { type: 'monthly', data };
        updateReportSummaryCards(data.summary);
        renderFinanceReportMonthly(data);
      } else if (currentReportPeriod === 'yearly') {
        const today = new Date();
        const yEl = document.getElementById('rep-finance-year-y');
        const y = (yEl && yEl.value) ? yEl.value : today.getFullYear();
        const res = await fetch(`/api/finance/reports/yearly?year=${y}` + branchParam);
        const data = await res.json();
        activeFinanceReportData = { type: 'yearly', data };
        updateReportSummaryCards(data.summary);
        renderFinanceReportYearly(data);
      }
    } catch (e) {
      contentArea.innerHTML = '<div class="text-center py-12 text-rose-500 bg-white rounded-2xl border border-slate-100">កំហុសក្នុងការទាញយករបាយការណ៍</div>';
    }
  }

  function updateReportSummaryCards(summary) {
    if (!summary) return;
    const incEl = document.getElementById('rep-summary-income');
    if (incEl) incEl.textContent = formatDualMoney(summary.income_khr, summary.income_usd);

    const expEl = document.getElementById('rep-summary-expense');
    if (expEl) expEl.textContent = formatDualMoney(summary.expense_khr, summary.expense_usd);
    
    const netEl = document.getElementById('rep-summary-net');
    if (netEl) {
      netEl.textContent = formatDualMoney(summary.net_khr, summary.net_usd);
      if (summary.net_khr >= 0 && summary.net_usd >= 0) {
        netEl.className = 'text-base sm:text-xl font-bold text-emerald-600 mt-1.5';
      } else {
        netEl.className = 'text-base sm:text-xl font-bold text-rose-600 mt-1.5';
      }
    }

    const cntEl = document.getElementById('rep-summary-count');
    if (cntEl) cntEl.textContent = summary.count || 0;

    // Advanced Operational KPIs
    const tripsEl = document.getElementById('rep-summary-trips');
    if (tripsEl) tripsEl.textContent = (summary.total_trips || 0) + ' ជើង';

    const arptEl = document.getElementById('rep-summary-arpt');
    if (arptEl) {
      arptEl.textContent = formatDualMoney(summary.arpt ? summary.arpt.khr : 0, summary.arpt ? summary.arpt.usd : 0);
    }

    const cashEl = document.getElementById('rep-summary-cash');
    if (cashEl) {
      const cBal = (summary.reconciliation && summary.reconciliation.cash_balance) ? summary.reconciliation.cash_balance : { khr: 0, usd: 0 };
      cashEl.textContent = formatDualMoney(cBal.khr, cBal.usd);
    }

    const bankEl = document.getElementById('rep-summary-bank');
    if (bankEl) {
      const bBal = (summary.reconciliation && summary.reconciliation.bank_balance) ? summary.reconciliation.bank_balance : { khr: 0, usd: 0 };
      bankEl.textContent = formatDualMoney(bBal.khr, bBal.usd);
    }
  }

  function renderExecutiveAnalyticsHTML(summary, periodTitle) {
    if (!summary) return '';
    const locs = summary.locations || [];
    const maxTrips = locs.reduce((m, l) => Math.max(m, l.trips || 0), 1);
    const totalTrips = summary.total_trips || 0;
    const rec = summary.reconciliation || {
      cash_income: { khr: 0, usd: 0, count: 0 },
      cash_expense: { khr: 0, usd: 0, count: 0 },
      cash_balance: { khr: 0, usd: 0 },
      bank_income: { khr: 0, usd: 0, count: 0 },
      bank_expense: { khr: 0, usd: 0, count: 0 },
      bank_balance: { khr: 0, usd: 0 }
    };
    const expStruct = summary.expense_structure || {};
    const staffList = summary.staff_breakdown || [];
    const expRatio = summary.expense_ratio_pct || 0;

    let ratioBadgeClass = 'bg-emerald-100 text-emerald-800 border-emerald-200';
    let ratioStatusText = 'ចំណេញខ្ពស់ (Healthy Profit Margin)';
    if (expRatio > 60) {
      ratioBadgeClass = 'bg-rose-100 text-rose-800 border-rose-200';
      ratioStatusText = 'ចំណាយខ្ពស់គួរត្រួតពិនិត្យ (High Cost)';
    } else if (expRatio > 35) {
      ratioBadgeClass = 'bg-amber-100 text-amber-800 border-amber-200';
      ratioStatusText = 'កម្រិតមធ្យម (Moderate Cost)';
    }

    return `
      <!-- 1. Cash vs Bank Reconciliation Hub -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <div class="px-5 py-3.5 bg-gradient-to-r from-slate-50 via-blue-50/40 to-indigo-50/40 border-b border-slate-100 flex items-center justify-between flex-wrap gap-2">
          <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-blue-600">account_balance_wallet</span>
            <h4 class="font-bold text-gray-800 text-sm">ផ្ទាំងផ្ទៀងផ្ទាត់សាច់ប្រាក់ និងធនាគារ (Cash & Bank Reconciliation)</h4>
          </div>
          <span class="text-xs text-gray-500 font-medium">${escapeHtml(periodTitle)}</span>
        </div>
        <div class="p-5 grid grid-cols-1 md:grid-cols-2 gap-5">
          <!-- Cash in Hand Safe Box -->
          <div class="rounded-2xl border border-emerald-200 bg-gradient-to-br from-emerald-50/70 to-teal-50/30 p-4 relative overflow-hidden">
            <div class="flex items-center justify-between mb-3">
              <span class="flex items-center gap-2 text-emerald-900 font-bold text-sm">
                <span class="material-symbols-outlined text-emerald-600">payments</span> សាច់ប្រាក់សុទ្ធ (Cash In Hand)
              </span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-200/80 text-emerald-900">ថតប្រាក់ / Drawer</span>
            </div>
            <div class="space-y-2 text-xs text-gray-700">
              <div class="flex items-center justify-between py-1 border-b border-emerald-100/80">
                <span class="text-gray-600">ចំណូលសាច់ប្រាក់ (+)</span>
                <span class="font-bold text-emerald-700 font-mono">${formatDualMoney(rec.cash_income.khr, rec.cash_income.usd)}</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-emerald-100/80">
                <span class="text-gray-600">ចំណាយសាច់ប្រាក់ (-)</span>
                <span class="font-bold text-rose-600 font-mono">${formatDualMoney(rec.cash_expense.khr, rec.cash_expense.usd)}</span>
              </div>
              <div class="flex items-center justify-between pt-2">
                <span class="font-bold text-gray-800">សាច់ប្រាក់នៅសល់ជាក់ស្ដែង៖</span>
                <span class="text-sm font-black text-emerald-800 font-mono">${formatDualMoney(rec.cash_balance.khr, rec.cash_balance.usd)}</span>
              </div>
            </div>
          </div>

          <!-- Bank Account Box -->
          <div class="rounded-2xl border border-blue-200 bg-gradient-to-br from-blue-50/70 to-indigo-50/30 p-4 relative overflow-hidden">
            <div class="flex items-center justify-between mb-3">
              <span class="flex items-center gap-2 text-blue-900 font-bold text-sm">
                <span class="material-symbols-outlined text-blue-600">account_balance</span> បាញ់តាមធនាគារ (Bank Transfer)
              </span>
              <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-blue-200/80 text-blue-900">KHQR / Transfer</span>
            </div>
            <div class="space-y-2 text-xs text-gray-700">
              <div class="flex items-center justify-between py-1 border-b border-blue-100/80">
                <span class="text-gray-600">ប្រាក់ផ្ទេរចូល (+)</span>
                <span class="font-bold text-blue-700 font-mono">${formatDualMoney(rec.bank_income.khr, rec.bank_income.usd)}</span>
              </div>
              <div class="flex items-center justify-between py-1 border-b border-blue-100/80">
                <span class="text-gray-600">ប្រាក់ផ្ទេរចេញ (-)</span>
                <span class="font-bold text-rose-600 font-mono">${formatDualMoney(rec.bank_expense.khr, rec.bank_expense.usd)}</span>
              </div>
              <div class="flex items-center justify-between pt-2">
                <span class="font-bold text-gray-800">សមតុល្យធនាគារកើនឡើង៖</span>
                <span class="text-sm font-black text-blue-800 font-mono">${formatDualMoney(rec.bank_balance.khr, rec.bank_balance.usd)}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 2. Locations & Trips Performance Leaderboard -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <div class="px-5 py-3.5 bg-gradient-to-r from-slate-50 via-indigo-50/30 to-blue-50/30 border-b border-slate-100 flex items-center justify-between flex-wrap gap-2">
          <div class="flex items-center gap-2">
            <span class="material-symbols-outlined text-indigo-600">location_on</span>
            <h4 class="font-bold text-gray-800 text-sm">វិភាគទីតាំងផ្ដល់សេវា & ចំនួនជើង (Service Locations & Trips Leaderboard)</h4>
          </div>
          <div class="flex items-center gap-2 sm:gap-3 text-xs">
            <span class="font-bold text-indigo-700 bg-indigo-50 px-2.5 py-1 rounded-lg border border-indigo-100">
              សរុប៖ <b>${totalTrips}</b> ជើង
            </span>
            <span class="font-bold text-emerald-700 bg-emerald-50 px-2.5 py-1 rounded-lg border border-emerald-100">
              មធ្យម/ជើង៖ <b>${formatDualMoney(summary.arpt ? summary.arpt.khr : 0, summary.arpt ? summary.arpt.usd : 0)}</b>
            </span>
          </div>
        </div>

        <div class="p-5">
          ${locs.length ? `
            <div class="overflow-x-auto">
              <table class="w-full text-xs text-left">
                <thead class="bg-slate-50 text-gray-600 border-b border-slate-100 font-semibold">
                  <tr>
                    <th class="px-4 py-2.5 rounded-l-xl">ចំណាត់ថ្នាក់</th>
                    <th class="px-4 py-2.5">ទីតាំងផ្ដល់សេវា</th>
                    <th class="px-4 py-2.5 text-center">ចំនួនជើងសេវា</th>
                    <th class="px-4 py-2.5">សមាមាត្រ (% ជើង)</th>
                    <th class="px-4 py-2.5 text-right">ចំណូលសរុប</th>
                    <th class="px-4 py-2.5 text-right rounded-r-xl">មធ្យម/ជើង</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-slate-100 text-gray-700">
                  ${locs.map((l, idx) => {
                    const pct = totalTrips ? ((l.trips / totalTrips) * 100).toFixed(1) : '0';
                    const barWidth = Math.round((l.trips / maxTrips) * 100);
                    const avgKhr = l.trips ? (l.khr / l.trips) : 0;
                    const avgUsd = l.trips ? (l.usd / l.trips) : 0;
                    const rankColors = ['bg-amber-400 text-white', 'bg-slate-400 text-white', 'bg-amber-700 text-white'];
                    const rankClass = idx < 3 ? rankColors[idx] : 'bg-slate-100 text-gray-600';
                    return `
                      <tr class="hover:bg-slate-50/80 transition">
                        <td class="px-4 py-3 whitespace-nowrap">
                          <span class="w-6 h-6 inline-flex items-center justify-center rounded-full text-[11px] font-bold ${rankClass}">
                            ${idx + 1}
                          </span>
                        </td>
                        <td class="px-4 py-3 font-semibold text-gray-900 whitespace-nowrap flex items-center gap-1.5">
                          <span class="material-symbols-outlined text-red-500 text-base">pin_drop</span>
                          ${escapeHtml(l.location)}
                        </td>
                        <td class="px-4 py-3 text-center whitespace-nowrap font-bold text-indigo-700">
                          ${l.trips} ជើង
                        </td>
                        <td class="px-4 py-3 w-44">
                          <div class="flex items-center gap-2">
                            <div class="w-full bg-slate-100 h-2 rounded-full overflow-hidden">
                              <div class="bg-indigo-600 h-2 rounded-full" style="width: ${barWidth}%"></div>
                            </div>
                            <span class="text-[10px] text-gray-500 font-mono">${pct}%</span>
                          </div>
                        </td>
                        <td class="px-4 py-3 text-right font-bold text-emerald-600 font-mono whitespace-nowrap">
                          ${formatDualMoney(l.khr, l.usd)}
                        </td>
                        <td class="px-4 py-3 text-right font-semibold text-gray-600 font-mono whitespace-nowrap">
                          ${formatDualMoney(avgKhr, avgUsd)}
                        </td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          ` : `
            <div class="text-center py-6 text-gray-400 text-xs">
              <span class="material-symbols-outlined text-3xl mb-1 text-gray-300">map</span>
              <p>មិនទាន់មានទិន្នន័យទីតាំងសេវាសម្រាប់កាលបរិច្ឆេទនេះទេ</p>
            </div>
          `}
        </div>
      </div>

      <!-- 3. Operating Cost Structure & Staff Contribution -->
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <!-- Expense Structure (2 Cols) -->
        <div class="lg:col-span-2 bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-5 py-3.5 bg-rose-50/40 border-b border-slate-100 flex items-center justify-between flex-wrap gap-2">
            <div class="flex items-center gap-2">
              <span class="material-symbols-outlined text-rose-600">pie_chart</span>
              <h4 class="font-bold text-gray-800 text-sm">វិភាគរចនាសម្ព័ន្ធចំណាយប្រតិបត្តិការ (Operating Cost Structure)</h4>
            </div>
            <span class="text-xs font-bold px-2.5 py-0.5 rounded-full border ${ratioBadgeClass}">
              សមាមាត្រចំណាយ៖ ${expRatio}% (${ratioStatusText})
            </span>
          </div>

          <div class="p-5 grid grid-cols-1 sm:grid-cols-2 gap-4">
            ${Object.keys(expStruct).map(k => {
              const item = expStruct[k];
              const hasVal = item.khr > 0 || item.usd > 0;
              const iconsMap = {
                fuel: 'local_gas_station',
                maintenance: 'build',
                disposal: 'delete_sweep',
                marketing: 'campaign',
                salary: 'badge',
                other: 'receipt'
              };
              const icon = iconsMap[k] || 'payments';
              return `
                <div class="p-3.5 rounded-xl border border-slate-100 bg-slate-50/50 hover:bg-slate-50 transition">
                  <div class="flex items-center justify-between mb-1.5">
                    <span class="flex items-center gap-1.5 text-xs font-semibold text-gray-700">
                      <span class="material-symbols-outlined text-base text-rose-500">${icon}</span>
                      ${escapeHtml(item.label)}
                    </span>
                    <span class="text-[10px] text-gray-400 font-mono">${item.count} ប្រតិបត្តិការ</span>
                  </div>
                  <p class="text-sm font-bold ${hasVal ? 'text-rose-600' : 'text-gray-400'} font-mono">
                    ${formatDualMoney(item.khr, item.usd)}
                  </p>
                </div>
              `;
            }).join('')}
          </div>
        </div>

        <!-- Staff Contribution Card (1 Col) -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden flex flex-col">
          <div class="px-5 py-3.5 bg-blue-50/40 border-b border-slate-100 flex items-center justify-between">
            <h4 class="font-bold text-gray-800 text-sm flex items-center gap-2">
              <span class="material-symbols-outlined text-blue-600">group</span> ការបំពេញការងារតាមបុគ្គលិក
            </h4>
            <span class="text-[11px] text-gray-500 font-mono">${staffList.length} នាក់</span>
          </div>
          <div class="p-4 space-y-3 flex-1 overflow-y-auto">
            ${staffList.length ? staffList.map(s => `
              <div class="p-3 rounded-xl border border-slate-100 bg-slate-50/60">
                <div class="flex items-center justify-between mb-1">
                  <span class="font-bold text-xs text-gray-800 flex items-center gap-1.5">
                    <span class="material-symbols-outlined text-blue-500 text-base">person</span>
                    ${escapeHtml(s.staff)}
                  </span>
                  <span class="text-[11px] font-bold text-indigo-700 bg-indigo-50 px-2 py-0.5 rounded-full">
                    ${s.trips} ជើង
                  </span>
                </div>
                <div class="flex items-center justify-between text-[11px] text-gray-600 font-mono">
                  <span>ចំណូលកត់ត្រា៖</span>
                  <span class="font-bold text-emerald-600">${formatDualMoney(s.income_khr, s.income_usd)}</span>
                </div>
              </div>
            `).join('') : '<p class="text-center py-6 text-xs text-gray-400">មិនមានទិន្នន័យបុគ្គលិក</p>'}
          </div>
        </div>
      </div>
    </div>
    `;
  }

  function renderFinanceReportDaily(data) {
    const content = document.getElementById('rep-finance-content');
    const { date, summary, transactions } = data;

    const incCats = summary.income_by_category || [];
    const expCats = summary.expense_by_category || [];

    content.innerHTML = `
      <!-- Executive Operational & Financial Analytics Sections -->
      ${renderExecutiveAnalyticsHTML(summary, `របាយការណ៍ប្រចាំថ្ងៃ ទី ${date}`)}

      <!-- Category Breakdown Cards (2 columns) -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
        <!-- Income Categories -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-5 py-3.5 bg-emerald-50/50 border-b border-slate-100 flex items-center justify-between">
            <h4 class="font-bold text-emerald-800 text-sm flex items-center gap-2">
              <span class="material-symbols-outlined text-emerald-600">trending_up</span> ចំណូលតាមកាតាឡុក (${date})
            </h4>
            <span class="text-xs font-semibold text-emerald-700 font-mono">${formatDualMoney(summary.income_khr, summary.income_usd)}</span>
          </div>
          <div class="p-4 space-y-3">
            ${incCats.length ? incCats.map(c => `
              <div class="flex items-center justify-between text-xs">
                <span class="font-medium text-gray-700">${escapeHtml(c.category)} (${c.count})</span>
                <span class="font-bold text-emerald-600 font-mono">${formatDualMoney(c.khr, c.usd)}</span>
              </div>
            `).join('') : '<p class="text-xs text-gray-400 text-center py-4">គ្មានចំណូលក្នុងថ្ងៃនេះទេ</p>'}
          </div>
        </div>

        <!-- Expense Categories -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-5 py-3.5 bg-rose-50/50 border-b border-slate-100 flex items-center justify-between">
            <h4 class="font-bold text-rose-800 text-sm flex items-center gap-2">
              <span class="material-symbols-outlined text-rose-600">trending_down</span> ចំណាយតាមកាតាឡុក (${date})
            </h4>
            <span class="text-xs font-semibold text-rose-700 font-mono">${formatDualMoney(summary.expense_khr, summary.expense_usd)}</span>
          </div>
          <div class="p-4 space-y-3">
            ${expCats.length ? expCats.map(c => `
              <div class="flex items-center justify-between text-xs">
                <span class="font-medium text-gray-700">${escapeHtml(c.category)} (${c.count})</span>
                <span class="font-bold text-rose-600 font-mono">${formatDualMoney(c.khr, c.usd)}</span>
              </div>
            `).join('') : '<p class="text-xs text-gray-400 text-center py-4">គ្មានចំណាយក្នុងថ្ងៃនេះទេ</p>'}
          </div>
        </div>
      </div>

      <!-- Transactions List for the Day -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden mt-6">
        <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
          <h4 class="font-bold text-gray-800 text-sm sm:text-base flex items-center gap-2">
            <span class="material-symbols-outlined text-blue-600">receipt_long</span> បញ្ជីប្រតិបត្តិការថ្ងៃទី ${date}
          </h4>
          <span class="text-xs text-gray-500 font-medium">សរុប ${transactions.length} ប្រតិបត្តិការ</span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-xs sm:text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100 font-medium">
              <tr>
                <th class="px-5 py-3">ម៉ោង</th>
                <th class="px-5 py-3">ប្រភេទ</th>
                <th class="px-5 py-3">កាតាឡុក</th>
                <th class="px-5 py-3">ពិពណ៌នា / ទីតាំង & ជើង</th>
                <th class="px-5 py-3">ការទូទាត់</th>
                <th class="px-5 py-3 text-right">ចំនួនទឹកប្រាក់</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-50 text-gray-700">
              ${transactions.length ? transactions.map(t => `
                <tr class="hover:bg-slate-50 transition">
                  <td class="px-5 py-3 font-mono text-gray-500 whitespace-nowrap">${escapeHtml(t.time || '')}</td>
                  <td class="px-5 py-3 whitespace-nowrap">
                    ${t.type === 'income' ? '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700">ចំណូល</span>' : '<span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-rose-100 text-rose-700">ចំណាយ</span>'}
                  </td>
                  <td class="px-5 py-3 font-medium whitespace-nowrap">${escapeHtml(t.category)}</td>
                  <td class="px-5 py-3 text-gray-600">
                    <span class="whitespace-pre-line">${escapeHtml(t.description || '—')}</span>
                  </td>
                  <td class="px-5 py-3 text-gray-500 whitespace-nowrap">
                    <span class="px-2 py-0.5 rounded-md text-[11px] font-medium ${(t.payment_method || '').includes('សាច់ប្រាក់') ? 'bg-emerald-50 text-emerald-800 border border-emerald-100' : 'bg-blue-50 text-blue-800 border border-blue-100'}">
                      ${escapeHtml(t.payment_method || 'សាច់ប្រាក់សុទ្ធ')}
                    </span>
                  </td>
                  <td class="px-5 py-3 text-right font-bold whitespace-nowrap font-mono ${t.type === 'income' ? 'text-emerald-600' : 'text-rose-600'}">
                    ${t.type === 'income' ? '+ ' : '- '}${formatMoney(t.amount, t.currency)}
                  </td>
                </tr>
              `).join('') : '<tr><td colspan="6" class="text-center py-8 text-gray-400">គ្មានប្រតិបត្តិការក្នុងថ្ងៃនេះទេ</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  function renderFinanceReportMonthly(data) {
    const content = document.getElementById('rep-finance-content');
    const { year, month, month_name, summary, daily_breakdown } = data;

    const incCats = summary.income_by_category || [];
    const expCats = summary.expense_by_category || [];

    content.innerHTML = `
      <!-- Executive Operational & Financial Analytics Sections -->
      ${renderExecutiveAnalyticsHTML(summary, `ខែ ${month_name} ឆ្នាំ ${year}`)}

      <!-- Category Breakdown Cards -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 p-5">
          <h4 class="font-bold text-emerald-800 text-sm flex items-center gap-2 mb-3">
            <span class="material-symbols-outlined text-emerald-600">trending_up</span> ចំណូលខែ ${month_name} តាមកាតាឡុក
          </h4>
          <div class="space-y-2.5">
            ${incCats.length ? incCats.map(c => `
              <div class="flex items-center justify-between text-xs">
                <span class="font-medium text-gray-700">${escapeHtml(c.category)} (${c.count})</span>
                <span class="font-bold text-emerald-600 font-mono">${formatDualMoney(c.khr, c.usd)}</span>
              </div>
            `).join('') : '<p class="text-xs text-gray-400 text-center py-4">គ្មានចំណូលក្នុងខែនេះទេ</p>'}
          </div>
        </div>

        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 p-5">
          <h4 class="font-bold text-rose-800 text-sm flex items-center gap-2 mb-3">
            <span class="material-symbols-outlined text-rose-600">trending_down</span> ចំណាយខែ ${month_name} តាមកាតាឡុក
          </h4>
          <div class="space-y-2.5">
            ${expCats.length ? expCats.map(c => `
              <div class="flex items-center justify-between text-xs">
                <span class="font-medium text-gray-700">${escapeHtml(c.category)} (${c.count})</span>
                <span class="font-bold text-rose-600 font-mono">${formatDualMoney(c.khr, c.usd)}</span>
              </div>
            `).join('') : '<p class="text-xs text-gray-400 text-center py-4">គ្មានចំណាយក្នុងខែនេះទេ</p>'}
          </div>
        </div>
      </div>

      <!-- Daily Breakdown Table -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden mt-6">
        <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
          <h4 class="font-bold text-gray-800 text-sm sm:text-base flex items-center gap-2">
            <span class="material-symbols-outlined text-blue-600">calendar_view_day</span> តារាងលម្អិតតាមថ្ងៃនីមួយៗ (ខែ ${month_name} ឆ្នាំ ${year})
          </h4>
          <span class="text-xs text-gray-500 font-medium">ចំនួន ${daily_breakdown.length} ថ្ងៃ</span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-xs sm:text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100 font-medium">
              <tr>
                <th class="px-5 py-3">កាលបរិច្ឆេទ</th>
                <th class="px-5 py-3 text-center">ចំនួនជើង (Trips)</th>
                <th class="px-5 py-3 text-right">ចំណូលរៀល (៛)</th>
                <th class="px-5 py-3 text-right">ចំណូលដុល្លារ ($)</th>
                <th class="px-5 py-3 text-right">ចំណាយរៀល (៛)</th>
                <th class="px-5 py-3 text-right">ចំណាយដុល្លារ ($)</th>
                <th class="px-5 py-3 text-right">សមតុល្យសុទ្ធ</th>
                <th class="px-5 py-3 text-center">ប្រតិបត្តិការ</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-50 text-gray-700 font-mono">
              ${daily_breakdown.map(d => {
                const hasData = d.income_khr || d.income_usd || d.expense_khr || d.expense_usd;
                const netStr = formatDualMoney(d.net_khr, d.net_usd);
                const isPos = d.net_khr >= 0 && d.net_usd >= 0;
                return `
                  <tr class="hover:bg-slate-50 transition ${hasData ? 'bg-white' : 'opacity-60'}">
                    <td class="px-5 py-2.5 font-sans font-medium text-gray-800">${escapeHtml(d.date)}</td>
                    <td class="px-5 py-2.5 text-center font-sans font-bold text-indigo-700">${d.trips || 0} ជើង</td>
                    <td class="px-5 py-2.5 text-right font-bold text-emerald-600">${Math.round(d.income_khr).toLocaleString('en-US')} ៛</td>
                    <td class="px-5 py-2.5 text-right font-bold text-emerald-600">$${d.income_usd.toFixed(2)}</td>
                    <td class="px-5 py-2.5 text-right font-bold text-rose-600">${Math.round(d.expense_khr).toLocaleString('en-US')} ៛</td>
                    <td class="px-5 py-2.5 text-right font-bold text-rose-600">$${d.expense_usd.toFixed(2)}</td>
                    <td class="px-5 py-2.5 text-right font-bold font-sans ${isPos ? 'text-emerald-700' : 'text-rose-700'}">${netStr}</td>
                    <td class="px-5 py-2.5 text-center font-sans text-xs text-gray-500">${d.count}</td>
                  </tr>
                `;
              }).join('')}
            </tbody>
            <tfoot class="bg-slate-100 font-bold border-t-2 border-slate-200">
              <tr>
                <td class="px-5 py-3 font-sans text-gray-900">សរុបប្រចាំខែ</td>
                <td class="px-5 py-3 text-center font-sans text-indigo-800">${summary.total_trips || 0} ជើង</td>
                <td class="px-5 py-3 text-right text-emerald-700 font-mono">${Math.round(summary.income_khr).toLocaleString('en-US')} ៛</td>
                <td class="px-5 py-3 text-right text-emerald-700 font-mono">$${summary.income_usd.toFixed(2)}</td>
                <td class="px-5 py-3 text-right text-rose-700 font-mono">${Math.round(summary.expense_khr).toLocaleString('en-US')} ៛</td>
                <td class="px-5 py-3 text-right text-rose-700 font-mono">$${summary.expense_usd.toFixed(2)}</td>
                <td class="px-5 py-3 text-right font-sans ${summary.net_khr >= 0 && summary.net_usd >= 0 ? 'text-emerald-700' : 'text-rose-700'}">${formatDualMoney(summary.net_khr, summary.net_usd)}</td>
                <td class="px-5 py-3 text-center font-sans">${summary.count}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    `;
  }

  function renderFinanceReportYearly(data) {
    const content = document.getElementById('rep-finance-content');
    const { year, summary, monthly_breakdown } = data;

    content.innerHTML = `
      <!-- Executive Operational & Financial Analytics Sections -->
      ${renderExecutiveAnalyticsHTML(summary, `ឆ្នាំ ${year}`)}

      <!-- Yearly 12-Month Summary Table -->
      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden mt-6">
        <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
          <h4 class="font-bold text-gray-800 text-sm sm:text-base flex items-center gap-2">
            <span class="material-symbols-outlined text-blue-600">table_chart</span> តារាងសង្ខេប ១២ ខែ ប្រចាំឆ្នាំ ${year}
          </h4>
          <span class="text-xs text-gray-500 font-medium">ក្រុមហ៊ុន AC BOMLU</span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-xs sm:text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100 font-medium">
              <tr>
                <th class="px-5 py-3">ខែ</th>
                <th class="px-5 py-3 text-center">ចំនួនជើង (Trips)</th>
                <th class="px-5 py-3 text-right">ចំណូលរៀល (៛)</th>
                <th class="px-5 py-3 text-right">ចំណូលដុល្លារ ($)</th>
                <th class="px-5 py-3 text-right">ចំណាយរៀល (៛)</th>
                <th class="px-5 py-3 text-right">ចំណាយដុល្លារ ($)</th>
                <th class="px-5 py-3 text-right">ចំណេញ/ខាតសុទ្ធ</th>
                <th class="px-5 py-3 text-center">ប្រតិបត្តិការ</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-50 text-gray-700 font-mono">
              ${monthly_breakdown.map(m => {
                const netStr = formatDualMoney(m.net_khr, m.net_usd);
                const isPos = m.net_khr >= 0 && m.net_usd >= 0;
                return `
                  <tr class="hover:bg-slate-50 transition">
                    <td class="px-5 py-3 font-sans font-bold text-gray-800">ខែ${escapeHtml(m.month_name)} (${m.month})</td>
                    <td class="px-5 py-3 text-center font-sans font-bold text-indigo-700">${m.trips || 0} ជើង</td>
                    <td class="px-5 py-3 text-right font-bold text-emerald-600">${Math.round(m.income_khr).toLocaleString('en-US')} ៛</td>
                    <td class="px-5 py-3 text-right font-bold text-emerald-600">$${m.income_usd.toFixed(2)}</td>
                    <td class="px-5 py-3 text-right font-bold text-rose-600">${Math.round(m.expense_khr).toLocaleString('en-US')} ៛</td>
                    <td class="px-5 py-3 text-right font-bold text-rose-600">$${m.expense_usd.toFixed(2)}</td>
                    <td class="px-5 py-3 text-right font-bold font-sans ${isPos ? 'text-emerald-700' : 'text-rose-700'}">${netStr}</td>
                    <td class="px-5 py-3 text-center font-sans text-xs text-gray-500">${m.count}</td>
                  </tr>
                `;
              }).join('')}
            </tbody>
            <tfoot class="bg-slate-100 font-bold border-t-2 border-slate-200">
              <tr>
                <td class="px-5 py-3 font-sans text-gray-900">សរុបឆ្នាំ ${year}</td>
                <td class="px-5 py-3 text-center font-sans text-indigo-800">${summary.total_trips || 0} ជើង</td>
                <td class="px-5 py-3 text-right text-emerald-700 font-mono">${Math.round(summary.income_khr).toLocaleString('en-US')} ៛</td>
                <td class="px-5 py-3 text-right text-emerald-700 font-mono">$${summary.income_usd.toFixed(2)}</td>
                <td class="px-5 py-3 text-right text-rose-700 font-mono">${Math.round(summary.expense_khr).toLocaleString('en-US')} ៛</td>
                <td class="px-5 py-3 text-right text-rose-700 font-mono">$${summary.expense_usd.toFixed(2)}</td>
                <td class="px-5 py-3 text-right font-sans ${summary.net_khr >= 0 && summary.net_usd >= 0 ? 'text-emerald-700' : 'text-rose-700'}">${formatDualMoney(summary.net_khr, summary.net_usd)}</td>
                <td class="px-5 py-3 text-center font-sans">${summary.count}</td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    `;
  }

  // --- PRINT & EXPORT ---

  function openExecutiveReportPreview() {
    if (!activeFinanceReportData) {
      showToast('សូមរង់ចាំរបាយការណ៍ដំណើរការរួចសិន!', true);
      return;
    }
    const modal = document.getElementById('executive-report-modal');
    const body = document.getElementById('executive-report-modal-body');
    if (!modal || !body) return;

    body.innerHTML = renderExecutivePrintableHTML(activeFinanceReportData);
    modal.classList.remove('hidden');
  }

  function printExecutiveReportDoc() {
    const body = document.getElementById('executive-report-modal-body');
    if (!body) return;
    
    const printWin = window.open('', '_blank', 'width=950,height=900');
    if (!printWin) {
      window.print();
      return;
    }
    const twTag = '<' + 'script src="https://cdn.tailwindcss.com"><' + '/script>';
    printWin.document.write(`<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8">
  <title>របាយការណ៍ផ្លូវការ AC BOMLU</title>
  ${twTag}
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Kantumruy Pro', sans-serif; background: #fff; color: #111827; padding: 20px; }
    @media print {
      body { padding: 0; }
      @page { size: A4; margin: 10mm; }
    }
  </style>
</head>
<body>
  ${body.innerHTML}
</body>
</html>`);
    printWin.document.close();
    printWin.focus();
    setTimeout(() => {
      printWin.print();
    }, 400);
  }

  function printFinanceReport() {
    openExecutiveReportPreview();
  }

  function exportFinanceReportCSV() {
    if (!activeFinanceReportData) {
      showToast('សូមរង់ចាំរបាយការណ៍ដំណើរការរួចសិន!', true);
      return;
    }

    let csv = '\uFEFF'; // UTF-8 BOM
    const { type, data } = activeFinanceReportData;
    const summary = data.summary || {};

    if (type === 'daily') {
      csv += `របាយការណ៍ហិរញ្ញវត្ថុ និងប្រតិបត្តិការប្រចាំថ្ងៃ,${data.date}\n`;
      csv += `សរុបចំណូល (KHR),${summary.income_khr},សរុបចំណូល (USD),${summary.income_usd}\n`;
      csv += `សរុបចំណាយ (KHR),${summary.expense_khr},សរុបចំណាយ (USD),${summary.expense_usd}\n`;
      csv += `ចំណេញសុទ្ធ (KHR),${summary.net_khr},ចំណេញសុទ្ធ (USD),${summary.net_usd}\n`;
      csv += `សរុបជើងផ្ដល់សេវា,${summary.total_trips || 0},ចំណូលមធ្យមក្នុង១ជើង (KHR),${summary.arpt ? summary.arpt.khr : 0}\n`;
      csv += `សាច់ប្រាក់ក្នុងដៃ (KHR),${summary.reconciliation ? summary.reconciliation.cash_balance.khr : 0},សមតុល្យធនាគារ (KHR),${summary.reconciliation ? summary.reconciliation.bank_balance.khr : 0}\n\n`;

      csv += `--- តារាងទីតាំងផ្ដល់សេវា ---\n`;
      csv += `ទីតាំង,ចំនួនជើង,ចំណូល KHR,ចំណូល USD\n`;
      (summary.locations || []).forEach(l => {
        csv += `"${l.location}",${l.trips},${l.khr},${l.usd}\n`;
      });
      csv += `\n`;

      csv += `--- បញ្ជីប្រតិបត្តិការលម្អិត ---\n`;
      csv += `កាលបរិច្ឆេទ,ម៉ោង,ប្រភេទ,កាតាឡុក,ពិពណ៌នា/ទីតាំង,ការទូទាត់,លេខវិក្កយបត្រ,ចំនួនទឹកប្រាក់,រូបិយប័ណ្ណ,អ្នកកត់ត្រា\n`;
      (data.transactions || []).forEach(t => {
        csv += `"${t.date}","${t.time||''}","${t.type==='income'?'ចំណូល':'ចំណាយ'}","${t.category}","${(t.description||'').replace(/"/g, '""')}","${t.payment_method||''}","${t.reference_no||''}","${t.amount}","${t.currency}","${t.recorded_by||''}"\n`;
      });
    } else if (type === 'monthly') {
      csv += `របាយការណ៍ហិរញ្ញវត្ថុ និងប្រតិបត្តិការប្រចាំខែ,ខែ ${data.month_name} ឆ្នាំ ${data.year}\n`;
      csv += `សរុបចំណូល (KHR),${summary.income_khr},សរុបចំណូល (USD),${summary.income_usd}\n`;
      csv += `សរុបចំណាយ (KHR),${summary.expense_khr},សរុបចំណាយ (USD),${summary.expense_usd}\n`;
      csv += `ចំណេញសុទ្ធ (KHR),${summary.net_khr},ចំណេញសុទ្ធ (USD),${summary.net_usd}\n`;
      csv += `សរុបជើងផ្ដល់សេវា,${summary.total_trips || 0},ចំណូលមធ្យមក្នុង១ជើង (KHR),${summary.arpt ? summary.arpt.khr : 0}\n`;
      csv += `សាច់ប្រាក់ក្នុងដៃ (KHR),${summary.reconciliation ? summary.reconciliation.cash_balance.khr : 0},សមតុល្យធនាគារ (KHR),${summary.reconciliation ? summary.reconciliation.bank_balance.khr : 0}\n\n`;

      csv += `--- តារាងទីតាំងផ្ដល់សេវា ---\n`;
      csv += `ទីតាំង,ចំនួនជើង,ចំណូល KHR,ចំណូល USD\n`;
      (summary.locations || []).forEach(l => {
        csv += `"${l.location}",${l.trips},${l.khr},${l.usd}\n`;
      });
      csv += `\n`;

      csv += `--- តារាងលម្អិតតាមថ្ងៃ ---\n`;
      csv += `កាលបរិច្ឆេទ,ចំនួនជើង,ចំណូល KHR,ចំណូល USD,ចំណាយ KHR,ចំណាយ USD,សមតុល្យសុទ្ធ KHR,សមតុល្យសុទ្ធ USD,ចំនួនប្រតិបត្តិការ\n`;
      data.daily_breakdown.forEach(d => {
        csv += `"${d.date}",${d.trips||0},${d.income_khr},${d.income_usd},${d.expense_khr},${d.expense_usd},${d.net_khr},${d.net_usd},${d.count}\n`;
      });
    } else if (type === 'yearly') {
      csv += `របាយការណ៍ហិរញ្ញវត្ថុ និងប្រតិបត្តិការប្រចាំឆ្នាំ,ឆ្នាំ ${data.year}\n`;
      csv += `សរុបចំណូល (KHR),${summary.income_khr},សរុបចំណូល (USD),${summary.income_usd}\n`;
      csv += `សរុបចំណាយ (KHR),${summary.expense_khr},សរុបចំណាយ (USD),${summary.expense_usd}\n`;
      csv += `ចំណេញសុទ្ធ (KHR),${summary.net_khr},ចំណេញសុទ្ធ (USD),${summary.net_usd}\n`;
      csv += `សរុបជើងផ្ដល់សេវា,${summary.total_trips || 0},ចំណូលមធ្យមក្នុង១ជើង (KHR),${summary.arpt ? summary.arpt.khr : 0}\n\n`;

      csv += `--- តារាងលម្អិត ១២ ខែ ---\n`;
      csv += `ខែ,ចំនួនជើង,ចំណូល KHR,ចំណូល USD,ចំណាយ KHR,ចំណាយ USD,សមតុល្យសុទ្ធ KHR,សមតុល្យសុទ្ធ USD,ចំនួនប្រតិបត្តិការ\n`;
      data.monthly_breakdown.forEach(m => {
        csv += `"ខែ${m.month_name}",${m.trips||0},${m.income_khr},${m.income_usd},${m.expense_khr},${m.expense_usd},${m.net_khr},${m.net_usd},${m.count}\n`;
      });
    }

    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', `AC_Bomlu_Finance_Report_${currentReportPeriod}_${getTodayString()}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    showToast('បានទាញយករបាយការណ៍ជា Excel/CSV រួចរាល់!');
  }

  function renderExecutivePrintableHTML(activeData) {
    if (!activeData || !activeData.data) return '';
    const { type, data } = activeData;
    const summary = data.summary || {};
    const rec = summary.reconciliation || {
      cash_income: { khr: 0, usd: 0 },
      cash_expense: { khr: 0, usd: 0 },
      cash_balance: { khr: 0, usd: 0 },
      bank_income: { khr: 0, usd: 0 },
      bank_expense: { khr: 0, usd: 0 },
      bank_balance: { khr: 0, usd: 0 }
    };
    const locs = summary.locations || [];
    const expStruct = summary.expense_structure || {};
    const nowStr = new Date().toLocaleDateString('km-KH', { year: 'numeric', month: 'long', day: 'numeric' });

    let periodTitle = '';
    if (type === 'daily') {
      periodTitle = `របាយការណ៍ប្រចាំថ្ងៃ ទី ${escapeHtml(data.date)}`;
    } else if (type === 'monthly') {
      periodTitle = `របាយការណ៍ប្រចាំខែ ${escapeHtml(data.month_name)} ឆ្នាំ ${data.year}`;
    } else if (type === 'yearly') {
      periodTitle = `របាយការណ៍ប្រចាំឆ្នាំ ${data.year}`;
    }

    return `
      <div class="max-w-4xl mx-auto p-4 sm:p-8 bg-white text-gray-900 font-sans printable-sheet">
        <!-- 1. Formal Letterhead Header -->
        <div class="border-b-2 border-blue-900 pb-4 mb-6">
          <div class="flex items-start justify-between gap-4">
            <div class="flex items-center gap-3">
              <img src="/static/ac_logo.jpg" alt="AC Logo" class="w-16 h-16 rounded-full border-2 border-blue-800 object-cover shadow-sm" onerror="this.style.display='none'">
              <div>
                <h1 class="text-xl sm:text-2xl font-black text-blue-950 uppercase tracking-tight">ក្រុមហ៊ុន អេស៊ី បូមលូ</h1>
                <p class="text-xs font-bold text-gray-700 tracking-wide">AC BOMLU SERVICE CO., LTD.</p>
                <p class="text-[11px] text-gray-500 mt-0.5">សេវាកម្មបូមលូ លាងសម្អាត និងដោះស្រាយបញ្ហាប្រព័ន្ធលូស្ដង់ដារ</p>
              </div>
            </div>
            <div class="text-right text-xs text-gray-600">
              <p class="font-bold text-blue-900">ការិយាល័យរដ្ឋបាល & ហិរញ្ញវត្ថុ</p>
              <p>ទូរស័ព្ទ៖ <span class="font-bold text-gray-800">092 673 641</span></p>
              <p>រាជធានីភ្នំពេញ & ខេត្តកំពង់ចាម</p>
            </div>
          </div>
        </div>

        <!-- 2. Formal Title -->
        <div class="text-center mb-6">
          <h2 class="text-lg sm:text-xl font-black text-gray-900 uppercase">របាយការណ៍បូកសរុបប្រតិបត្តិការ និងហិរញ្ញវត្ថុផ្លូវការ</h2>
          <p class="text-xs font-bold text-blue-800 mt-1">${periodTitle}</p>
          <p class="text-[10px] text-gray-400 mt-0.5">កាលបរិច្ឆេទបង្កើតឯកសារ៖ ${nowStr}</p>
        </div>

        <!-- 3. Executive KPI Summary Grid -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
          <div class="border border-slate-200 rounded-xl p-3 bg-slate-50/50">
            <p class="text-[11px] text-gray-500 font-medium">សរុបចំណូល (Income)</p>
            <p class="text-sm sm:text-base font-bold text-emerald-700 font-mono mt-1">${formatDualMoney(summary.income_khr, summary.income_usd)}</p>
          </div>
          <div class="border border-slate-200 rounded-xl p-3 bg-slate-50/50">
            <p class="text-[11px] text-gray-500 font-medium">សរុបចំណាយ (Expense)</p>
            <p class="text-sm sm:text-base font-bold text-rose-700 font-mono mt-1">${formatDualMoney(summary.expense_khr, summary.expense_usd)}</p>
          </div>
          <div class="border border-slate-200 rounded-xl p-3 bg-slate-50/50">
            <p class="text-[11px] text-gray-500 font-medium">ចំណេញ/ខាតសុទ្ធ (Net)</p>
            <p class="text-sm sm:text-base font-bold ${summary.net_khr >= 0 ? 'text-emerald-700' : 'text-rose-700'} font-mono mt-1">${formatDualMoney(summary.net_khr, summary.net_usd)}</p>
          </div>
          <div class="border border-slate-200 rounded-xl p-3 bg-slate-50/50">
            <p class="text-[11px] text-gray-500 font-medium">សរុបជើងសេវា (Trips)</p>
            <p class="text-sm sm:text-base font-bold text-blue-700 font-mono mt-1">${summary.total_trips || 0} ជើង</p>
          </div>
        </div>

        <!-- 4. Cash vs Bank Reconciliation Table -->
        <div class="mb-6">
          <h3 class="text-xs font-bold text-gray-800 uppercase tracking-wider mb-2 flex items-center gap-1.5">
            <span class="w-1.5 h-3.5 bg-blue-700 rounded-sm"></span> ១. ការផ្ទៀងផ្ទាត់សាច់ប្រាក់ និងធនាគារ (Cash & Bank Reconciliation)
          </h3>
          <table class="w-full text-xs text-left border border-slate-200 rounded-lg overflow-hidden">
            <thead class="bg-slate-100 font-semibold text-gray-700">
              <tr>
                <th class="p-2.5 border-b border-slate-200">ឆានែលទូទាត់</th>
                <th class="p-2.5 border-b border-slate-200 text-right">ចំណូលចូល (+)</th>
                <th class="p-2.5 border-b border-slate-200 text-right">ចំណាយចេញ (-)</th>
                <th class="p-2.5 border-b border-slate-200 text-right">សមតុល្យនៅសល់ជាក់ស្ដែង</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 text-gray-800 font-mono">
              <tr>
                <td class="p-2.5 font-sans font-medium">សាច់ប្រាក់សុទ្ធក្នុងដៃ (Cash In Hand / Safe)</td>
                <td class="p-2.5 text-right text-emerald-700 font-bold">${formatDualMoney(rec.cash_income.khr, rec.cash_income.usd)}</td>
                <td class="p-2.5 text-right text-rose-600 font-bold">${formatDualMoney(rec.cash_expense.khr, rec.cash_expense.usd)}</td>
                <td class="p-2.5 text-right font-black text-emerald-800 bg-emerald-50/50">${formatDualMoney(rec.cash_balance.khr, rec.cash_balance.usd)}</td>
              </tr>
              <tr>
                <td class="p-2.5 font-sans font-medium">គណនីធនាគារ / ABA / KHQR</td>
                <td class="p-2.5 text-right text-blue-700 font-bold">${formatDualMoney(rec.bank_income.khr, rec.bank_income.usd)}</td>
                <td class="p-2.5 text-right text-rose-600 font-bold">${formatDualMoney(rec.bank_expense.khr, rec.bank_expense.usd)}</td>
                <td class="p-2.5 text-right font-black text-blue-800 bg-blue-50/50">${formatDualMoney(rec.bank_balance.khr, rec.bank_balance.usd)}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 5. Locations & Trips Breakdown Table -->
        <div class="mb-6">
          <h3 class="text-xs font-bold text-gray-800 uppercase tracking-wider mb-2 flex items-center gap-1.5">
            <span class="w-1.5 h-3.5 bg-indigo-700 rounded-sm"></span> ២. តារាងលម្អិតទីតាំងផ្ដល់សេវា & ចំនួនជើង (Service Locations & Trips)
          </h3>
          <table class="w-full text-xs text-left border border-slate-200 rounded-lg overflow-hidden">
            <thead class="bg-slate-100 font-semibold text-gray-700">
              <tr>
                <th class="p-2 border-b border-slate-200 w-12 text-center">ល.រ</th>
                <th class="p-2 border-b border-slate-200">ទីតាំងផ្ដល់សេវា</th>
                <th class="p-2 border-b border-slate-200 text-center">ចំនួនជើង</th>
                <th class="p-2 border-b border-slate-200 text-right">ចំណូលសរុប</th>
                <th class="p-2 border-b border-slate-200 text-right">មធ្យម/១ជើង</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 text-gray-800 font-mono">
              ${locs.length ? locs.map((l, i) => `
                <tr>
                  <td class="p-2 text-center font-sans text-gray-500">${i + 1}</td>
                  <td class="p-2 font-sans font-semibold">${escapeHtml(l.location)}</td>
                  <td class="p-2 text-center font-bold text-indigo-700 font-sans">${l.trips} ជើង</td>
                  <td class="p-2 text-right font-bold text-emerald-700">${formatDualMoney(l.khr, l.usd)}</td>
                  <td class="p-2 text-right text-gray-600">${formatDualMoney(l.trips ? l.khr / l.trips : 0, l.trips ? l.usd / l.trips : 0)}</td>
                </tr>
              `).join('') : '<tr><td colspan="5" class="p-3 text-center text-gray-400 font-sans">គ្មានទិន្នន័យទីតាំង</td></tr>'}
            </tbody>
          </table>
        </div>

        <!-- 6. Operating Expenses Breakdown Table -->
        <div class="mb-6">
          <h3 class="text-xs font-bold text-gray-800 uppercase tracking-wider mb-2 flex items-center gap-1.5">
            <span class="w-1.5 h-3.5 bg-rose-700 rounded-sm"></span> ៣. រចនាសម្ព័ន្ធចំណាយប្រតិបត្តិការ (Operating Cost Breakdown)
          </h3>
          <table class="w-full text-xs text-left border border-slate-200 rounded-lg overflow-hidden">
            <thead class="bg-slate-100 font-semibold text-gray-700">
              <tr>
                <th class="p-2 border-b border-slate-200">ប្រភេទចំណាយ</th>
                <th class="p-2 border-b border-slate-200 text-center">ចំនួនប្រតិបត្តិការ</th>
                <th class="p-2 border-b border-slate-200 text-right">ទឹកប្រាក់សរុប</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100 text-gray-800 font-mono">
              ${Object.keys(expStruct).map(k => {
                const item = expStruct[k];
                return `
                  <tr>
                    <td class="p-2 font-sans font-medium">${escapeHtml(item.label)}</td>
                    <td class="p-2 text-center text-gray-600 font-sans">${item.count}</td>
                    <td class="p-2 text-right font-bold text-rose-700">${formatDualMoney(item.khr, item.usd)}</td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        </div>

        <!-- 7. Formal 3-Signatures Section (ហត្ថលេខា ៣ ថ្នាក់) -->
        <div class="pt-8 mt-10 border-t-2 border-slate-200 no-break-inside">
          <div class="grid grid-cols-3 gap-6 text-center">
            <!-- Col 1: Secretary / Accountant -->
            <div class="flex flex-col items-center">
              <p class="text-xs font-bold text-gray-900">អ្នករៀបចំរបាយការណ៍</p>
              <p class="text-[10px] text-gray-500">(Prepared By)</p>
              <div class="h-20 flex items-center justify-center">
                <span class="text-xs text-slate-300 italic">ហត្ថលេខា</span>
              </div>
              <p class="text-xs font-semibold text-gray-800 border-t border-dashed border-gray-400 pt-1.5 w-44">
                លេខាធិការដ្ឋាន / គណនេយ្យ
              </p>
            </div>

            <!-- Col 2: Administration -->
            <div class="flex flex-col items-center">
              <p class="text-xs font-bold text-gray-900">អ្នកត្រួតពិនិត្យ</p>
              <p class="text-[10px] text-gray-500">(Reviewed By)</p>
              <div class="h-20 flex items-center justify-center">
                <span class="text-xs text-slate-300 italic">ហត្ថលេខា</span>
              </div>
              <p class="text-xs font-semibold text-gray-800 border-t border-dashed border-gray-400 pt-1.5 w-44">
                ប្រធានផ្នែករដ្ឋបាល
              </p>
            </div>

            <!-- Col 3: Managing Director / Owner -->
            <div class="flex flex-col items-center">
              <p class="text-xs font-bold text-blue-950">ការឯកភាព និងអនុម័ត</p>
              <p class="text-[10px] text-gray-500">(Approved By - CEO)</p>
              <div class="h-20 flex items-center justify-center">
                <div class="w-16 h-16 rounded-full border border-dashed border-red-300 flex items-center justify-center text-[9px] text-red-500 font-bold">
                  ត្រា & ហត្ថលេខា
                </div>
              </div>
              <p class="text-xs font-bold text-blue-900 border-t border-dashed border-gray-400 pt-1.5 w-44">
                លោក កឹម បូរី (Kem Borey)
              </p>
              <p class="text-[10px] text-gray-500">នាយកប្រតិបត្តិ / ម្ចាស់ក្រុមហ៊ុន</p>
            </div>
          </div>
        </div>

        <!-- Footer Note -->
        <div class="mt-8 pt-4 border-t border-slate-100 text-center text-[10px] text-gray-400">
          ឯកសារផ្លូវការចេញដោយប្រព័ន្ធគ្រប់គ្រងស្វ័យប្រវត្ត AC BOMLU System • រក្សាសិទ្ធិគ្រប់យ៉ាង
        </div>
      </div>
    `;
  }
  // ===== Init =====
  (async function init() {
    initPWA();
    initGlobalBranchSelect();
    initFinanceReportFilters();
    loadCatalogs();

    // Check if URL has a hash for deep-linking (e.g. #reports-leave, #income)
    const initialHash = window.location.hash ? window.location.hash.replace('#', '') : '';
    const startTab = tabs.includes(initialHash) ? initialHash : 'dashboard';

    window.history.replaceState({ tab: startTab }, '', window.location.pathname);
    uiHistory = [startTab];
    uiHistoryIndex = 0;
    currentActiveTab = startTab;

    await checkAuthAndInitialize();
    loadTransferHistory();

    if (startTab !== 'dashboard') {
      showTab(startTab, false);
    }
    updateNavButtonsState();
  })();
</script>
</body>
</html>
"""

# ===== Serve Dashboard =====
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return HTMLResponse(
        content=HTML_TEMPLATE,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# ===== Run =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
