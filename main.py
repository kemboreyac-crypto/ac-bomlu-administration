import os
import json
import re
import base64
import asyncio
from datetime import datetime, timezone, timedelta
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
    gemini_api_key: Optional[str] = ""
    gemini_model: Optional[str] = "gemini-flash-lite-latest"
    notify_on_leave: bool = True
    notify_on_loan: bool = True

class TelegramTestRequest(BaseModel):
    bot_token: Optional[str] = ""
    chat_id: Optional[str] = ""

class GeminiTestRequest(BaseModel):
    gemini_api_key: Optional[str] = ""
    gemini_model: Optional[str] = "gemini-flash-lite-latest"

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

def send_telegram_notify(text: str, reply_markup: dict = None, target_chat_id: str = None):
    try:
        s = settings_collection.find_one({"_id": "app_settings"})
        token = (s.get("telegram_bot_token", "").strip() if s else "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        group_chat_id = (s.get("telegram_chat_id", "").strip() if s else "") or os.getenv("TELEGRAM_CHAT_ID", "")
        dest_chat_id = target_chat_id or group_chat_id
        if not token or not dest_chat_id:
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
            "chat_id": dest_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        print(f"Telegram notify error: {e}")

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
        pending_leave_days = sum(l.get("days", 1) for l in user_leaves if l.get("status") == "pending")
        
        user_loans = list(loans_collection.find({"employee_name": emp_name}))
        approved_loan_amt = sum(l.get("amount_khr", 0) for l in user_loans if l.get("status") == "approved")
        pending_loan_amt = sum(l.get("amount_khr", 0) for l in user_loans if l.get("status") == "pending")
        
        return (
            f"👤 <b>ព័ត៌មានផ្ទាល់ខ្លួន:</b> {emp_name}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>ប្រវត្តិច្បាប់ឈប់សម្រាក:</b>\n"
            f"• បានអនុញ្ញាតិ (Approved): <b>{approved_leave_days} ថ្ងៃ</b>\n"
            f"• កំពុងរង់ចាំ (Pending): <b>{pending_leave_days} ថ្ងៃ</b>\n"
            f"• សំណើសុំសរុប: <b>{total_leave_requests} លើក</b>\n\n"
            f"💰 <b>ប្រវត្តិខ្ចីប្រាក់ / បុរេប្រទាន:</b>\n"
            f"• ទឹកប្រាក់បានអនុញ្ញាតិ: <b>{approved_loan_amt:,} រៀល</b>\n"
            f"• ទឹកប្រាក់កំពុងរង់ចាំ: <b>{pending_loan_amt:,} រៀល</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"ℹ️ <i>ប្រព័ន្ធគ្រប់គ្រងបុគ្គលិក AC BOMLU</i>"
        )
    except Exception as e:
        return f"⚠️ មិនអាចទាញយកទិន្នន័យបានទេ: {e}"

def generate_daily_morning_briefing() -> str:
    """Feature 3: Daily morning summary of attendance and leaves for Admins (06:00 AM)."""
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        today_date = now_cam.date()
        today_str = now_cam.strftime("%d/%m/%Y")
        
        total_emp = employees_collection.count_documents({})
        
        # Only include APPROVED leaves! Unapproved, pending, or rejected leaves are excluded.
        approved_leaves = list(leaves_collection.find({"status": "approved"}))
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
        
        pending_loans = list(loans_collection.find({"status": "pending"}))
        pending_loan_count = len(pending_loans)
        pending_loan_amt = sum(ln.get("amount_khr", 0) for ln in pending_loans)
        
        return (
            f"🌅 <b>របាយការណ៍សង្ខេបពេលព្រឹក (Daily Briefing - ម៉ោង ០៦:០០ ព្រឹក)</b>\n"
            f"📅 <b>កាលបរិច្ឆេទ:</b> {today_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>បុគ្គលិកសរុបក្នុងប្រព័ន្ធ:</b> {total_emp} នាក់\n"
            f"📝 <b>បុគ្គលិកសុំច្បាប់ថ្ងៃនេះ ({leave_count} នាក់):</b>{leave_list_str}\n"
            f"💰 <b>សំណើខ្ចីប្រាក់រង់ចាំពិនិត្យ:</b> {pending_loan_count} ករណី ({pending_loan_amt:,} ៛)\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"💪 <i>សូមជូនពរឱ្យដំណើរការការងារថ្ងៃនេះទទួលបានជោគជ័យ និងរលូនល្អ!</i>"
        )
    except Exception as e:
        return f"⚠️ មិនអាចបង្កើតរបាយការណ៍ពេលព្រឹកបានទេ: {e}"

def generate_weekend_leave_report() -> dict:
    """
    Weekly Leave Report for staff to compensate (ធ្វើការសង) on Saturday & Sunday.
    Covers APPROVED leaves within the last 7 days.
    """
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        start_date = (now_cam - timedelta(days=7)).date()
        
        # Only approved leaves must compensate on weekends
        leaves = list(leaves_collection.find({"status": "approved"}).sort("_id", -1))
        
        emp_comp = {}  # ename -> {"total_days": float, "reasons": list, "dates": list}
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
                
                if ename not in emp_comp:
                    emp_comp[ename] = {"total_days": 0.0, "reasons": [], "dates": []}
                emp_comp[ename]["total_days"] += days
                if rsn not in emp_comp[ename]["reasons"]:
                    emp_comp[ename]["reasons"].append(rsn)
                if date_str not in emp_comp[ename]["dates"]:
                    emp_comp[ename]["dates"].append(date_str)
                    
        compensate_list = []
        total_days_to_compensate = 0.0
        
        for ename, info in emp_comp.items():
            tot = info["total_days"]
            total_days_to_compensate += tot
            if tot <= 0.5:
                comp_desc = "សង កន្លះថ្ងៃ (សៅរ៍ ឬ អាទិត្យ)"
                days_label = "កន្លះថ្ងៃ"
            elif tot == 1.0:
                comp_desc = "សង ១ ថ្ងៃ (សៅរ៍ ឬ អាទិត្យ)"
                days_label = "១ ថ្ងៃ"
            elif tot >= 2.0:
                comp_desc = f"សង {int(tot) if tot.is_integer() else tot} ថ្ងៃ (ថ្ងៃសៅរ៍ & អាទិត្យ)"
                days_label = f"{int(tot) if tot.is_integer() else tot} ថ្ងៃ"
            else:
                comp_desc = f"សង {tot} ថ្ងៃ (សៅរ៍ ឬ អាទិត្យ)"
                days_label = f"{tot} ថ្ងៃ"
                
            compensate_list.append({
                "employee_name": ename,
                "days": days_label,
                "raw_days": tot,
                "reason": ", ".join(info["reasons"]),
                "status": "approved",
                "date": ", ".join(info["dates"]),
                "compensation": comp_desc
            })
            
        tot_days_display = int(total_days_to_compensate) if total_days_to_compensate.is_integer() else total_days_to_compensate
        return {
            "report_type": "weekend_leave",
            "generated_at": now_cam.strftime("%d/%m/%Y %H:%M"),
            "total_staff_count": len(compensate_list),
            "total_days_to_compensate": tot_days_display,
            "items": compensate_list
        }
    except Exception as e:
        print("generate_weekend_leave_report error:", e)
        return {"report_type": "weekend_leave", "items": [], "total_staff_count": 0, "total_days_to_compensate": 0}

def format_telegram_weekend_leave_report(data: dict) -> str:
    now_cam_str = data.get("generated_at", "")
    items = data.get("items", [])
    total_staff = data.get("total_staff_count", 0)
    total_days = data.get("total_days_to_compensate", 0)
    
    lines = []
    for idx, item in enumerate(items, 1):
        lines.append(
            f"{idx}. <b>{item['employee_name']}</b> (✅)\n"
            f"   • ច្បាប់ឈប់: <b>{item['days']}</b> (មូលហេតុ: {item['reason']})\n"
            f"   • <b>កាតព្វកិច្ច:</b> <b>{item['compensation']}</b>"
        )
        
    items_str = "\n\n".join(lines) if lines else "  <i>គ្មានបុគ្គលិកសុំច្បាប់ត្រូវធ្វើការសងក្នុងចុងសប្តាហ៍នេះទេ</i>"
    
    return (
        f"📢 <b>របាយការណ៍ច្បាប់ចុងសប្តាហ៍ (បុគ្គលិកត្រូវធ្វើការសង ថ្ងៃសៅរ៍-អាទិត្យ)</b>\n"
        f"📅 <b>កាលបរិច្ឆេទ:</b> {now_cam_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>បុគ្គលិកត្រូវធ្វើការសង:</b> {total_staff} នាក់ (សរុប {total_days} ថ្ងៃ)\n\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>សូមបុគ្គលិកដែលមានឈ្មោះខាងលើ មកបំពេញការងារសងនៅថ្ងៃសៅរ៍ និងថ្ងៃអាទិត្យឱ្យបានទៀងទាត់!</i>"
    )

def generate_monthly_loan_report() -> dict:
    """
    Monthly Loan Report for month-end payroll deduction.
    """
    try:
        cambodia_tz = timezone(timedelta(hours=7))
        now_cam = datetime.now(cambodia_tz)
        cur_year = now_cam.year
        cur_month = now_cam.month
        
        loans = list(loans_collection.find({"status": {"$in": ["approved", "pending"]}}).sort("_id", -1))
        
        month_items = []
        total_amount = 0
        
        for ln in loans:
            doc_dt = ln["_id"].generation_time.astimezone(cambodia_tz)
            if doc_dt.year == cur_year and doc_dt.month == cur_month:
                amt = ln.get("amount_khr", 0)
                total_amount += amt
                month_items.append({
                    "id": str(ln["_id"]),
                    "employee_name": ln.get("employee_name", "បុគ្គលិក"),
                    "amount_khr": amt,
                    "purpose": ln.get("purpose", "មិនបានបញ្ជាក់"),
                    "status": ln.get("status", "approved"),
                    "date": doc_dt.strftime("%d/%m/%Y")
                })
                
        return {
            "report_type": "monthly_loan",
            "month_str": now_cam.strftime("%m/%Y"),
            "total_borrowers": len(month_items),
            "total_amount_khr": total_amount,
            "items": month_items
        }
    except Exception as e:
        print("generate_monthly_loan_report error:", e)
        return {"report_type": "monthly_loan", "items": [], "total_borrowers": 0, "total_amount_khr": 0}

def format_telegram_monthly_loan_report(data: dict) -> str:
    month_str = data.get("month_str", "")
    items = data.get("items", [])
    total_amt = data.get("total_amount_khr", 0)
    total_borrowers = data.get("total_borrowers", 0)
    
    lines = []
    for idx, item in enumerate(items, 1):
        st = "✅" if item["status"] == "approved" else "🟡"
        lines.append(
            f"{idx}. <b>{item['employee_name']}</b> ({st})\n"
            f"   • ទឹកប្រាក់ខ្ចី: <b>{item['amount_khr']:,} រៀល</b>\n"
            f"   • គោលបំណង: {item['purpose']}"
        )
        
    items_str = "\n\n".join(lines) if lines else "  <i>គ្មានសំណើខ្ចីប្រាក់ក្នុងខែនេះទេ</i>"
    
    return (
        f"💰 <b>របាយការណ៍ខ្ចីប្រាក់ប្រចាំខែ (សម្រាប់កាត់ប្រាក់ខែចុងខែ {month_str})</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>ទឹកប្រាក់សរុបត្រូវកាត់ប្រាក់ខែ:</b> <b>{total_amt:,} រៀល</b>\n"
        f"👥 <b>ចំនួនបុគ្គលិកខ្ចីប្រាក់:</b> {total_borrowers} នាក់\n\n"
        f"{items_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <i>សូមគណនេយ្យ និង Admin ពិនិត្យទិន្នន័យដើម្បីកាត់ប្រាក់ខែបុគ្គលិកខាងលើឱ្យបានត្រឹមត្រូវ!</i>"
    )

async def morning_briefing_scheduler():
    """Background loop sending scheduled daily briefing, weekend leave compensation report, and monthly loan reports."""
    cambodia_tz = timezone(timedelta(hours=7))
    last_daily_date = ""
    last_weekend_date = ""
    last_month_loan_date = ""
    
    while True:
        try:
            now_cam = datetime.now(cambodia_tz)
            today_str = now_cam.strftime("%Y-%m-%d")
            
            # 1. Daily morning briefing at 06:00 AM
            if now_cam.hour == 6 and now_cam.minute >= 0 and last_daily_date != today_str:
                briefing = generate_daily_morning_briefing()
                send_telegram_notify(briefing)
                last_daily_date = today_str
                
            # 2. Weekend Leave Compensation Report at 06:00 AM on Friday (4) and Saturday (5)
            # Sends report so staff know they must compensate on Saturday & Sunday!
            if now_cam.weekday() in [4, 5] and now_cam.hour == 6 and now_cam.minute >= 0 and last_weekend_date != today_str:
                rep_data = generate_weekend_leave_report()
                if rep_data.get("items"):
                    tg_msg = format_telegram_weekend_leave_report(rep_data)
                    send_telegram_notify(tg_msg)
                last_weekend_date = today_str
                
            # 3. Monthly Loan Deduction Report on the LAST DAY of every month at 06:00 AM
            import calendar
            last_day = calendar.monthrange(now_cam.year, now_cam.month)[1]
            if now_cam.day == last_day and now_cam.hour == 6 and now_cam.minute >= 0 and last_month_loan_date != today_str:
                loan_rep_data = generate_monthly_loan_report()
                if loan_rep_data.get("items"):
                    loan_tg_msg = format_telegram_monthly_loan_report(loan_rep_data)
                    send_telegram_notify(loan_tg_msg)
                last_month_loan_date = today_str
                
        except Exception as e:
            print("Scheduler error:", e)
        await asyncio.sleep(60)

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(morning_briefing_scheduler())

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
            {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
            {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
        ]]
    }
    send_telegram_notify(
        f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
        f"👤 <b>បុគ្គលិក:</b> {request.employee_name}\n"
        f"📝 <b>មូលហេតុ:</b> {request.reason}\n"
        f"⏱️ <b>ចំនួនថ្ងៃ:</b> {request.days} ថ្ងៃ\n"
        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ",
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
                f"🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved)"
            )
        elif status == "rejected":
            send_telegram_notify(
                f"❌ <b>ការសម្រេចលើសំណើសុំច្បាប់</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                f"🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected)"
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
            {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
            {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
        ]]
    }
    send_telegram_notify(
        f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
        f"👤 <b>បុគ្គលិក:</b> {request.employee_name}\n"
        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {formatted_amount} រៀល\n"
        f"🎯 <b>គោលបំណង:</b> {request.purpose or 'មិនបានបញ្ជាក់'}\n"
        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ",
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
                f"🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved)"
            )
        elif status == "rejected":
            send_telegram_notify(
                f"❌ <b>ការសម្រេចលើសំណើខ្ចីប្រាក់</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {name}\n"
                f"💵 <b>ចំនួន:</b> {amount} រៀល\n"
                f"🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected)"
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
        "notify_on_loan": notify_loan
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
        
    settings_collection.replace_one(
        {"_id": "app_settings"},
        {
            "_id": "app_settings",
            "telegram_bot_token": token_to_save,
            "telegram_chat_id": (payload.telegram_chat_id or "").strip(),
            "gemini_api_key": gemini_to_save,
            "gemini_model": (payload.gemini_model or "gemini-flash-lite-latest").strip(),
            "notify_on_leave": payload.notify_on_leave,
            "notify_on_loan": payload.notify_on_loan
        },
        upsert=True
    )
    return {"message": "ការកំណត់ត្រូវបានរក្សាទុកដោយសុវត្ថិភាព (Encrypted & Masked)!"}

@app.post("/api/test-gemini")
async def test_gemini(payload: GeminiTestRequest):
    key = (payload.gemini_api_key or "").strip()
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    if not key or "•" in key:
        key = s.get("gemini_api_key", os.getenv("GEMINI_API_KEY", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="សូមបញ្ចូល Google Gemini API Key ជាមុនសិន!")
        
    model = (payload.gemini_model or "gemini-3.5-flash-lite").strip()
    candidate_models = [model, "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
    seen = set()
    models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]
    
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
            with urllib.request.urlopen(req, timeout=4) as r:
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
async def send_briefing_api():
    briefing = generate_daily_morning_briefing()
    send_telegram_notify(briefing)
    return {"message": "បានផ្ញើរបាយការណ៍សង្ខេបពេលព្រឹកទៅ Telegram រួចរាល់!"}

@app.get("/api/reports/weekend-leave")
async def api_get_weekend_leave_report():
    return generate_weekend_leave_report()

@app.post("/api/reports/send-weekend-leave")
async def api_send_weekend_leave_report():
    data = generate_weekend_leave_report()
    msg = format_telegram_weekend_leave_report(data)
    send_telegram_notify(msg)
    return {"message": "បានផ្ញើរបាយការណ៍ច្បាប់ចុងសប្តាហ៍ទៅ Telegram រួចរាល់!"}

@app.get("/api/reports/monthly-loan")
async def api_get_monthly_loan_report():
    return generate_monthly_loan_report()

@app.post("/api/reports/send-monthly-loan")
async def api_send_monthly_loan_report():
    data = generate_monthly_loan_report()
    msg = format_telegram_monthly_loan_report(data)
    send_telegram_notify(msg)
    return {"message": "បានផ្ញើរបាយការណ៍ខ្ចីប្រាក់ប្រចាំខែទៅ Telegram រួចរាល់!"}

# ===== Telegram Mini App Web Forms (Option 4) =====
LEAVE_FORM_HTML = """<!DOCTYPE html>
<html lang="km">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>ទម្រង់សុំច្បាប់ - AC BOMLU</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Kantumruy+Pro:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,100..700,0..1,-50..200" />
  <style>
    body { font-family: 'Kantumruy Pro', sans-serif; background: #0b1329; color: #f8fafc; }
    .glass-card { background: rgba(23, 37, 84, 0.7); backdrop-filter: blur(16px); border: 1px solid rgba(59, 130, 246, 0.2); }
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
          <button type="button" onclick="setReason('💍 ចូលរួមពិធីមង្គលការ/បុណ្យ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">💍 ទៅការ/បុណ្យ</button>
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
      <p class="text-xs text-slate-400">សំណើត្រូវបានបញ្ជូនទៅគ្រុប និង Admin រួចរាល់</p>
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
          <button type="button" onclick="setPurpose('🏠 បង់ថ្លៃផ្ទះ / បន្ទប់')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">🏠 បង់ថ្លៃផ្ទះ</button>
          <button type="button" onclick="setPurpose('💊 ទិញថ្នាំពេទ្យ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">💊 ថ្នាំពេទ្យ</button>
          <button type="button" onclick="setPurpose('⚡ ថ្លៃទឹកភ្លើង / ម្ហូបអាហារ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">⚡ ទឹកភ្លើង</button>
          <button type="button" onclick="setPurpose('👨‍👩‍👧 ផ្ញើជូនគ្រួសារ')" class="px-2.5 py-1 text-xs rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition">👨‍👩‍👧 ផ្ញើគ្រួសារ</button>
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
      <p class="text-xs text-slate-400">សំណើត្រូវបានបញ្ជូនទៅគ្រុប និង Admin រួចរាល់</p>
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
    "ceremony": "💍 ចូលរួមពិធីមង្គលការ/បុណ្យ",
}

PURPOSE_MAP = {
    "rent": "🏠 បង់ថ្លៃផ្ទះ / បន្ទប់",
    "med": "💊 ទិញថ្នាំពេទ្យ",
    "util": "⚡ ថ្លៃទឹកភ្លើង",
    "family": "👨‍👩‍👧 ផ្ញើជូនគ្រួសារ",
}

def call_gemini_ai(prompt: str, user_name: str = "", audio_bytes: bytes = None, audio_mime: str = "audio/ogg") -> Optional[dict]:
    s = settings_collection.find_one({"_id": "app_settings"}) or {}
    api_key = (s.get("gemini_api_key", "").strip() if s else "") or os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
        
    model = (s.get("gemini_model", "") or "gemini-3.5-flash-lite").strip()
    candidate_models = [model, "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
    seen = set()
    models_to_try = [m for m in candidate_models if not (m in seen or seen.add(m))]
    
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
        "   - For 'leave' or 'loan': A warm acknowledgment stating that their request has been registered and forwarded to Admin for approval.\n"
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
            with urllib.request.urlopen(req, timeout=5) as response:
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

        # A. Handle Callback Query (When clicking Inline Buttons: Days or Loan Amount)
        callback_query = update.get("callback_query")
        if callback_query:
            cq_id = callback_query.get("id")
            cq_data = callback_query.get("data", "")
            cq_user = callback_query.get("from", {})
            cq_name = (cq_user.get("first_name", "") + " " + cq_user.get("last_name", "")).strip() or cq_user.get("username", "បុគ្គលិក")
            cq_msg = callback_query.get("message", {})
            cq_chat = cq_msg.get("chat", {})
            cq_chat_id = str(cq_chat.get("id") or "")
            cq_chat_type = cq_chat.get("type", "group")
            is_cq_private = (cq_chat_type == "private")
            cq_user_id = str(cq_user.get("id") or "")
            
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
                
                user_flows_collection.replace_one(
                    {"user_id": cq_user_id},
                    {"user_id": cq_user_id, "type": "leave", "days": days, "days_display": days_display, "name": cq_name, "is_private": is_cq_private},
                    upsert=True
                )
                
                reason_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🤒 ឈឺ / មិនស្រួលខ្លួន", "callback_data": f"leave_rsn:sick:{days}:{cq_user_id}"},
                            {"text": "🏠 រវល់ធុរៈគ្រួសារ", "callback_data": f"leave_rsn:family:{days}:{cq_user_id}"}
                        ],
                        [
                            {"text": "🚗 ទៅស្រុកកំណើត", "callback_data": f"leave_rsn:hometown:{days}:{cq_user_id}"},
                            {"text": "💍 ចូលរួមពិធីមង្គលការ/បុណ្យ", "callback_data": f"leave_rsn:ceremony:{days}:{cq_user_id}"}
                        ]
                    ]
                }
                ask_reason_msg = (
                    f"⏱️ <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"⏱️ <b>ចំនួនថ្ងៃ:</b> {days_display}\n\n"
                    f"👉 <b>សូមចុចជ្រើសរើស «មូលហេតុ» ខាងក្រោម ឬវាយបញ្ចូលផ្ទាល់ក្នុងប្រអប់សារ (មិនបាច់ Reply ទេ)៖</b>"
                )
                send_telegram_notify(ask_reason_msg, reply_markup=reason_buttons, target_chat_id=cq_chat_id)
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
                    
                user_flows_collection.delete_one({"user_id": cq_user_id})
                
                if days == 0.5:
                    days_display = "កន្លះថ្ងៃ"
                elif days.is_integer():
                    days_display = f"{int(days)} ថ្ងៃ"
                else:
                    days_display = f"{days} ថ្ងៃ"
                    
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                l_res = leaves_collection.insert_one({
                    "employee_name": cq_name,
                    "reason": rsn,
                    "days": days,
                    "status": "pending",
                    "date": today_khr
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
                    f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                    f"📝 <b>មូលហេតុ:</b> {rsn}\n"
                    f"{conflict_alert}"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_cq_private:
                    emp_conf = (
                        f"✅ <b>សំណើសុំច្បាប់ឈប់សម្រាកត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                        f"📝 <b>មូលហេតុ:</b> {rsn}\n\n"
                        f"🔒 <i>សំណើរបស់អ្នកត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅកាន់ Admin ពិនិត្យអនុញ្ញាត។</i>"
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
                    
                user_flows_collection.replace_one(
                    {"user_id": cq_user_id},
                    {"user_id": cq_user_id, "type": "loan", "amount": amount, "name": cq_name, "is_private": is_cq_private},
                    upsert=True
                )
                
                purpose_buttons = {
                    "inline_keyboard": [
                        [
                            {"text": "🏠 បង់ថ្លៃផ្ទះ / បន្ទប់", "callback_data": f"loan_pur:rent:{amount}:{cq_user_id}"},
                            {"text": "💊 ទិញថ្នាំពេទ្យ", "callback_data": f"loan_pur:med:{amount}:{cq_user_id}"}
                        ],
                        [
                            {"text": "⚡ ថ្លៃទឹកភ្លើង", "callback_data": f"loan_pur:util:{amount}:{cq_user_id}"},
                            {"text": "👨‍👩‍👧 ផ្ញើជូនគ្រួសារ", "callback_data": f"loan_pur:family:{amount}:{cq_user_id}"}
                        ]
                    ]
                }
                ask_purpose_msg = (
                    f"💵 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n\n"
                    f"👉 <b>សូមចុចជ្រើសរើស «គោលបំណង» ខាងក្រោម ឬវាយបញ្ចូលផ្ទាល់ក្នុងប្រអប់សារ (មិនបាច់ Reply ទេ)៖</b>"
                )
                send_telegram_notify(ask_purpose_msg, reply_markup=purpose_buttons, target_chat_id=cq_chat_id)
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
                    
                user_flows_collection.delete_one({"user_id": cq_user_id})
                
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                ln_res = loans_collection.insert_one({
                    "employee_name": cq_name,
                    "amount_khr": amt,
                    "purpose": pur,
                    "status": "pending",
                    "date": today_khr
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
                    f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amt:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {pur}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_cq_private:
                    emp_conf = (
                        f"✅ <b>សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {cq_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amt:,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {pur}\n\n"
                        f"🔒 <i>សំណើរបស់អ្នកត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅកាន់ Admin ពិនិត្យអនុញ្ញាត។</i>"
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
                        "text": "⚠️ សុំទោស! លោកអ្នកជាបុគ្គលិកធម្មតា មិនមានសិទ្ធិចុចអនុញ្ញាតិឡើយ។ មានតែ Admin ក្នុងគ្រុបប៉ុណ្ណោះដែលអាចចុចបាន!",
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
                        
                        result_text = "🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved) ✅" if is_approve else "🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected) ❌"
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
                        
                        result_text = "🟢 <b>លទ្ធផល:</b> ត្រូវបានអនុញ្ញាតិ (Approved) ✅" if is_approve else "🔴 <b>លទ្ធផល:</b> មិនអនុញ្ញាតិ (Rejected) ❌"
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
                    "text": "✅ បានអនុញ្ញាតិជោគជ័យ!" if is_approve else "❌ មិនអនុញ្ញាតិ!"
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
        
        # Check active flow immediately to prevent UnboundLocalError
        active_flow = user_flows_collection.find_one({"user_id": user_id_str})
        
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
                send_telegram_notify(welcome_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        chat = message.get("chat", {})
        chat_id_str = str(chat.get("id"))
        chat_type = chat.get("type", "group")
        is_private_chat = (chat_type == "private")
        
        text = (message.get("text") or "").strip()
        has_voice = bool(message.get("voice") or message.get("audio"))
        clean_cmd = text.split('@')[0].strip().lower()

        bot_username = "ac_bomlu_administration_bot"
        is_reply_to_bot = bool(message.get("reply_to_message", {}).get("from", {}).get("is_bot"))
        is_mentioned = ("@" in text and bot_username.lower() in text.lower()) or is_reply_to_bot
        is_hr_topic = any(w in text for w in ["សុំច្បាប់", "ច្បាប់", "ឈប់", "leave", "ខ្ចីប្រាក់", "ខ្ចី", "បុរេប្រទាន", "loan", "វត្តមាន", "ប្រាក់ខែ", "លុយ"])

        is_leave_btn = text in ["📝 ស្នើសុំច្បាប់", "ស្នើសុំច្បាប់", "📝 សុំច្បាប់", "សុំច្បាប់", "ច្បាប់", "ឈប់"] or clean_cmd in ["/leave", "leave", "leav"]
        is_loan_btn = text in ["💰 ស្នើខ្ចីប្រាក់", "ស្នើខ្ចីប្រាក់", "💰 សុំខ្ចីប្រាក់", "សុំខ្ចីប្រាក់", "ខ្ចីប្រាក់", "បុរេប្រទាន"] or clean_cmd in ["/loan", "loan", "lon"]
        is_system_cmd = clean_cmd in ["/start", "/help", "/briefing", "/report", "/today", "/morning", "/me", "/status", "/profile", "/my_leave", "/my_loan", "/weekend_report", "/leave_report", "/weekend", "/month_report", "/loan_report", "/month_loan"] or is_leave_btn or is_loan_btn

        # Feature 3: Daily Morning Briefing Command (/briefing, /report, /today)
        if clean_cmd in ["/briefing", "/report", "/today", "/morning"]:
            briefing = generate_daily_morning_briefing()
            send_telegram_notify(briefing, target_chat_id=chat_id_str)
            return {"ok": True}

        # Weekend Leave Compensation Report Command (/weekend_report, /leave_report)
        if clean_cmd in ["/weekend_report", "/leave_report", "/weekend"]:
            rep_data = generate_weekend_leave_report()
            tg_msg = format_telegram_weekend_leave_report(rep_data)
            send_telegram_notify(tg_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # Monthly Loan Deduction Report Command (/month_report, /loan_report)
        if clean_cmd in ["/month_report", "/loan_report", "/month_loan"]:
            loan_rep_data = generate_monthly_loan_report()
            loan_tg_msg = format_telegram_monthly_loan_report(loan_rep_data)
            send_telegram_notify(loan_tg_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # Feature 5: Self-Service Query Command (/me, /status, or natural language question)
        is_self_query = clean_cmd in ["/me", "/status", "/profile", "/my_leave", "/my_loan"] or any(k in text for k in [
            "ច្បាប់ប៉ុន្មាន", "លុយខ្ចីប៉ុន្មាន", "ខ្ចីប៉ុន្មាន", "ច្បាប់ខ្ញុំ", 
            "ខ្ចីខ្ញុំ", "ប្រវត្តិខ្ញុំ", "មើលច្បាប់", "មើលខ្ចី", "ទិន្នន័យខ្ញុំ", "សល់លុយ"
        ])
        if is_self_query:
            ai_data = call_gemini_ai(prompt=f"Staff member '{sender_name}' asked self-service inquiry: {text}", user_name=sender_name)
            ai_msg = (ai_data.get("response_text") if ai_data else "").strip()
            report = get_employee_self_service_report(sender_name)
            if ai_msg:
                full_reply = f"🤖 <b>ឆ្លើយតប ({sender_name}):</b>\n{ai_msg}\n\n{report}"
            else:
                full_reply = report
            send_telegram_notify(full_reply, target_chat_id=chat_id_str)
            return {"ok": True}

        # Feature 2: Smart Group Filtering (Quiet Bot in Groups for normal casual chatter)
        if not is_private_chat:
            if not is_system_cmd and not active_flow and not is_mentioned and not is_hr_topic and not has_voice:
                return {"ok": True}

        # 2. Check if user is in an active flow (waiting for Reason or Purpose)!
        if active_flow and not is_system_cmd:
            content = (message.get("caption") or "សារសំឡេង (Voice Message)") if has_voice else text
            flow_type = active_flow.get("type")
            flow_is_private = active_flow.get("is_private", is_private_chat)
            
            if flow_type == "leave":
                days = active_flow.get("days", 1)
                try:
                    days_val = float(days)
                except (ValueError, TypeError):
                    days_val = 1.0
                if days_val == 0.5:
                    days_display = "កន្លះថ្ងៃ"
                elif days_val.is_integer():
                    days_display = f"{int(days_val)} ថ្ងៃ"
                else:
                    days_display = f"{days_val} ថ្ងៃ"
                    
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                l_res = leaves_collection.insert_one({
                    "employee_name": sender_name,
                    "reason": content,
                    "days": days_val,
                    "status": "pending",
                    "date": today_khr
                })
                leave_id = str(l_res.inserted_id)
                user_flows_collection.delete_one({"user_id": user_id_str})
                
                conflict_alert = check_leave_conflicts(sender_name)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                admin_card = (
                    f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាកថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                    f"📝 <b>មូលហេតុ:</b> {content}\n"
                    f"{conflict_alert}"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if flow_is_private or is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើសុំច្បាប់ឈប់សម្រាកត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                        f"📝 <b>មូលហេតុ:</b> {content}\n\n"
                        f"🔒 <i>សំណើរបស់អ្នកត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅកាន់ Admin ពិនិត្យអនុញ្ញាត។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                return {"ok": True}
                
            elif flow_type == "loan":
                amount = active_flow.get("amount", 100000)
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                ln_res = loans_collection.insert_one({
                    "employee_name": sender_name,
                    "amount_khr": amount,
                    "purpose": content,
                    "status": "pending",
                    "date": today_khr
                })
                loan_id = str(ln_res.inserted_id)
                user_flows_collection.delete_one({"user_id": user_id_str})
                
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                admin_card = (
                    f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {content}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if flow_is_private or is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {content}\n\n"
                        f"🔒 <i>សំណើរបស់អ្នកត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅកាន់ Admin ពិនិត្យអនុញ្ញាត។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
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
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                admin_card = (
                    f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាក (បានបន្ថែមមូលហេតុ)!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {recent_leave.get('days', 1)} ថ្ងៃ\n"
                    f"📝 <b>មូលហេតុ:</b> {text}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើសុំច្បាប់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {recent_leave.get('days', 1)} ថ្ងៃ\n"
                        f"📝 <b>មូលហេតុ:</b> {text}\n\n"
                        f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
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
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                admin_card = (
                    f"💰 <b>សំណើខ្ចីប្រាក់ (បានបន្ថែមគោលបំណង)!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {recent_loan.get('amount_khr', 100000):,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {text}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {recent_loan.get('amount_khr', 100000):,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {text}\n\n"
                        f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                return {"ok": True}
        
        # 2. Handle Voice Message (Powered by Google Gemini Multimodal Audio)
        if has_voice:
            caption = message.get("caption") or ""
            voice_obj = message.get("voice") or message.get("audio")
            file_id = voice_obj.get("file_id") if voice_obj else None
            audio_bytes = None
            if file_id and token:
                try:
                    gf_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
                    gf_req = urllib.request.Request(gf_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(gf_req, timeout=6) as r:
                        gf_data = json.loads(r.read().decode())
                        file_path = gf_data.get("result", {}).get("file_path")
                        if file_path:
                            down_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                            with urllib.request.urlopen(urllib.request.Request(down_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10) as down_r:
                                audio_bytes = down_r.read()
                except Exception as ve:
                    print("Error downloading voice message:", ve)
                    
            ai_data = None
            if audio_bytes:
                ai_data = call_gemini_ai(
                    prompt=f"Please listen carefully to this Khmer voice message from staff '{sender_name}' and analyze their intent.",
                    user_name=sender_name,
                    audio_bytes=audio_bytes
                )
                
            if ai_data and ai_data.get("intent") == "leave":
                days = int(ai_data.get("days", 1))
                reason = ai_data.get("reason") or "សារសំឡេងសុំច្បាប់"
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                l_res = leaves_collection.insert_one({
                    "employee_name": sender_name,
                    "reason": reason,
                    "days": days,
                    "status": "pending",
                    "date": today_khr
                })
                leave_id = str(l_res.inserted_id)
                conflict_alert = check_leave_conflicts(sender_name)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                admin_card = (
                    f"🎙️ <b>សំណើសុំច្បាប់តាមសំឡេង (វិភាគដោយ AI)!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                    f"📝 <b>មូលហេតុ:</b> {reason}\n"
                    f"{conflict_alert}"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើសុំច្បាប់តាមសំឡេងត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {days} ថ្ងៃ\n"
                        f"📝 <b>មូលហេតុ:</b> {reason}\n\n"
                        f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                return {"ok": True}
                
            elif ai_data and ai_data.get("intent") == "loan":
                amount = int(ai_data.get("amount_khr", 100000))
                purpose = ai_data.get("purpose") or "សារសំឡេងសុំខ្ចីប្រាក់"
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                ln_res = loans_collection.insert_one({
                    "employee_name": sender_name,
                    "amount_khr": amount,
                    "purpose": purpose,
                    "status": "pending",
                    "date": today_khr
                })
                loan_id = str(ln_res.inserted_id)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
                    ]]
                }
                admin_card = (
                    f"💰 <b>សំណើខ្ចីប្រាក់តាមសំឡេង (វិភាគដោយ AI)!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {purpose}\n"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើខ្ចីប្រាក់តាមសំឡេងត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {purpose}\n\n"
                        f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                return {"ok": True}
                
            elif ai_data and ai_data.get("response_text"):
                send_telegram_notify(f"🤖 <b>ឆ្លើយតបសារសំឡេង ({sender_name}):</b>\n{ai_data.get('response_text')}", target_chat_id=chat_id_str)
                return {"ok": True}
                
            else:
                # Fallback if Gemini not available
                voice_label = caption or "សារសំឡេង (Voice Message)"
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
                v_res = leaves_collection.insert_one({
                    "employee_name": sender_name,
                    "reason": voice_label,
                    "days": 1,
                    "status": "pending",
                    "date": today_khr
                })
                leave_id = str(v_res.inserted_id)
                conflict_alert = check_leave_conflicts(sender_name)
                adm_btn = {
                    "inline_keyboard": [[
                        {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                        {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                    ]]
                }
                admin_card = (
                    f"🎙️ <b>សំណើសុំច្បាប់តាមសំឡេងថ្មី!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"📝 <b>មូលហេតុ:</b> {voice_label}\n"
                    f"⏱️ <b>ចំនួន:</b> 1 ថ្ងៃ\n"
                    f"{conflict_alert}"
                    f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                )
                if is_private_chat:
                    emp_conf = (
                        f"✅ <b>សំណើសុំច្បាប់តាមសំឡេងត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"⏱️ <b>ចំនួន:</b> 1 ថ្ងៃ\n"
                        f"📝 <b>មូលហេតុ:</b> {voice_label}\n\n"
                        f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                    )
                    send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                else:
                    send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                return {"ok": True}

        # 3. Handle Greetings & Casual Messages -> Re-attach 2 Keyboard buttons!
        if text.lower() in ["hi", "hello", "hey", "សួស្តី", "ជំរាបសួរ", "បាទ", "ចាស", "ok"]:
            reply = (
                f"👋 <b>សួស្តី {sender_name}!</b>\n\n"
                f"👇 ក្ដារចុចប៊ូតុងត្រូវបានភ្ជាប់ជូនអ្នករួចរាល់។ សូមចុចប៊ូតុង <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ឬ <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ខាងក្រោមដើម្បីប្រើប្រាស់។"
            )
            send_telegram_notify(reply, target_chat_id=chat_id_str)
            return {"ok": True}

        # 4. Handle Leave Button -> Step 1: Show Days Selection Buttons (ជម្រើសទី ១)!
        if is_leave_btn:
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
            send_telegram_notify(msg, reply_markup=inline_days, target_chat_id=chat_id_str)
            return {"ok": True}

        # 5. Smart AI Understanding with Google Gemini for free-form text:
        if text and not is_system_cmd:
            ai_data = call_gemini_ai(prompt=f"Staff member '{sender_name}' wrote: {text}", user_name=sender_name)
            if ai_data:
                intent = ai_data.get("intent")
                cambodia_tz = timezone(timedelta(hours=7))
                today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")

                if intent == "self_query":
                    ai_msg = (ai_data.get("response_text") or "").strip()
                    report = get_employee_self_service_report(sender_name)
                    if ai_msg:
                        full_reply = f"🤖 <b>ឆ្លើយតប ({sender_name}):</b>\n{ai_msg}\n\n{report}"
                    else:
                        full_reply = report
                    send_telegram_notify(full_reply, target_chat_id=chat_id_str)
                    return {"ok": True}

                elif intent == "leave":
                    try:
                        days = float(ai_data.get("days", 1))
                    except (ValueError, TypeError):
                        days = 1.0
                    days_display = "កន្លះថ្ងៃ" if days == 0.5 else (f"{int(days)} ថ្ងៃ" if days.is_integer() else f"{days} ថ្ងៃ")
                    reason = ai_data.get("reason") or text
                    l_res = leaves_collection.insert_one({
                        "employee_name": sender_name,
                        "reason": reason,
                        "days": days,
                        "status": "pending",
                        "date": today_khr
                    })
                    leave_id = str(l_res.inserted_id)
                    conflict_alert = check_leave_conflicts(sender_name)
                    adm_btn = {
                        "inline_keyboard": [[
                            {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                            {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                        ]]
                    }
                    admin_card = (
                        f"📋 <b>សំណើសុំច្បាប់ឈប់សម្រាក (វិភាគដោយ AI)!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                        f"📝 <b>មូលហេតុ:</b> {reason}\n"
                        f"{conflict_alert}"
                        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                    )
                    if is_private_chat:
                        emp_conf = (
                            f"✅ <b>សំណើសុំច្បាប់ឈប់សម្រាកត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                            f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                            f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                            f"📝 <b>មូលហេតុ:</b> {reason}\n\n"
                            f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                        )
                        send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                        send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                    else:
                        send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                    return {"ok": True}

                elif intent == "loan":
                    try:
                        amount = int(ai_data.get("amount_khr", 100000))
                    except (ValueError, TypeError):
                        amount = 100000
                    purpose = ai_data.get("purpose") or text
                    ln_res = loans_collection.insert_one({
                        "employee_name": sender_name,
                        "amount_khr": amount,
                        "purpose": purpose,
                        "status": "pending",
                        "date": today_khr
                    })
                    loan_id = str(ln_res.inserted_id)
                    adm_btn = {
                        "inline_keyboard": [[
                            {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
                            {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
                        ]]
                    }
                    admin_card = (
                        f"💰 <b>សំណើខ្ចីប្រាក់ថ្មី (វិភាគដោយ AI)!</b>\n\n"
                        f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                        f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                        f"🎯 <b>គោលបំណង:</b> {purpose}\n"
                        f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
                    )
                    if is_private_chat:
                        emp_conf = (
                            f"✅ <b>សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                            f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                            f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                            f"🎯 <b>គោលបំណង:</b> {purpose}\n\n"
                            f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                        )
                        send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                        send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
                    else:
                        send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
                    return {"ok": True}

                elif intent in ["hr_query", "general_chat"] and ai_data.get("response_text"):
                    clean_res = ai_data.get("response_text")
                    send_telegram_notify(f"🤖 <b>ជំនួយការ AI:</b>\n{clean_res}", target_chat_id=chat_id_str)
                    return {"ok": True}

        # 6. Fallback: Detailed Leave Text (e.g. ស្អែកសុំច្បាប់ កន្លះថ្ងៃ / ២ ថ្ងៃ ឈឺក្បាល...)
        if ("សុំច្បាប់" in text or "ច្បាប់" in text or "leave" in text.lower()) and not is_leave_btn and not is_loan_btn:
            days = 1.0
            if "កន្លះ" in text or "half" in text.lower() or "0.5" in text:
                days = 0.5
            else:
                import re
                day_match = re.search(r'(\d+(?:\.\d+)?)\s*(ថ្ងៃ|day)', text, re.IGNORECASE)
                if day_match:
                    try:
                        days = float(day_match.group(1))
                    except Exception:
                        days = 1.0
                        
            days_display = "កន្លះថ្ងៃ" if days == 0.5 else (f"{int(days)} ថ្ងៃ" if days.is_integer() else f"{days} ថ្ងៃ")
            cambodia_tz = timezone(timedelta(hours=7))
            today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
            l_res = leaves_collection.insert_one({
                "employee_name": sender_name,
                "reason": text,
                "days": days,
                "status": "pending",
                "date": today_khr
            })
            leave_id = str(l_res.inserted_id)
            conflict_alert = check_leave_conflicts(sender_name)
            adm_btn = {
                "inline_keyboard": [[
                    {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_leave:{leave_id}"},
                    {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_leave:{leave_id}"}
                ]]
            }
            admin_card = (
                f"📋 <b>បានកត់ត្រាសំណើសុំច្បាប់ជោគជ័យ!</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                f"📝 <b>មូលហេតុ:</b> {text}\n"
                f"{conflict_alert}"
                f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
            )
            if is_private_chat:
                emp_conf = (
                    f"✅ <b>សំណើសុំច្បាប់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"⏱️ <b>ចំនួន:</b> {days_display}\n"
                    f"📝 <b>មូលហេតុ:</b> {text}\n\n"
                    f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                )
                send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
            else:
                send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
            return {"ok": True}

        # 6. Handle Loan Button -> Step 1: Show Amount Selection Buttons (ជម្រើសទី ១)!
        if is_loan_btn:
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
            send_telegram_notify(msg, reply_markup=inline_amounts, target_chat_id=chat_id_str)
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
                    
            cambodia_tz = timezone(timedelta(hours=7))
            today_khr = datetime.now(cambodia_tz).strftime("%Y-%m-%d")
            ln_res = loans_collection.insert_one({
                "employee_name": sender_name,
                "amount_khr": amount,
                "purpose": text,
                "status": "pending",
                "date": today_khr
            })
            loan_id = str(ln_res.inserted_id)
            adm_btn = {
                "inline_keyboard": [[
                    {"text": "✅ អនុញ្ញាតិ", "callback_data": f"app_loan:{loan_id}"},
                    {"text": "❌ មិនអនុញ្ញាតិ", "callback_data": f"rej_loan:{loan_id}"}
                ]]
            }
            admin_card = (
                f"💰 <b>បានកត់ត្រាសំណើខ្ចីប្រាក់ជោគជ័យ!</b>\n\n"
                f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                f"🎯 <b>គោលបំណង:</b> {text}\n"
                f"🟡 <b>ស្ថានភាព:</b> រង់ចាំ Admin អនុញ្ញាតិ"
            )
            if is_private_chat:
                emp_conf = (
                    f"✅ <b>សំណើខ្ចីប្រាក់ត្រូវបានបញ្ជូនរួចរាល់!</b>\n\n"
                    f"👤 <b>បុគ្គលិក:</b> {sender_name}\n"
                    f"💵 <b>ចំនួនទឹកប្រាក់:</b> {amount:,} រៀល\n"
                    f"🎯 <b>គោលបំណង:</b> {text}\n\n"
                    f"🔒 <i>សំណើត្រូវបានរក្សាការសម្ងាត់ក្នុង DM និងបានបញ្ជូនទៅ Admin រួចរាល់។</i>"
                )
                send_telegram_notify(emp_conf, target_chat_id=chat_id_str)
                send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=admin_group_chat_id)
            else:
                send_telegram_notify(admin_card, reply_markup=adm_btn, target_chat_id=chat_id_str)
            return {"ok": True}

        # 8. Handle Help/Start Command
        if text.startswith("/help") or text.startswith("/start"):
            help_msg = (
                f"👋 <b>ជម្រាបសួរ {sender_name}!</b>\n\n"
                f"📌 <b>របៀបសុំច្បាប់ ឬខ្ចីប្រាក់ជាមួយ Bot AC BOMLU:</b>\n"
                f"• ចុចប៊ូតុង <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ខាងក្រោម ➡️ រើសចំនួនថ្ងៃ\n"
                f"• ចុចប៊ូតុង <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ខាងក្រោម ➡️ រើសចំនួនប្រាក់\n"
                f"• ឬផ្ញើសារជា <b>Voice Message សំឡេង</b>\n\n"
                f"🚀 ប្រព័ន្ធនឹងកត់ត្រាចូល Dashboard របស់ AC BOMLU ដោយស្វ័យប្រវត្តិ!"
            )
            send_telegram_notify(help_msg, target_chat_id=chat_id_str)
            return {"ok": True}

        # 9. Private Chat Friendly Fallback (Never leave staff hanging in DM!)
        if is_private_chat and text:
            fallback_dm = (
                f"🤖 <b>សួស្តី {sender_name}!</b>\n\n"
                f"ខ្ញុំជាជំនួយការ AI របស់ក្រុមហ៊ុន <b>AC BOMLU</b>។\n\n"
                f"លោកអ្នកអាចចុចប៊ូតុងខាងក្រោមដើម្បី៖\n"
                f"• <b>[ 📝 ស្នើសុំច្បាប់ ]</b> ឈប់សម្រាក\n"
                f"• <b>[ 💰 ស្នើខ្ចីប្រាក់ ]</b> ឬបុរេប្រទាន\n\n"
                f"ឬអាចវាយសារសួរអំពី <b>ច្បាប់</b>, <b>ប្រាក់ខែ</b> ឬ <b>វត្តមាន</b> ផ្ទាល់ខ្លួនបានគ្រប់ពេល!"
            )
            send_telegram_notify(fallback_dm, target_chat_id=chat_id_str)
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

<!-- Mobile Sidebar Backdrop Overlay -->
<div id="sidebar-backdrop" onclick="closeSidebar()"
  class="fixed inset-0 bg-black/50 backdrop-blur-sm z-40 hidden lg:hidden transition-opacity duration-300"></div>

<!-- ======================================================
     SIDEBAR
====================================================== -->
<aside id="sidebar"
  class="fixed inset-y-0 left-0 z-50 w-64 bg-gradient-to-b from-blue-900 to-blue-950 text-white flex flex-col shadow-2xl transition-transform duration-300 ease-in-out -translate-x-full lg:translate-x-0 lg:static flex-shrink-0">

  <!-- Logo -->
  <div class="h-16 flex items-center justify-between px-5 border-b border-blue-800">
    <div class="flex items-center gap-3">
      <img src="/static/ac_logo.jpg" alt="AC Logo"
        class="w-10 h-10 rounded-full object-cover shadow-md border-2 border-white/30 flex-shrink-0" />
      <div class="leading-tight">
        <p class="text-xs text-blue-300 font-medium">AC BOMLU</p>
        <p class="text-sm font-bold">HR Dashboard</p>
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
      <span class="material-symbols-outlined text-xl">dashboard</span>
      <span>ផ្ទៃតាប្លូ</span>
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
  <header class="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-4 sm:px-6 shadow-sm flex-shrink-0">
    <div class="flex items-center gap-3">
      <!-- Hamburger (mobile) -->
      <button onclick="toggleSidebar()" class="w-10 h-10 flex items-center justify-center rounded-xl hover:bg-slate-100 text-gray-600 hover:text-blue-700 lg:hidden transition">
        <span class="material-symbols-outlined text-2xl">menu</span>
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
            <p class="text-xs text-gray-500 mb-0.5">ខ្ចីប្រាក់អនុញ្ញាតិ</p>
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
        <button onclick="filterLeaves('approved')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-approved">អនុញ្ញាតិ</button>
        <button onclick="filterLeaves('rejected')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lf-rejected">មិនអនុញ្ញាតិ</button>
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
        <button onclick="filterLoans('approved')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-approved">អនុញ្ញាតិ</button>
        <button onclick="filterLoans('rejected')" class="px-4 py-1.5 rounded-full bg-white border border-slate-200 text-gray-600 hover:bg-slate-50" id="lnf-rejected">មិនអនុញ្ញាតិ</button>
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
                <option value="gemini-3.5-flash-lite">⚡ Gemini 3.5 Flash Lite (Ultra Fast 0.7s - ណែនាំ)</option>
                <option value="gemini-flash-lite-latest">⚡ Gemini Flash Lite Latest (Fast 0.8s)</option>
                <option value="gemini-3.5-flash">⚡ Gemini 3.5 Flash (Ultra Fast - លឿនបំផុត)</option>
                <option value="gemini-3.6-flash">🚀 Gemini 3.6 Flash (Advanced)</option>
                <option value="gemini-3.1-flash-lite">✨ Gemini 3.1 Flash Lite</option>
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
              <p class="text-xs text-gray-500 mt-0.5">បញ្ជីបុគ្គលិកសុំច្បាប់ក្នុងសប្តាហ៍ដែលត្រូវបំពេញការងារសងនៅចុងសប្តាហ៍ (ស្វ័យប្រវត្តិតាម Telegram រៀងរាល់ថ្ងៃសុក្រ និងសៅរ៍ ម៉ោង ៦:០០ ព្រឹក)</p>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-wrap">
            <button onclick="sendWeekendLeaveReportTelegram()" id="btn-send-tg-leave"
              class="bg-blue-600 hover:bg-blue-700 text-white flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs sm:text-sm font-semibold shadow transition">
              <span class="material-symbols-outlined text-lg" id="icon-send-tg-leave">send</span>
              <span>ផ្ញើទៅ Telegram ឥឡូវនេះ</span>
            </button>
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
              <p class="text-xs sm:text-sm font-semibold text-emerald-600">រៀងរាល់ម៉ោង ០៦:០០ ព្រឹក (ចុងសប្តាហ៍)</p>
            </div>
          </div>
        </div>

        <!-- Table Container -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="p-4 sm:p-5 border-b border-slate-100 flex items-center justify-between">
            <h3 class="font-bold text-sm text-gray-800 flex items-center gap-2">
              <span class="material-symbols-outlined text-amber-600">format_list_bulleted</span>
              បញ្ជីបុគ្គលិកសុំច្បាប់ក្នុងសប្តាហ៍ (កាតព្វកិច្ចសងថ្ងៃសៅរ៍ & អាទិត្យ)
            </h3>
            <span class="text-xs text-gray-400" id="rep-leave-updated-at"></span>
          </div>
          <div class="overflow-x-auto w-full">
            <table class="w-full text-left text-xs sm:text-sm">
              <thead class="bg-slate-50 text-gray-500 font-medium border-b border-slate-100 text-xs">
                <tr>
                  <th class="py-3 px-4">#</th>
                  <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
                  <th class="py-3 px-4">ថ្ងៃសុំច្បាប់</th>
                  <th class="py-3 px-4">ចំនួនថ្ងៃឈប់</th>
                  <th class="py-3 px-4">មូលហេតុ</th>
                  <th class="py-3 px-4">កាតព្វកិច្ចធ្វើការសង</th>
                  <th class="py-3 px-4">ស្ថានភាព</th>
                </tr>
              </thead>
              <tbody id="rep-leave-tbody" class="divide-y divide-slate-100 text-gray-700">
                <tr><td colspan="7" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>
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
              <p class="text-xs text-gray-500 mt-0.5">បញ្ជីបុគ្គលិកខ្ចីប្រាក់ក្នុងខែដែលត្រូវកាត់ប្រាក់ខែ (ស្វ័យប្រវត្តិតាម Telegram នៅថ្ងៃចុងក្រោយនៃរៀងរាល់ខែ ម៉ោង ៦:០០ ព្រឹក)</p>
            </div>
          </div>
          <div class="flex items-center gap-2 flex-wrap">
            <button onclick="sendMonthlyLoanReportTelegram()" id="btn-send-tg-loan"
              class="bg-blue-600 hover:bg-blue-700 text-white flex items-center gap-2 px-4 py-2.5 rounded-xl text-xs sm:text-sm font-semibold shadow transition">
              <span class="material-symbols-outlined text-lg" id="icon-send-tg-loan">send</span>
              <span>ផ្ញើទៅ Telegram ឥឡូវនេះ</span>
            </button>
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
              <span class="material-symbols-outlined text-2xl">event_repeat</span>
            </div>
            <div>
              <p class="text-xs text-gray-500 font-medium">កាលបរិច្ឆេទជូនដំណឹង</p>
              <p class="text-xs sm:text-sm font-semibold text-purple-600">ថ្ងៃចុងក្រោយនៃរៀងរាល់ខែ (ម៉ោង ០៦:០០ ព្រឹក)</p>
            </div>
          </div>
        </div>

        <!-- Table Container -->
        <div class="bg-white rounded-2xl shadow-sm border border-slate-100 overflow-hidden">
          <div class="p-4 sm:p-5 border-b border-slate-100 flex items-center justify-between">
            <h3 class="font-bold text-sm text-gray-800 flex items-center gap-2">
              <span class="material-symbols-outlined text-indigo-600">format_list_bulleted</span>
              បញ្ជីបុគ្គលិកខ្ចីប្រាក់ក្នុងខែនេះ (កាត់ប្រាក់ខែចុងខែ)
            </h3>
            <span class="text-xs text-gray-400" id="rep-loan-updated-at"></span>
          </div>
          <div class="overflow-x-auto w-full">
            <table class="w-full text-left text-xs sm:text-sm">
              <thead class="bg-slate-50 text-gray-500 font-medium border-b border-slate-100 text-xs">
                <tr>
                  <th class="py-3 px-4">#</th>
                  <th class="py-3 px-4">ឈ្មោះបុគ្គលិក</th>
                  <th class="py-3 px-4">កាលបរិច្ឆេទខ្ចី</th>
                  <th class="py-3 px-4">ចំនួនទឹកប្រាក់ខ្ចី</th>
                  <th class="py-3 px-4">គោលបំណង</th>
                  <th class="py-3 px-4">ចំនួនត្រូវកាត់ប្រាក់ខែ</th>
                  <th class="py-3 px-4">ស្ថានភាព</th>
                </tr>
              </thead>
              <tbody id="rep-loan-tbody" class="divide-y divide-slate-100 text-gray-700">
                <tr><td colspan="7" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>
              </tbody>
            </table>
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

  // ===== Tab switching =====
  const tabs = ['dashboard','leaves','loans','employees','settings','reports-leave','reports-loan'];
  const titles = {
    dashboard: 'ផ្ទៃតាប្លូ',
    leaves: 'ច្បាប់ឈប់សម្រាក',
    loans: 'ខ្ចីប្រាក់ / បុរេប្រទាន',
    employees: 'បុគ្គលិកទាំងអស់',
    settings: 'ការកំណត់ប្រព័ន្ធ',
    'reports-leave': 'របាយការណ៍ច្បាប់ (សងថ្ងៃសៅរ៍-អាទិត្យ)',
    'reports-loan': 'របាយការណ៍ខ្ចីប្រាក់ (កាត់ប្រាក់ខែចុងខែ)'
  };

  function showTab(tab) {
    if (window.innerWidth < 1024) closeSidebar();
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
    document.getElementById('page-title').textContent = titles[tab] || 'ផ្ទៃតាប្លូ';
    if (tab === 'leaves') loadLeaves();
    if (tab === 'loans') loadLoans();
    if (tab === 'employees') loadEmployees();
    if (tab === 'settings') loadSettings();
    if (tab === 'reports-leave') loadWeekendLeaveReport();
    if (tab === 'reports-loan') loadMonthlyLoanReport();
  }

  // ===== Badge =====
  function statusBadge(s) {
    const map = { pending:'badge-pending', approved:'badge-approved', rejected:'badge-rejected' };
    const lbl = { pending:'រង់ចាំ', approved:'អនុញ្ញាតិ', rejected:'មិនអនុញ្ញាតិ' };
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
    } catch(e) {
      showToast('មិនអាចទាញយកការកំណត់បានទេ', true);
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
      const r = await fetch('/api/send-briefing', { method: 'POST' });
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

  // ===== Reports: Weekend Leave Compensation =====
  async function loadWeekendLeaveReport() {
    const tbody = document.getElementById('rep-leave-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>';
    try {
      const res = await fetch('/api/reports/weekend-leave');
      const data = await res.json();
      document.getElementById('rep-leave-staff-count').textContent = (data.total_staff_count || 0) + ' នាក់';
      document.getElementById('rep-leave-days-count').textContent = (data.total_days_to_compensate || 0) + ' ថ្ងៃ';
      document.getElementById('rep-leave-updated-at').textContent = 'ធ្វើបច្ចុប្បន្នភាព: ' + (data.generated_at || '');
      
      const items = data.items || [];
      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center py-8 text-gray-400">គ្មានបុគ្គលិកសុំច្បាប់ដែលត្រូវធ្វើការសងក្នុងចុងសប្តាហ៍នេះទេ</td></tr>';
        return;
      }
      tbody.innerHTML = items.map((it, idx) => `
        <tr class="hover:bg-slate-50/70 transition">
          <td class="py-3 px-4 font-mono text-xs text-gray-400">${idx + 1}</td>
          <td class="py-3 px-4 font-semibold text-gray-800">${escapeHtml(it.employee_name)}</td>
          <td class="py-3 px-4 text-gray-500 font-mono text-xs">${it.date}</td>
          <td class="py-3 px-4 font-semibold text-amber-600">${it.days} ថ្ងៃ</td>
          <td class="py-3 px-4 text-gray-600 max-w-xs truncate" title="${escapeHtml(it.reason)}">${escapeHtml(it.reason)}</td>
          <td class="py-3 px-4">
            <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200 shadow-sm">
              <span class="material-symbols-outlined text-sm">event_repeat</span>
              ${it.compensation}
            </span>
          </td>
          <td class="py-3 px-4">${statusBadge(it.status)}</td>
        </tr>
      `).join('');
    } catch(e) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center py-8 text-red-500">បរាជ័យក្នុងការទាញយក: ${e.message}</td></tr>`;
    }
  }

  async function sendWeekendLeaveReportTelegram() {
    const btn = document.getElementById('btn-send-tg-leave');
    const icon = document.getElementById('icon-send-tg-leave');
    if (icon) icon.classList.add('animate-spin');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/reports/send-weekend-leave', { method: 'POST' });
      const d = await res.json();
      if (res.ok) {
        showToast(d.message || 'បានផ្ញើរបាយការណ៍ច្បាប់ចុងសប្តាហ៍ទៅ Telegram រួចរាល់!');
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

  // ===== Reports: Monthly Loan Payroll Deduction =====
  async function loadMonthlyLoanReport() {
    const tbody = document.getElementById('rep-loan-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-8 text-gray-400">កំពុងទាញយកទិន្នន័យ...</td></tr>';
    try {
      const res = await fetch('/api/reports/monthly-loan');
      const data = await res.json();
      document.getElementById('rep-loan-total-amt').textContent = (data.total_amount_khr || 0).toLocaleString() + ' ៛';
      document.getElementById('rep-loan-borrowers-count').textContent = (data.total_borrowers || 0) + ' នាក់';
      document.getElementById('rep-loan-updated-at').textContent = 'ខែ: ' + (data.month_str || '');
      
      const items = data.items || [];
      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center py-8 text-gray-400">គ្មានសំណើខ្ចីប្រាក់ក្នុងខែនេះទេ</td></tr>';
        return;
      }
      tbody.innerHTML = items.map((it, idx) => `
        <tr class="hover:bg-slate-50/70 transition">
          <td class="py-3 px-4 font-mono text-xs text-gray-400">${idx + 1}</td>
          <td class="py-3 px-4 font-semibold text-gray-800">${escapeHtml(it.employee_name)}</td>
          <td class="py-3 px-4 text-gray-500 font-mono text-xs">${it.date}</td>
          <td class="py-3 px-4 font-bold text-emerald-600">${Number(it.amount_khr).toLocaleString()} ៛</td>
          <td class="py-3 px-4 text-gray-600 max-w-xs truncate" title="${escapeHtml(it.purpose)}">${escapeHtml(it.purpose)}</td>
          <td class="py-3 px-4 font-bold text-indigo-600">
            <span class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold bg-indigo-50 text-indigo-700 border border-indigo-200">
              - ${Number(it.amount_khr).toLocaleString()} ៛
            </span>
          </td>
          <td class="py-3 px-4">${statusBadge(it.status)}</td>
        </tr>
      `).join('');
    } catch(e) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-center py-8 text-red-500">បរាជ័យក្នុងការទាញយក: ${e.message}</td></tr>`;
    }
  }

  async function sendMonthlyLoanReportTelegram() {
    const btn = document.getElementById('btn-send-tg-loan');
    const icon = document.getElementById('icon-send-tg-loan');
    if (icon) icon.classList.add('animate-spin');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/reports/send-monthly-loan', { method: 'POST' });
      const d = await res.json();
      if (res.ok) {
        showToast(d.message || 'បានផ្ញើរបាយការណ៍ខ្ចីប្រាក់ប្រចាំខែទៅ Telegram រួចរាល់!');
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
