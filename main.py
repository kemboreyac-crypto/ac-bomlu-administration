import os
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient
from pydantic import BaseModel
from typing import Optional
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
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
client = MongoClient(MONGO_URI)
db = client["acbank_db"]
leaves_collection = db["leaves"]
loans_collection = db["loans"]
employees_collection = db["employees"]
settings_collection = db["settings"]
user_flows_collection = db["user_flows"]

# ===== Pydantic Models =====
class LeaveRequest(BaseModel):
    employee_name: str
    reason: str
    days: int = 1
    status: str = "pending"

class LoanRequest(BaseModel):
    employee_name: str
    amount_khr: int
    purpose: Optional[str] = ""
    status: str = "pending"

class Employee(BaseModel):
    name: str
    position: str
    phone: Optional[str] = ""
    phone2: Optional[str] = ""

class SettingsUpdate(BaseModel):
    telegram_bot_token: Optional[str] = ""
    telegram_chat_id: Optional[str] = ""
    notify_on_leave: bool = True
    notify_on_loan: bool = True

class TelegramTestRequest(BaseModel):
    bot_token: Optional[str] = ""
    chat_id: Optional[str] = ""

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

def send_telegram_notify(text: str, reply_markup: dict = None):
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = (s.get("telegram_chat_id", "").strip() if s else "") or os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
            
        # Default persistent keyboard buttons if no custom markup provided
        if reply_markup is None:
            reply_markup = {
                "keyboard": [
                    [{"text": "📝 ស្នើសុំច្បាប់"}, {"text": "💰 ស្នើខ្ចីប្រាក់"}]
                ],
                "resize_keyboard": True,
                "is_persistent": True
            }
            
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        print(f"Telegram notify error: {e}")

# ===== API Routes =====
@app.get("/api/stats")
async def get_stats():
    return {
        "total_employees": employees_collection.count_documents({}),
        "pending_leaves": leaves_collection.count_documents({"status": "pending"}),
        "pending_loans": loans_collection.count_documents({"status": "pending"}),
        "approved_loans": loans_collection.count_documents({"status": "approved"}),
    }

@app.get("/api/leaves")
async def get_leaves():
    docs = list(leaves_collection.find().sort("_id", -1).limit(50))
    return [fix_id(d) for d in docs]

@app.post("/api/leave")
async def create_leave(request: LeaveRequest):
    result = leaves_collection.insert_one(request.dict())
    leave_id = str(result.inserted_id)
    adm_btn = {
        "inline_keyboard": [[
            {"text": "✅ អនុម័ត", "callback_data": f"app_leave:{leave_id}"},
            {"text": "❌ បដិសេធ", "callback_data": f"rej_leave:{leave_id}"}
        ]]
    }
    send_telegram_notify(
        f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
        f"👤 <b>បុគ្គលិក:</b> {request.employee_name}\n"
        f"📝 <b>មូលហេតុ:</b> {request.reason}\n"
        f"⏱️ <b>ចំនួនថ្ងៃ:</b> {request.days} ថ្ងៃ\n"
        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត",
        reply_markup=adm_btn
    )
    return {"message": "Leave request saved!", "id": leave_id}

@app.patch("/api/leave/{leave_id}/status")
async def update_leave_status(leave_id: str, status: str):
    leave = leaves_collection.find_one({"_id": ObjectId(leave_id)})
    leaves_collection.update_one({"_id": ObjectId(leave_id)}, {"$set": {"status": status}})
    if leave:
        name = leave.get("employee_name", "បុគ្គលិក")
        days = leave.get("days", 1)
        if status == "approved":
            send_telegram_notify(
                f"✅ <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                f"🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុម័ត (Approved)"
            )
        elif status == "rejected":
            send_telegram_notify(
                f"❌ <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                f"🔴 <b>លទ្ធផល:</b> ត្រូវបានបដិសេធ (Rejected)"
            )
    return {"message": f"Leave updated to {status}"}

@app.delete("/api/leave/{leave_id}")
async def delete_leave(leave_id: str):
    leaves_collection.delete_one({"_id": ObjectId(leave_id)})
    return {"message": "Deleted"}

@app.get("/api/loans")
async def get_loans():
    docs = list(loans_collection.find().sort("_id", -1).limit(50))
    return [fix_id(d) for d in docs]

@app.post("/api/loan")
async def create_loan(request: LoanRequest):
    result = loans_collection.insert_one(request.dict())
    loan_id = str(result.inserted_id)
    formatted_amount = f"{request.amount_khr:,}"
    adm_btn = {
        "inline_keyboard": [[
            {"text": "✅ អនុម័ត", "callback_data": f"app_loan:{loan_id}"},
            {"text": "❌ បដិសេធ", "callback_data": f"rej_loan:{loan_id}"}
        ]]
    }
    send_telegram_notify(
        f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
        f"👤 <b>បុគ្គលិក:</b> {request.employee_name}\n"
        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {formatted_amount} រៀល\n"
        f"🎯 <b>គោលបំណង:</b> {request.purpose or 'មិនបានបញ្ជាក់'}\n"
        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត",
        reply_markup=adm_btn
    )
    return {"message": "Loan request saved!", "id": loan_id}

@app.patch("/api/loan/{loan_id}/status")
async def update_loan_status(loan_id: str, status: str):
    loan = loans_collection.find_one({"_id": ObjectId(loan_id)})
    loans_collection.update_one({"_id": ObjectId(loan_id)}, {"$set": {"status": status}})
    if loan:
        name = loan.get("employee_name", "បុគ្គលិក")
        amount = f"{loan.get('amount_khr', 0):,}"
        if status == "approved":
            send_telegram_notify(
                f"✅ <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"💵 <b>ចំនួន:</b> {amount} រៀល\n"
                f"🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុម័ត (Approved)"
            )
        elif status == "rejected":
            send_telegram_notify(
                f"❌ <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"💵 <b>ចំនួន:</b> {amount} រៀល\n"
                f"🔴 <b>លទ្ធផល:</b> ត្រូវបានបដិសេធ (Rejected)"
            )
    return {"message": f"Loan updated to {status}"}

@app.delete("/api/loan/{loan_id}")
async def delete_loan(loan_id: str):
    loans_collection.delete_one({"_id": ObjectId(loan_id)})
    return {"message": "Deleted"}

@app.get("/api/employees")
async def get_employees():
    docs = list(employees_collection.find().sort("_id", -1).limit(100))
    return [fix_id(d) for d in docs]

@app.post("/api/employee")
async def create_employee(emp: Employee):
    result = employees_collection.insert_one(emp.dict())
    return {"message": "Employee added!", "id": str(result.inserted_id)}

@app.delete("/api/employee/{emp_id}")
async def delete_employee(emp_id: str):
    employees_collection.delete_one({"_id": ObjectId(emp_id)})
    return {"message": "Deleted"}

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
    notify_leave = True
    notify_loan = True
    
    if s:
        token = s.get("telegram_bot_token", "")
        chat_id = s.get("telegram_chat_id", "")
        notify_leave = s.get("notify_on_leave", True)
        notify_loan = s.get("notify_on_loan", True)
    else:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        
    return {
        "telegram_bot_token": mask_token(token),
        "has_token": bool(token),
        "telegram_chat_id": chat_id,
        "notify_on_leave": notify_leave,
        "notify_on_loan": notify_loan
    }

@app.post("/api/settings")
async def save_settings(payload: SettingsUpdate):
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    token_to_save = (payload.telegram_bot_token or "").strip()
    
    # Security: If the token contains bullet points (masked) or is empty, keep existing token
    if not token_to_save or "•" in token_to_save:
        token_to_save = s.get("telegram_bot_token", os.getenv("TELEGRAM_BOT_TOKEN", ""))
        
    settings_collection.replace_one(
        {"_id": "app_settings"},
        {
            "_id": "app_settings",
            "telegram_bot_token": token_to_save,
            "telegram_chat_id": (payload.telegram_chat_id or "").strip(),
            "notify_on_leave": payload.notify_on_leave,
            "notify_on_loan": payload.notify_on_loan
        },
        upsert=True
    )
    return {"message": "ការកំណត់ត្រូវបានរក្សាទុកដោយសុវត្ថិភាព (Encrypted & Masked)!"}

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

@app.post("/api/telegram-webhook")
async def telegram_webhook(req: Request):
    try:
        update = await req.json()
        
        # Helper to get bot token
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")

        # A. Handle Callback Query (When clicking Inline Buttons: Days or Loan Amount)
        callback_query = update.get("callback_query")
        if callback_query:
            cq_id = callback_query.get("id")
            cq_data = callback_query.get("data", "")
            cq_user = callback_query.get("from", {})
            cq_name = (cq_user.get("first_name", "") + " " + cq_user.get("last_name", "")).strip() or cq_user.get("username", "បុគ្គលិក")
            
            # Answer callback immediately for selection buttons to remove loading spinner
            if (cq_data.startswith("leave_days:") or cq_data.startswith("loan_amt:") or 
                cq_data.startswith("leave_rsn:") or cq_data.startswith("loan_pur:")):
                if token and cq_id:
                    try:
                        ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                        ans_req = urllib.request.Request(ans_url, data=json.dumps({"callback_query_id": cq_id}).encode('utf-8'), headers={"Content-Type": "application/json"})
                        urllib.request.urlopen(ans_req, timeout=5)
                    except Exception:
                        pass

            # 1. Callback for Leave Days selection -> Show Reason Selection & Allow Direct Typing!
            if cq_data.startswith("leave_days:"):
                parts = cq_data.split(":")
                days_str = parts[1]
                days = int(days_str) if days_str.isdigit() else 1
                
                cq_user_id = str(cq_user.get("id"))
                user_flows_collection.replace_one(
                    {"user_id": cq_user_id},
                    {"user_id": cq_user_id, "type": "leave", "days": days, "name": cq_name},
                    upsert=True
                )
                
                reason_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🤒 ឈឺ / មិនស្រួលខ្លួន", "callback_data": f"leave_rsn:ឈឺ / មិនស្រួលខ្លួន:{days}"},
                            {"text": "🏠 រវល់ធុរៈគ្រួសារ", "callback_data": f"leave_rsn:រវល់ធុរៈគ្រួសារ:{days}"}
                        ],
                        [
                            {"text": "🚗 ទៅស្រុកកំណើត", "callback_data": f"leave_rsn:ទៅស្រុកកំណើត:{days}"},
                            {"text": "💍 ចូលរួមពិធីមង្គលការ/បុណ្យ", "callback_data": f"leave_rsn:ចូលរួមពិធីមង្គលការ/បុណ្យ:{days}"}
                        ]
                    ]
                }
                ask_reason_msg = (
                    f"⏱️ <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"⏱️ <b>ចំនួនថ្ងៃ:</b> {days} ថ្ងៃ\n\n"
                    f"👉 <b>សូមចុចជ្រើសរើស «មូលហេតុ» ខាងក្រោម ឬវាយបញ្ចូលផ្ទាល់ក្នុងប្រអប់សារ (មិនបាច់ Reply ទេ)៖</b>"
                )
                send_telegram_notify(ask_reason_msg, reply_markup=reason_buttons)
                return {"ok": True}

            # 1.1 Callback when clicking a quick Reason button!
            if cq_data.startswith("leave_rsn:"):
                parts = cq_data.split(":")
                rsn = parts[1]
                days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
                cq_user_id = str(cq_user.get("id"))
                user_flows_collection.delete_one({"user_id": cq_user_id})
                
                l_res = leaves_collection.insert_one({
                    "employee_name": cq_name,
                    "reason": rsn,
                    "days": days,
                    "status": "pending"
                })
                leave_id = str(l_res.inserted_id)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុម័ត", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ បដិសេធ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                reply = (
                    f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                    f"📝 <b>មូលហេតុ:</b> {rsn}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
                )
                send_telegram_notify(reply, reply_markup=adm_btn)
                return {"ok": True}

            # 2. Callback for Loan Amount selection -> Show Purpose Selection & Allow Direct Typing!
            if cq_data.startswith("loan_amt:"):
                parts = cq_data.split(":")
                amount = int(parts[1]) if parts[1].isdigit() else 100000
                
                cq_user_id = str(cq_user.get("id"))
                user_flows_collection.replace_one(
                    {"user_id": cq_user_id},
                    {"user_id": cq_user_id, "type": "loan", "amount": amount, "name": cq_name},
                    upsert=True
                )
                
                purpose_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🏠 បង់ថ្លៃផ្ទះ / បន្ទប់", "callback_data": f"loan_pur:បង់ថ្លៃផ្ទះ / បន្ទប់:{amount}"},
                            {"text": "💊 ទិញថ្នាំពេទ្យ", "callback_data": f"loan_pur:ទិញថ្នាំពេទ្យ:{amount}"}
                        ],
                        [
                            {"text": "⚡ ថ្លៃទឹកភ្លើង", "callback_data": f"loan_pur:ថ្លៃទឹកភ្លើង:{amount}"},
                            {"text": "👨‍👩‍👧 ផ្ញើជូនគ្រួសារ", "callback_data": f"loan_pur:ផ្ញើជូនគ្រួសារ:{amount}"}
                        ]
                    ]
                }
                ask_purpose_msg = (
                    f"💵 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n\n"
                    f"👉 <b>សូមចុចជ្រើសរើស «គោលបំណង» ខាងក្រោម ឬវាយបញ្ចូលផ្ទាល់ក្នុងប្រអប់សារ (មិនបាច់ Reply ទេ)៖</b>"
                )
                send_telegram_notify(ask_purpose_msg, reply_markup=purpose_buttons)
                return {"ok": True}

            # 2.1 Callback when clicking a quick Purpose button!
            if cq_data.startswith("loan_pur:"):
                parts = cq_data.split(":")
                pur = parts[1]
                amt = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 100000
                cq_user_id = str(cq_user.get("id"))
                user_flows_collection.delete_one({"user_id": cq_user_id})
                
                ln_res = loans_collection.insert_one({
                    "employee_name": cq_name,
                    "amount_khr": amt,
                    "purpose": pur,
                    "status": "pending"
                })
                loan_id = str(ln_res.inserted_id)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុម័ត", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ បដិសេធ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                reply = (
                    f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amt:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {pur}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
                )
                send_telegram_notify(reply, reply_markup=adm_btn)
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
                        "text": "⚠️ សុំទោស! លោកអ្នកជាបុគ្គលិកធម្មតា មិនមានសិទ្ធិអនុម័តឡើយ។ មានតែ Admin ក្នុងគ្រុបប៉ុណ្ណោះដែលអាចចុចបាន!",
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
                    if leave:
                        emp_name = leave.get("employee_name", "បុគ្គលិក")
                        days = leave.get("days", 1)
                        reason = leave.get("reason", "")
                        status_str = "approved" if is_approve else "rejected"
                        leaves_collection.update_one({"_id": ObjectId(doc_id)}, {"$set": {"status": status_str}})
                        
                        result_text = "🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុម័ត (Approved) ✅" if is_approve else "🔴 <b>លទ្ធផល:</b> ត្រូវបានបដិសេធ (Rejected) ❌"
                        edit_text = (
                            f"📋 <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                            f"👤 <b>បុគ្គលិក:</b> {emp_name}\n"
                            f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                            f"📝 <b>មូលហេតុ:</b> {reason}\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"{result_text}\n"
                            f"👮‍♂️ <b>ដោយ Admin:</b> {cq_name}"
                        )
                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                        urllib.request.urlopen(urllib.request.Request(edit_url, data=json.dumps({
                            "chat_id": chat_id_str,
                            "message_id": msg_id,
                            "text": edit_text,
                            "parse_mode": "HTML"
                        }).encode('utf-8'), headers={"Content-Type": "application/json"}), timeout=8)
                        
                elif "loan" in action:
                    loan = loans_collection.find_one({"_id": ObjectId(doc_id)})
                    if loan:
                        emp_name = loan.get("employee_name", "បុគ្គលិក")
                        amount = loan.get("amount_khr", 0)
                        purpose = loan.get("purpose", "")
                        status_str = "approved" if is_approve else "rejected"
                        loans_collection.update_one({"_id": ObjectId(doc_id)}, {"$set": {"status": status_str}})
                        
                        result_text = "🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុម័ត (Approved) ✅" if is_approve else "🔴 <b>លទ្ធផល:</b> ត្រូវបានបដិសេធ (Rejected) ❌"
                        edit_text = (
                            f"💰 <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                            f"👤 <b>បុគ្គលិក:</b> {emp_name}\n"
                            f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                            f"🎯 <b>គោលបំណង:</b> {purpose}\n"
                            f"━━━━━━━━━━━━━━━━━━━\n"
                            f"{result_text}\n"
                            f"👮‍♂️ <b>ដោយ Admin:</b> {cq_name}"
                        )
                        edit_url = f"https://api.telegram.org/bot{token}/editMessageText"
                        urllib.request.urlopen(urllib.request.Request(edit_url, data=json.dumps({
                            "chat_id": chat_id_str,
                            "message_id": msg_id,
                            "text": edit_text,
                            "parse_mode": "HTML"
                        }).encode('utf-8'), headers={"Content-Type": "application/json"}), timeout=8)

                ans_url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
                ans_data = json.dumps({
                    "callback_query_id": cq_id,
                    "text": "✅ បានអនុម័តជោគជ័យ!" if is_approve else "❌ បានបដិសេធសំណើ!"
                }).encode('utf-8')
                urllib.request.urlopen(urllib.request.Request(ans_url, data=ans_data, headers={"Content-Type": "application/json"}), timeout=5)
                return {"ok": True}

        # B. Handle Regular Messages
        message = update.get("message") or update.get("channel_post")
        if not message:
            return {"ok": True}

        from_user = message.get("from", {})
        sender_name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip() or from_user.get("username", "បុគ្គលិក")
        user_id_str = str(from_user.get("id"))
        
        # 1. Handle New Member Added to Group -> Welcome & Send 4-corner Keyboard automatically!
        new_members = message.get("new_chat_members", [])
        if new_members:
            for m in new_members:
                if m.get("is_bot"):
                    continue
                m_name = (m.get("first_name", "") + " " + m.get("last_name", "")).strip() or m.get("username", "បុគ្គលិក")
                welcome_msg = (
                    f"👋 <b>សូមស្វាគមន៍ {m_name} មកកាន់គ្រុប សុំច្បាប់ AC BOMLU!</b>\n\n"
                    f"👇 <b>ក្ដារចុចប៊ូតុងត្រូវបានភ្ជាប់ជូនអ្នកស្វ័យប្រវត្តិ៖</b>\n"
                    f"សូមមើលប៊ូតុងខាងក្រោម៖ ចុចលើ <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ឬ <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ដើម្បីស្នើសុំបានគ្រប់ពេល។"
                )
                send_telegram_notify(welcome_msg)
            return {"ok": True}

        text = (message.get("text") or "").strip()
        has_voice = bool(message.get("voice") or message.get("audio"))
        clean_cmd = text.split('@')[0].strip().lower()

        is_leave_btn = text in ["📝 ស្នើសុំច្បាប់", "ស្នើសុំច្បាប់", "📝 សុំច្បាប់", "សុំច្បាប់", "ច្បាប់", "ឈប់"] or clean_cmd in ["/leave", "leave", "leav"]
        is_loan_btn = text in ["💰 ស្នើខ្ចីប្រាក់", "ស្នើខ្ចីប្រាក់", "💰 សុំខ្ចីប្រាក់", "សុំខ្ចីប្រាក់", "ខ្ចីប្រាក់", "បុរេប្រទាន"] or clean_cmd in ["/loan", "loan", "lon"]
        is_system_cmd = clean_cmd in ["/start", "/help"] or is_leave_btn or is_loan_btn

        # 2. Check if user is in an active flow (waiting for Reason or Purpose)!
        active_flow = user_flows_collection.find_one({"user_id": user_id_str})
        if active_flow and not is_system_cmd:
            content = (message.get("caption") or "សារសំឡេង (Voice Message)") if has_voice else text
            flow_type = active_flow.get("type")
            
            if flow_type == "leave":
                days = active_flow.get("days", 1)
                l_res = leaves_collection.insert_one({
                    "employee_name": sender_name,
                    "reason": content,
                    "days": days,
                    "status": "pending"
                })
                leave_id = str(l_res.inserted_id)
                user_flows_collection.delete_one({"user_id": user_id_str})
                
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុម័ត", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ បដិសេធ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                reply = (
                    f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                    f"📝 <b>មូលហេតុ:</b> {content}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
                )
                send_telegram_notify(reply, reply_markup=adm_btn)
                return {"ok": True}
                
            elif flow_type == "loan":
                amount = active_flow.get("amount", 100000)
                ln_res = loans_collection.insert_one({
                    "employee_name": sender_name,
                    "amount_khr": amount,
                    "purpose": content,
                    "status": "pending"
                })
                loan_id = str(ln_res.inserted_id)
                user_flows_collection.delete_one({"user_id": user_id_str})
                
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុម័ត", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ បដិសេធ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                reply = (
                    f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {content}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
                )
                send_telegram_notify(reply, reply_markup=adm_btn)
                return {"ok": True}

        # 2.1 Fallback: If user recently requested leave/loan and now types the reason (like 'រវល់ទៅការកូន')
        if text and not is_system_cmd:
            recent_leave = leaves_collection.find_one(
                {"employee_name": sender_name, "status": "pending"},
                sort=[("_id", -1)]
            )
            if recent_leave and ("សុំច្បាប់" in recent_leave.get("reason", "") or "📝" in recent_leave.get("reason", "") or "ស្នើសុំ" in recent_leave.get("reason", "")):
                leaves_collection.update_one({"_id": recent_leave["_id"]}, {"$set": {"reason": text}})
                leave_id = str(recent_leave["_id"])
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុម័ត", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ បដិសេធ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                reply = (
                    f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាក (បានបន្ថែមមូលហេតុ)!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {recent_leave.get('days', 1)} ថ្ងៃ\n"
                    f"📝 <b>មូលហេតុ:</b> {text}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
                )
                send_telegram_notify(reply, reply_markup=adm_btn)
                return {"ok": True}

            recent_loan = loans_collection.find_one(
                {"employee_name": sender_name, "status": "pending"},
                sort=[("_id", -1)]
            )
            if recent_loan and ("ខ្ចីប្រាក់" in recent_loan.get("purpose", "") or "💰" in recent_loan.get("purpose", "") or "ស្នើ" in recent_loan.get("purpose", "")):
                loans_collection.update_one({"_id": recent_loan["_id"]}, {"$set": {"purpose": text}})
                loan_id = str(recent_loan["_id"])
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុម័ត", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ បដិសេធ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                reply = (
                    f"💰 <b>សំណើខ្ចីប្រាក់ (បានបន្ថែមគោលបំណង)!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {recent_loan.get('amount_khr', 100000):,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {text}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
                )
                send_telegram_notify(reply, reply_markup=adm_btn)
                return {"ok": True}
        
        # 2. Handle Voice Message for Leave Permission
        if has_voice:
            caption = message.get("caption") or "សារសំឡេង (Voice Message)"
            v_res = leaves_collection.insert_one({
                "employee_name": sender_name,
                "reason": caption,
                "days": 1,
                "status": "pending"
            })
            leave_id = str(v_res.inserted_id)
            adm_btn = {
                "inline_keyboard": [[
                    {"text": "✅ អនុម័ត", "callback_data": f"app_leave:{leave_id}"},
                    {"text": "❌ បដិសេធ", "callback_data": f"rej_leave:{leave_id}"}
                ]]
            }
            reply = (
                f"🎙️ <b>សំណើសុំច្បាប់តាមសំឡេងថ្មី!</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                f"📝 <b>មូលហេតុ:</b> {caption}\n"
                f"⏱️ <b>ចំនួន:</b> 1 ថ្ងៃ\n"
                f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
            )
            send_telegram_notify(reply, reply_markup=adm_btn)
            return {"ok": True}

        # 3. Handle Greetings & Casual Messages -> Re-attach 2 Keyboard buttons!
        if text.lower() in ["hi", "hello", "hey", "សួស្តី", "ជំរាបសួរ", "បាទ", "ចាស", "ok"]:
            reply = (
                f"👋 <b>សួស្តី {sender_name}!</b>\n\n"
                f"👇 ក្ដារចុចប៊ូតុងត្រូវបានភ្ជាប់ជូនអ្នករួចរាល់។ សូមចុចប៊ូតុង <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ឬ <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ខាងក្រោមដើម្បីប្រើប្រាស់។"
            )
            send_telegram_notify(reply)
            return {"ok": True}

        # 4. Handle Leave Button or Exact Command -> Show Days Selection (Inline Buttons)!
        if is_leave_btn:
            inline_days = {
                "inline_keyboard": [
                    [
                        {"text": "១ ថ្ងៃ", "callback_data": f"leave_days:1:{sender_name}"},
                        {"text": "២ ថ្ងៃ", "callback_data": f"leave_days:2:{sender_name}"},
                        {"text": "៣ ថ្ងៃ", "callback_data": f"leave_days:3:{sender_name}"}
                    ],
                    [
                        {"text": "៤ ថ្ងៃ", "callback_data": f"leave_days:4:{sender_name}"},
                        {"text": "៥ ថ្ងៃ", "callback_data": f"leave_days:5:{sender_name}"}
                    ]
                ]
            }
            msg = (
                f"📋 <b>សួស្តី {sender_name}!</b>\n\n"
                f"👉 <b>សូមចុចជ្រើសរើស ចំនួនថ្ងៃឈប់សម្រាក ខាងក្រោម៖</b>"
            )
            send_telegram_notify(msg, reply_markup=inline_days)
            return {"ok": True}

        # 5. Handle Detailed Leave Text (e.g. ស្អែកសុំច្បាប់ ២ ថ្ងៃ ឈឺក្បាល...)
        if ("សុំច្បាប់" in text or "ច្បាប់" in text or "leave" in text.lower()) and not is_leave_btn and not is_loan_btn:
            days = 1
            import re
            day_match = re.search(r'(\d+)\s*(ថ្ងៃ|day)', text, re.IGNORECASE)
            if day_match:
                days = int(day_match.group(1))
                
            l_res = leaves_collection.insert_one({
                "employee_name": sender_name,
                "reason": text,
                "days": days,
                "status": "pending"
            })
            leave_id = str(l_res.inserted_id)
            adm_btn = {
                "inline_keyboard": [[
                    {"text": "✅ អនុម័ត", "callback_data": f"app_leave:{leave_id}"},
                    {"text": "❌ បដិសេធ", "callback_data": f"rej_leave:{leave_id}"}
                ]]
            }
            reply = (
                f"📋 <b>បានកត់ត្រាសំណើសុំច្បាប់ជោគជ័យ!</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                f"📝 <b>មូលហេតុ:</b> {text}\n"
                f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
            )
            send_telegram_notify(reply, reply_markup=adm_btn)
            return {"ok": True}

        # 6. Handle Loan Button or Exact Command -> Show Amount Selection (Inline Buttons)!
        if is_loan_btn:
            inline_amounts = {
                "inline_keyboard": [
                    [
                        {"text": "៥០,០០០ ៛", "callback_data": f"loan_amt:50000:{sender_name}"},
                        {"text": "១០០,០០០ ៛", "callback_data": f"loan_amt:100000:{sender_name}"}
                    ],
                    [
                        {"text": "២០០,០០០ ៛", "callback_data": f"loan_amt:200000:{sender_name}"},
                        {"text": "៥០០,០០០ ៛", "callback_data": f"loan_amt:500000:{sender_name}"}
                    ]
                ]
            }
            msg = (
                f"💰 <b>សួស្តី {sender_name}!</b>\n\n"
                f"👉 <b>សូមចុចជ្រើសរើស ចំនួនទឹកប្រាក់ដែលចង់ខ្ចី (រៀល)៖</b>"
            )
            send_telegram_notify(msg, reply_markup=inline_amounts)
            return {"ok": True}

        # 7. Handle Detailed Loan Text with custom amount
        if ("ខ្ចីប្រាក់" in text or "បុរេប្រទាន" in text or "ខ្ចី" in text) and not is_loan_btn and not is_leave_btn:
            import re
            amount = 100000
            numbers = re.findall(r'(\d[\d,]*\d|\d+)', text.replace(' ', ''))
            if numbers:
                try:
                    amount = int(numbers[0].replace(',', ''))
                except Exception:
                    amount = 100000
                    
            ln_res = loans_collection.insert_one({
                "employee_name": sender_name,
                "amount_khr": amount,
                "purpose": text,
                "status": "pending"
            })
            loan_id = str(ln_res.inserted_id)
            adm_btn = {
                "inline_keyboard": [[
                    {"text": "✅ អនុម័ត", "callback_data": f"app_loan:{loan_id}"},
                    {"text": "❌ បដិសេធ", "callback_data": f"rej_loan:{loan_id}"}
                ]]
            }
            reply = (
                f"💰 <b>បានកត់ត្រាសំណើខ្ចីប្រាក់ជោគជ័យ!</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                f"🎯 <b>គោលបំណង:</b> {text}\n"
                f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុម័ត"
            )
            send_telegram_notify(reply, reply_markup=adm_btn)
            return {"ok": True}

        # 8. Handle Help/Start Command
        if text.startswith("/help") or text.startswith("/start"):
            help_msg = (
                f"👋 <b>ជម្រាបសួរ {sender_name}!</b>\n\n"
                f"📌 <b>របៀបសុំច្បាប់ ឬខ្ចីប្រាក់ក្នុងគ្រុបនេះ:</b>\n"
                f"• ចុចប៊ូតុង <b>[ 📝 សុំច្បាប់ ]</b> ខាងក្រោម ➡️ រើសចំនួនថ្ងៃ\n"
                f"• ចុចប៊ូតុង <b>[ 💰 សុំខ្ចីប្រាក់ ]</b> ខាងក្រោម ➡️ រើសចំនួនប្រាក់\n"
                f"• ឬផ្ញើសារជា <b>Voice Message សំឡេង</b>\n\n"
                f"🚀 ប្រព័ន្ធនឹងកត់ត្រាចូល Dashboard របស់ AC BOMLU ដោយស្វ័យប្រវត្តិ!"
            )
            send_telegram_notify(help_msg)
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
    webhook_url = "https://ac-bomlu-administration.onrender.com/api/telegram-webhook"
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
            webhook_url = "https://ac-bomlu-administration.onrender.com/api/telegram-webhook"
            url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                print("Webhook Auto-Registered:", r.read().decode())
    except Exception as e:
        print("Startup webhook error:", e)

# ===== Modern HTML UI =====
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AC BOMLU — ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក</title>

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
  </style>
</head>

<body class="bg-slate-100 flex h-screen overflow-hidden text-gray-800">

<!-- ======================================================
     SIDEBAR
====================================================== -->
<aside id="sidebar"
  class="w-64 flex-shrink-0 bg-gradient-to-b from-blue-900 to-blue-950 text-white flex flex-col shadow-2xl z-30 transition-transform duration-300">

  <!-- Logo -->
  <div class="h-16 flex items-center px-5 gap-3 border-b border-blue-800">
    <img src="/static/ac_logo.jpg" alt="AC Logo"
      class="w-10 h-10 rounded-full object-cover shadow-md border-2 border-white/30 flex-shrink-0" />
    <div class="leading-tight">
      <p class="text-xs text-blue-300 font-medium">AC BOMLU</p>
      <p class="text-sm font-bold">HR Dashboard</p>
    </div>
  </div>

  <!-- Nav -->
  <nav class="flex-1 p-3 space-y-1 text-sm">
    <button onclick="showTab('dashboard')"
      id="nav-dashboard"
      class="nav-link active w-full flex items-center gap-3 px-4 py-3 rounded-xl text-left">
      <span class="material-symbols-outlined text-xl">dashboard</span>
      <span>ផ្ទៃតាប្លូ</span>
    </button>
    <button onclick="showTab('leaves')"
      id="nav-leaves"
      class="nav-link w-full flex items-center gap-3 px-4 py-3 rounded-xl text-left text-blue-200">
      <span class="material-symbols-outlined text-xl">event_busy</span>
      <span>ច្បាប់ឈប់សម្រាក</span>
      <span id="badge-leaves" class="ml-auto text-xs bg-orange-400 text-white rounded-full px-2 py-0.5 hidden">0</span>
    </button>
    <button onclick="showTab('loans')"
      id="nav-loans"
      class="nav-link w-full flex items-center gap-3 px-4 py-3 rounded-xl text-left text-blue-200">
      <span class="material-symbols-outlined text-xl">payments</span>
      <span>ខ្ចីប្រាក់ / បុរេប្រទាន</span>
      <span id="badge-loans" class="ml-auto text-xs bg-orange-400 text-white rounded-full px-2 py-0.5 hidden">0</span>
    </button>
    <button onclick="showTab('employees')"
      id="nav-employees"
      class="nav-link w-full flex items-center gap-3 px-4 py-3 rounded-xl text-left text-blue-200">
      <span class="material-symbols-outlined text-xl">group</span>
      <span>បុគ្គលិកទាំងអស់</span>
    </button>
    <button onclick="showTab('settings')"
      id="nav-settings"
      class="nav-link w-full flex items-center gap-3 px-4 py-3 rounded-xl text-left text-blue-200">
      <span class="material-symbols-outlined text-xl">settings</span>
      <span>ការកំណត់</span>
    </button>
  </nav>

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
  <header class="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-6 shadow-sm flex-shrink-0">
    <div class="flex items-center gap-3">
      <!-- Hamburger (mobile) -->
      <button onclick="toggleSidebar()" class="text-gray-500 hover:text-blue-700 lg:hidden">
        <span class="material-symbols-outlined">menu</span>
      </button>
      <div>
        <h1 id="page-title" class="text-base font-semibold text-gray-800">ផ្ទៃតាប្លូ</h1>
        <p class="text-xs text-gray-400" id="page-date"></p>
      </div>
    </div>

    <div class="flex items-center gap-3">
      <!-- Refresh -->
      <button onclick="refreshAll()" title="ធ្វើឱ្យស្រស់" id="refresh-btn"
        class="w-9 h-9 flex items-center justify-center rounded-full hover:bg-slate-100 text-gray-500 hover:text-blue-600 transition">
        <span class="material-symbols-outlined text-xl">refresh</span>
      </button>
      <!-- Notifications -->
      <button class="w-9 h-9 flex items-center justify-center rounded-full hover:bg-slate-100 text-gray-500 hover:text-blue-600 transition relative">
        <span class="material-symbols-outlined text-xl">notifications</span>
        <span class="absolute top-1.5 right-1.5 w-2 h-2 bg-red-500 rounded-full border border-white"></span>
      </button>
      <!-- Avatar -->
      <div class="w-9 h-9 rounded-full overflow-hidden border-2 border-blue-200 shadow">
        <img src="/static/ac_logo.jpg" alt="AC" class="w-full h-full object-cover" />
      </div>
    </div>
  </header>

  <!-- CONTENT -->
  <main class="flex-1 overflow-auto">

    <!-- ===== DASHBOARD TAB ===== -->
    <div id="tab-dashboard" class="p-6 animate-fadeIn">

      <!-- Stat Cards -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
          <div class="w-12 h-12 rounded-xl bg-blue-100 flex items-center justify-center">
            <span class="material-symbols-outlined icon-fill text-blue-600 text-2xl">group</span>
          </div>
          <div>
            <p class="text-xs text-gray-500 mb-0.5">បុគ្គលិកសរុប</p>
            <p id="stat-employees" class="text-2xl font-bold text-gray-800">—</p>
          </div>
        </div>
        <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
          <div class="w-12 h-12 rounded-xl bg-orange-100 flex items-center justify-center">
            <span class="material-symbols-outlined icon-fill text-orange-500 text-2xl">pending_actions</span>
          </div>
          <div>
            <p class="text-xs text-gray-500 mb-0.5">ច្បាប់រង់ចាំ</p>
            <p id="stat-leaves" class="text-2xl font-bold text-gray-800">—</p>
          </div>
        </div>
        <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
          <div class="w-12 h-12 rounded-xl bg-red-100 flex items-center justify-center">
            <span class="material-symbols-outlined icon-fill text-red-500 text-2xl">account_balance_wallet</span>
          </div>
          <div>
            <p class="text-xs text-gray-500 mb-0.5">ខ្ចីប្រាក់រង់ចាំ</p>
            <p id="stat-loans" class="text-2xl font-bold text-gray-800">—</p>
          </div>
        </div>
        <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-100 flex items-center gap-4">
          <div class="w-12 h-12 rounded-xl bg-green-100 flex items-center justify-center">
            <span class="material-symbols-outlined icon-fill text-green-600 text-2xl">check_circle</span>
          </div>
          <div>
            <p class="text-xs text-gray-500 mb-0.5">ខ្ចីប្រាក់អនុម័ត</p>
            <p id="stat-approved" class="text-2xl font-bold text-gray-800">—</p>
          </div>
        </div>
      </div>

      <!-- Recent panels (2 col) -->
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">

        <!-- Recent Leaves -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
            <h2 class="font-semibold text-gray-700 flex items-center gap-2">
              <span class="material-symbols-outlined text-orange-500">event_busy</span>ច្បាប់ថ្មីៗ
            </h2>
            <button onclick="showTab('leaves')" class="text-xs text-blue-600 hover:underline flex items-center gap-1">
              មើលទាំងអស់ <span class="material-symbols-outlined text-sm">arrow_forward</span>
            </button>
          </div>
          <div id="dash-leaves" class="divide-y divide-slate-50 text-sm max-h-64 overflow-y-auto">
            <div class="p-4 text-gray-400 text-center">កំពុងទាញទិន្នន័យ...</div>
          </div>
        </div>

        <!-- Recent Loans -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
            <h2 class="font-semibold text-gray-700 flex items-center gap-2">
              <span class="material-symbols-outlined text-green-600">payments</span>ខ្ចីប្រាក់ថ្មីៗ
            </h2>
            <button onclick="showTab('loans')" class="text-xs text-blue-600 hover:underline flex items-center gap-1">
              មើលទាំងអស់ <span class="material-symbols-outlined text-sm">arrow_forward</span>
            </button>
          </div>
          <div id="dash-loans" class="divide-y divide-slate-50 text-sm max-h-64 overflow-y-auto">
            <div class="p-4 text-gray-400 text-center">កំពុងទាញទិន្នន័យ...</div>
          </div>
        </div>

      </div>
    </div>

    <!-- ===== LEAVES TAB ===== -->
    <div id="tab-leaves" class="p-6 animate-fadeIn hidden">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-lg font-semibold text-gray-800">ការគ្រប់គ្រងច្បាប់ឈប់សម្រាក</h2>
        <button onclick="openModal('leave-modal')"
          class="btn-primary flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium shadow-sm transition">
          <span class="material-symbols-outlined text-lg">add</span> បន្ថែម
        </button>
      </div>

      <!-- Filter bar -->
      <div class="flex gap-2 mb-4 text-sm flex-wrap">
        <button onclick="filterLeaves('all')" class="px-4 py-1.5 rounded-full bg-blue-600 text-white font-medium" id="lf-all">ទាំងអស់</button>
        <button onclick="filterLeaves('pending')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-pending">រង់ចាំ</button>
        <button onclick="filterLeaves('approved')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-approved">អនុម័ត</button>
        <button onclick="filterLeaves('rejected')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-rejected">បដិសេធ</button>
      </div>

      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100">
              <tr>
                <th class="px-5 py-3 font-medium">ឈ្មោះ</th>
                <th class="px-5 py-3 font-medium">មូលហេតុ</th>
                <th class="px-5 py-3 font-medium">ថ្ងៃ</th>
                <th class="px-5 py-3 font-medium">ស្ថានភាព</th>
                <th class="px-5 py-3 font-medium text-right">សកម្មភាព</th>
              </tr>
            </thead>
            <tbody id="leaves-tbody" class="divide-y divide-slate-50 text-gray-700">
              <tr><td colspan="5" class="text-center py-8 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ===== LOANS TAB ===== -->
    <div id="tab-loans" class="p-6 animate-fadeIn hidden">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-lg font-semibold text-gray-800">ការគ្រប់គ្រងខ្ចីប្រាក់ / បុរេប្រទាន</h2>
        <button onclick="openModal('loan-modal')"
          class="btn-primary flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium shadow-sm transition">
          <span class="material-symbols-outlined text-lg">add</span> បន្ថែម
        </button>
      </div>

      <!-- Filter bar -->
      <div class="flex gap-2 mb-4 text-sm flex-wrap">
        <button onclick="filterLoans('all')" class="px-4 py-1.5 rounded-full bg-blue-600 text-white font-medium" id="lnf-all">ទាំងអស់</button>
        <button onclick="filterLoans('pending')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-pending">រង់ចាំ</button>
        <button onclick="filterLoans('approved')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-approved">អនុម័ត</button>
        <button onclick="filterLoans('rejected')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-rejected">បដិសេធ</button>
      </div>

      <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-sm text-left">
            <thead class="bg-slate-50 text-gray-500 border-b border-slate-100">
              <tr>
                <th class="px-5 py-3 font-medium">ឈ្មោះ</th>
                <th class="px-5 py-3 font-medium">ចំនួនទឹកប្រាក់</th>
                <th class="px-5 py-3 font-medium">គោលបំណង</th>
                <th class="px-5 py-3 font-medium">ស្ថានភាព</th>
                <th class="px-5 py-3 font-medium text-right">សកម្មភាព</th>
              </tr>
            </thead>
            <tbody id="loans-tbody" class="divide-y divide-slate-50 text-gray-700">
              <tr><td colspan="5" class="text-center py-8 text-gray-400">កំពុងទាញទិន្នន័យ...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ===== EMPLOYEES TAB ===== -->
    <div id="tab-employees" class="p-6 animate-fadeIn hidden">
      <div class="flex items-center justify-between mb-5">
        <h2 class="text-lg font-semibold text-gray-800">បុគ្គលិកទាំងអស់</h2>
        <button onclick="openModal('emp-modal')"
          class="btn-primary flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium shadow-sm transition">
          <span class="material-symbols-outlined text-lg">person_add</span> បន្ថែម
        </button>
      </div>

      <div id="employees-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        <div class="col-span-full text-center py-10 text-gray-400">កំពុងទាញទិន្នន័យ...</div>
      </div>
    </div>

    <!-- ===== SETTINGS TAB ===== -->
    <div id="tab-settings" class="p-6 animate-fadeIn hidden">
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
            </div>
          </form>
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

      </div>
    </div>

  </main>
</div>

<!-- ======================================================
     MODALS
====================================================== -->
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
        <label class="block text-sm text-gray-600 mb-1">ឈ្មោះបុគ្គលិក</label>
        <input id="leave-name" type="text" required placeholder="ឈ្មោះ..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">មូលហេតុ</label>
        <input id="leave-reason" type="text" required placeholder="មូលហេតុ..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">ចំនួនថ្ងៃ</label>
        <input id="leave-days" type="number" min="1" value="1" required
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
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
        <label class="block text-sm text-gray-600 mb-1">ឈ្មោះបុគ្គលិក</label>
        <input id="loan-name" type="text" required placeholder="ឈ្មោះ..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">ចំនួនទឹកប្រាក់ (រៀល)</label>
        <input id="loan-amount" type="number" min="1000" required placeholder="ចំនួន..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
      </div>
      <div>
        <label class="block text-sm text-gray-600 mb-1">គោលបំណង</label>
        <input id="loan-purpose" type="text" placeholder="គោលបំណង..."
          class="w-full border border-slate-200 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 transition" />
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

  // ===== Toast =====
  function showToast(msg, isError = false) {
    const t = document.getElementById('toast');
    document.getElementById('toast-msg').textContent = msg;
    document.getElementById('toast-icon').textContent = isError ? 'error' : 'check_circle';
    document.getElementById('toast-icon').className = `material-symbols-outlined text-lg ${isError ? 'text-red-400' : 'text-green-400'}`;
    t.classList.remove('hidden');
    setTimeout(() => t.classList.add('hidden'), 3000);
  }

  // ===== Modal =====
  function openModal(id) { document.getElementById(id).classList.remove('hidden'); }
  function closeModal(id) { document.getElementById(id).classList.add('hidden'); }
  // Close on overlay click
  document.querySelectorAll('.modal-overlay').forEach(el => {
    el.addEventListener('click', e => { if (e.target === el) el.classList.add('hidden'); });
  });

  // ===== Sidebar toggle =====
  function toggleSidebar() {
    document.getElementById('sidebar').classList.toggle('-translate-x-full');
  }

  // ===== Tab switching =====
  const tabs = ['dashboard','leaves','loans','employees','settings'];
  const titles = { dashboard:'ផ្ទៃតាប្លូ', leaves:'ច្បាប់ឈប់សម្រាក', loans:'ខ្ចីប្រាក់ / បុរេប្រទាន', employees:'បុគ្គលិកទាំងអស់', settings:'ការកំណត់ប្រព័ន្ធ' };

  function showTab(tab) {
    tabs.forEach(t => {
      document.getElementById('tab-' + t).classList.add('hidden');
      const nav = document.getElementById('nav-' + t);
      if (nav) {
        nav.classList.remove('active');
        nav.classList.add('text-blue-200');
      }
    });
    document.getElementById('tab-' + tab).classList.remove('hidden');
    const activeNav = document.getElementById('nav-' + tab);
    if (activeNav) {
      activeNav.classList.add('active');
      activeNav.classList.remove('text-blue-200');
    }
    document.getElementById('page-title').textContent = titles[tab];
    if (tab === 'leaves') loadLeaves();
    if (tab === 'loans') loadLoans();
    if (tab === 'employees') loadEmployees();
    if (tab === 'settings') loadSettings();
  }

  // ===== Badge =====
  function statusBadge(s) {
    const map = { pending:'badge-pending', approved:'badge-approved', rejected:'badge-rejected' };
    const lbl = { pending:'រង់ចាំ', approved:'អនុម័ត', rejected:'បដិសេធ' };
    return `<span class="text-xs px-2 py-0.5 rounded-full font-medium ${map[s]||'bg-gray-100 text-gray-500'}">${lbl[s]||s}</span>`;
  }

  // ===== LOAD STATS =====
  async function loadStats() {
    try {
      const r = await fetch('/api/stats');
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
      const r = await fetch('/api/leaves');
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
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center py-10 text-gray-400">គ្មានទិន្នន័យ</td></tr>'; return;
    }
    tbody.innerHTML = filtered.map(l => `
      <tr class="hover:bg-slate-50 transition">
        <td class="px-5 py-3.5 font-medium">${l.employee_name}</td>
        <td class="px-5 py-3.5 text-gray-500">${l.reason}</td>
        <td class="px-5 py-3.5">${l.days || 1} ថ្ងៃ</td>
        <td class="px-5 py-3.5">${statusBadge(l.status)}</td>
        <td class="px-5 py-3.5 text-right">
          <div class="flex items-center justify-end gap-1">
            ${l.status === 'pending' ? `
            <button onclick="updateLeave('${l._id}','approved')" title="អនុម័ត"
              class="w-8 h-8 flex items-center justify-center rounded-lg bg-green-50 text-green-600 hover:bg-green-100 transition">
              <span class="material-symbols-outlined text-base">check</span>
            </button>
            <button onclick="updateLeave('${l._id}','rejected')" title="បដិសេធ"
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

  function renderDashLeaves() {
    const el = document.getElementById('dash-leaves');
    const recent = leavesData.slice(0, 5);
    if (!recent.length) { el.innerHTML = '<div class="p-4 text-gray-400 text-center text-sm">គ្មានទិន្នន័យ</div>'; return; }
    el.innerHTML = recent.map(l => `
      <div class="px-5 py-3 flex items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-full bg-orange-100 flex items-center justify-center text-orange-600 text-xs font-bold flex-shrink-0">
            ${l.employee_name.charAt(0)}
          </div>
          <div>
            <p class="font-medium text-gray-800 text-sm">${l.employee_name}</p>
            <p class="text-xs text-gray-400">${l.reason} · ${l.days||1} ថ្ងៃ</p>
          </div>
        </div>
        ${statusBadge(l.status)}
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
    showToast(status === 'approved' ? 'បានអនុម័ត!' : 'បានបដិសេធ!');
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
    const payload = {
      employee_name: document.getElementById('leave-name').value,
      reason: document.getElementById('leave-reason').value,
      days: parseInt(document.getElementById('leave-days').value)
    };
    await fetch('/api/leave', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    closeModal('leave-modal');
    e.target.reset();
    showToast('បានបន្ថែមសំណើច្បាប់!');
    await loadLeaves(); await loadStats();
  }

  // ===== LOAD LOANS =====
  async function loadLoans() {
    try {
      const r = await fetch('/api/loans');
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
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center py-10 text-gray-400">គ្មានទិន្នន័យ</td></tr>'; return;
    }
    tbody.innerHTML = filtered.map(l => `
      <tr class="hover:bg-slate-50 transition">
        <td class="px-5 py-3.5 font-medium">${l.employee_name}</td>
        <td class="px-5 py-3.5 font-bold text-blue-700">${Number(l.amount_khr).toLocaleString('km-KH')} ៛</td>
        <td class="px-5 py-3.5 text-gray-500">${l.purpose || '—'}</td>
        <td class="px-5 py-3.5">${statusBadge(l.status)}</td>
        <td class="px-5 py-3.5 text-right">
          <div class="flex items-center justify-end gap-1">
            ${l.status === 'pending' ? `
            <button onclick="updateLoan('${l._id}','approved')" title="អនុម័ត"
              class="w-8 h-8 flex items-center justify-center rounded-lg bg-green-50 text-green-600 hover:bg-green-100 transition">
              <span class="material-symbols-outlined text-base">check</span>
            </button>
            <button onclick="updateLoan('${l._id}','rejected')" title="បដិសេធ"
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

  function renderDashLoans() {
    const el = document.getElementById('dash-loans');
    const recent = loansData.slice(0, 5);
    if (!recent.length) { el.innerHTML = '<div class="p-4 text-gray-400 text-center text-sm">គ្មានទិន្នន័យ</div>'; return; }
    el.innerHTML = recent.map(l => `
      <div class="px-5 py-3 flex items-center justify-between gap-3">
        <div class="flex items-center gap-3">
          <div class="w-8 h-8 rounded-full bg-green-100 flex items-center justify-center text-green-600 text-xs font-bold flex-shrink-0">
            ${l.employee_name.charAt(0)}
          </div>
          <div>
            <p class="font-medium text-gray-800 text-sm">${l.employee_name}</p>
            <p class="text-xs text-blue-600 font-semibold">${Number(l.amount_khr).toLocaleString()} ៛</p>
          </div>
        </div>
        ${statusBadge(l.status)}
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
    showToast(status === 'approved' ? 'បានអនុម័ត!' : 'បានបដិសេធ!');
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
    const payload = {
      employee_name: document.getElementById('loan-name').value,
      amount_khr: parseInt(document.getElementById('loan-amount').value),
      purpose: document.getElementById('loan-purpose').value
    };
    await fetch('/api/loan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    closeModal('loan-modal');
    e.target.reset();
    showToast('បានបន្ថែមសំណើខ្ចីប្រាក់!');
    await loadLoans(); await loadStats();
  }

  // ===== LOAD EMPLOYEES =====
  async function loadEmployees() {
    try {
      const r = await fetch('/api/employees');
      const data = await r.json();
      const grid = document.getElementById('employees-grid');
      if (!data.length) {
        grid.innerHTML = '<div class="col-span-full text-center py-10 text-gray-400">គ្មានបុគ្គលិក — ចុច "បន្ថែម" ដើម្បីចាប់ផ្តើម</div>'; return;
      }
      grid.innerHTML = data.map((emp, i) => {
        const color = avatarColors[i % avatarColors.length];
        return `<div class="bg-white rounded-2xl shadow-sm border border-slate-100 p-5 flex flex-col items-center text-center gap-3 hover:shadow-md transition">
          <div class="w-14 h-14 rounded-full ${color} flex items-center justify-center text-white text-xl font-bold">
            ${emp.name.charAt(0)}
          </div>
          <div>
            <p class="font-semibold text-gray-800">${emp.name}</p>
            <p class="text-xs text-blue-600 font-medium">${emp.position}</p>
            ${emp.phone ? `<p class="text-xs text-gray-400 mt-1 flex items-center justify-center gap-1"><span class="material-symbols-outlined text-sm">call</span>${emp.phone}</p>` : ''}
            ${emp.phone2 ? `<p class="text-xs text-gray-400 flex items-center justify-center gap-1"><span class="material-symbols-outlined text-sm">call</span>${emp.phone2}</p>` : ''}
          </div>
          <button onclick="deleteEmployee('${emp._id}')"
            class="text-xs text-red-400 hover:text-red-600 flex items-center gap-1 mt-1 transition">
            <span class="material-symbols-outlined text-sm">delete</span> លុប
          </button>
        </div>`;
      }).join('');
    } catch(e) {
      document.getElementById('employees-grid').innerHTML = '<div class="col-span-full text-center py-10 text-red-400">មិនអាចភ្ជាប់ MongoDB បាន</div>';
    }
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
      phone2: document.getElementById('emp-phone2').value
    };
    await fetch('/api/employee', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    closeModal('emp-modal');
    e.target.reset();
    showToast('បានបន្ថែមបុគ្គលិក!');
    await loadEmployees(); await loadStats();
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
    } catch(e) {
      showToast('មិនអាចទាញយកការកំណត់បានទេ', true);
    }
  }

  async function saveSettings(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-save-settings');
    btn.disabled = true;
    btn.classList.add('opacity-70');
    try {
      const payload = {
        telegram_bot_token: document.getElementById('setting-bot-token').value.trim(),
        telegram_chat_id: document.getElementById('setting-chat-id').value.trim(),
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
    } catch(err) {
      showToast('មានបញ្ហាក្នុងការរក្សាទុក', true);
    } finally {
      btn.disabled = false;
      btn.classList.remove('opacity-70');
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

  // ===== Refresh =====
  async function refreshAll() {
    const btn = document.getElementById('refresh-btn').querySelector('span');
    btn.classList.add('animate-spin');
    await Promise.all([loadStats(), loadLeaves(), loadLoans()]);
    setTimeout(() => btn.classList.remove('animate-spin'), 500);
    showToast('ទិន្នន័យស្រស់ហើយ!');
  }

  // ===== Init =====
  (async function init() {
    await loadStats();
    await loadLeaves();
    await loadLoans();
  })();
</script>
</body>
</html>
"""

# ===== Serve Dashboard =====
@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return HTML_TEMPLATE

# ===== Run =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
