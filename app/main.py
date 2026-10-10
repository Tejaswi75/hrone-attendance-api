"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds (integer maths, no float error)."""
    if dt is None:
        return None
    if dt.tzinfo is None:          # pymongo naive datetime = UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_hhmm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST date of the punch-in; for an overnight shift, a punch-in before
    shift_end belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        d -= timedelta(days=1)
    return d


def shift_start_at(att_date: date, shift_start: str) -> datetime:
    return datetime.combine(att_date, parse_hhmm(shift_start), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start;
    then whole minutes (floor) since shift start."""
    seconds = int((punch_in - shift_start_at(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def shift_end_at(att_date: date, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(att_date, parse_hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)     # overnight shift ends the next calendar day
    return end


def compute_overtime(punch_out: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) after shift end, only if >= 30."""
    seconds = int((punch_out - shift_end_at(att_date, shift_start, shift_end)).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API JSON)
# --------------------------------------------------------------------------- #
def history_out(h: dict) -> dict:
    changes = {}
    for field, ch in (h.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": to_ms(ch.get("from")), "to": to_ms(ch.get("to"))}
        else:
            changes[field] = ch
    return {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


def validation_error(loc: list, msg: str) -> RequestValidationError:
    """422 in FastAPI's default shape, for checks Pydantic cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": None}])


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: date

    @model_validator(mode="after")
    def check_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now = datetime.now(timezone.utc)
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)  # Mongo stores ms
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_ms(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    items = []
    for doc in cursor:
        doc["created_at"] = to_ms(doc.get("created_at"))
        items.append(doc)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    att = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, att, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent record whose punch_in is at or before punched_at (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)
    if ts <= pin:
        raise validation_error(["body", "punched_at"], "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise validation_error(["body", "punched_at"], "punched_at must be within 24 hours of punch_in")

    att = date.fromisoformat(rec["date"])
    work_hours = compute_work_hours(pin, ts)
    update = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, att, emp["shift_start"], emp["shift_end"]),
        "half_day": work_hours < 4.5,
    }
    # Atomic: only succeeds if nobody punched out in between -> exactly one 200 under a race.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Regularization (PATCH /attendance/{emp_code}/{date})
# --------------------------------------------------------------------------- #
TRACKED_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
ABSENCE_STATUSES = ("ABSENT", "LEAVE")


class RegularizeIn(BaseModel):
    """Derived fields are not declared, so a client sending them is simply ignored."""
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """PyMongo returns naive datetimes that are really UTC; make them aware."""
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def derived_fields(status: str, pin: Optional[datetime], pout: Optional[datetime], att: date, emp: dict) -> dict:
    """R2-R5 for any final state of a record (used after a correction)."""
    if status in ABSENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pin, att, emp["shift_start"])
    if pout is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(pin, pout)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(pout, att, emp["shift_start"], emp["shift_end"]),
        "half_day": hours < 4.5,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str,
    att: date = Path(alias="date"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    # Current state, with legacy defaults (missing half_day -> False, etc.)
    old = {
        "status": rec["status"],
        "punch_in": as_utc(rec.get("punch_in")),
        "punch_out": as_utc(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    # Final status and punch times (omitted fields stay unchanged)
    status = body.status or old["status"]
    if status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise validation_error(["body"], f"{status} clears punch times; do not send punch_in/punch_out")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if pin is None:
            raise validation_error(["body", "punch_in"], f"status {status} requires a punch_in")
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]) != att:
            raise validation_error(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if pout is not None:
            if pout <= pin:
                raise validation_error(["body", "punch_out"], "punch_out must be after punch_in")
            if pout - pin > timedelta(hours=24):
                raise validation_error(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    new = {"status": status, "punch_in": pin, "punch_out": pout, **derived_fields(status, pin, pout, att, emp)}
    if all(new[f] == old[f] for f in ("status", "punch_in", "punch_out")):
        raise validation_error(["body"], "request changes nothing")
    changes = {f: {"from": old[f], "to": new[f]} for f in TRACKED_FIELDS if new[f] != old[f]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: only apply if the record is exactly as we read it
    # (same punch fields, same number of history entries). A concurrent winner changes
    # one of these, so the loser matches nothing and gets 409 instead of losing history.
    history = rec.get("history")
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "history": {"$size": len(history)} if history is not None else {"$exists": False},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def attendance_filter(
    emp_code: Optional[str], date_from: Optional[date], date_to: Optional[date], status: Optional[str]
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise validation_error(["query", "date_from"], "date_from must not be after date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics: shared helpers and expression builders
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
    return first, last


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7)."""
    if start > end:
        return 0
    full_weeks, extra = divmod((end - start).days + 1, 7)
    n = full_weeks * 5
    for i in range(extra):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            n += 1
    return n


def half_up(value: Decimal, places: int) -> float:
    """R8 rounding: decimal half-up."""
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# Expression builders. `f` turns a field name into a reference: "$date" at top level,
# or "$$l.date" inside a $map over an array of logs.
def top(name: str) -> str:
    return f"${name}"


def is_weekday(f) -> dict:
    return {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": f("date")}}}, 5]}


def present_credit(f) -> dict:
    """R5/R6: 1 for a presence record, 0.5 if half day, 0 otherwise."""
    return {"$cond": [
        {"$in": [f("status"), PRESENCE]},
        {"$cond": [{"$ifNull": [f("half_day"), False]}, 0.5, 1]},
        0,
    ]}


def late_flag(f) -> dict:
    return {"$cond": [{"$gt": [{"$ifNull": [f("late_minutes"), 0]}, 0]}, 1, 0]}


def status_flag(f, status: str) -> dict:
    return {"$cond": [{"$eq": [f("status"), status]}, 1, 0]}


# --------------------------------------------------------------------------- #
# GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [is_weekday(top), present_credit(top), 0]}},
            "leave_days": {"$sum": status_flag(top, "LEAVE")},
            "late_count": {"$sum": late_flag(top)},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    working_days = count_weekdays(max(first, date.fromisoformat(emp["joined_on"])), last)

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}
    present = agg.get("present_days", 0)
    pct = None
    if working_days > 0:
        pct = half_up(Decimal(str(present)) * 100 / working_days, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    """Runs on `employees`, so people with zero logs are still counted (R9)."""
    emp_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_match["department"] = department

    def lg(name: str) -> str:
        return f"$$l.{name}"

    def total(expr: dict) -> dict:          # sum an expression over this employee's logs
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    has_hours = {"$and": [{"$in": [lg("status"), PRESENCE]}, {"$ne": [{"$ifNull": [lg("work_hours"), None]}, None]}]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "work_hours": 1, "late_minutes": 1}},
            ],
            "as": "logs",
        }},
        {"$project": {
            "department": 1,
            "present_days": total({"$cond": [is_weekday(lg), present_credit(lg), 0]}),
            # work hours summed as integer hundredths, so the average can be rounded exactly
            "hours_cents": total({"$cond": [has_hours, {"$round": [{"$multiply": [lg("work_hours"), 100]}, 0]}, 0]}),
            "hours_records": total({"$cond": [has_hours, 1, 0]}),
            "late_count": total(late_flag(lg)),
            "total_late_minutes": total({"$ifNull": [lg("late_minutes"), 0]}),
            "leave_count": total(status_flag(lg, "LEAVE")),
            "on_duty_count": total(status_flag(lg, "ON_DUTY")),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "hours_cents": {"$sum": "$hours_cents"},
            "hours_records": {"$sum": "$hours_records"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        avg = None
        if row["hours_records"] > 0:
            avg = half_up(Decimal(int(row["hours_cents"])) / 100 / row["hours_records"], 2)
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": row["present_days"],
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # inner join: logs whose employee does not exist are dropped
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # rank inside the department
    pipeline += [
        # standard competition ranking: ties share a rank, next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},          # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
MAX_TREND_DAYS = 92


def check_trend_range(start: date, end: date) -> None:
    if end < start:
        raise validation_error(["query", "to"], "to must not be before from")
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise validation_error(["query", "to"], f"range must not exceed {MAX_TREND_DAYS} days")


def department_trend_pipeline(department: str, emp_codes: list, start: date, end: date) -> list:
    """Runs on attendance_logs. Rates are kept as integers in units of 1/10000 so that
    half-up rounding (R8) is exact; Python only divides by 10000 at the end."""
    lo = datetime.combine(start, time(0), tzinfo=timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), time(0), tzinfo=timezone.utc)   # exclusive
    working = {"$lte": [{"$isoDayOfWeek": "$day"}, 5]}
    rate_e4 = {"$cond": [
        {"$and": [working, {"$gt": ["$headcount", 0]}]},
        # half-up(p / h, 4 dp) * 10000 == floor((20000 p + h) / (2 h)); p is a multiple of 0.5
        {"$floor": {"$divide": [
            {"$add": [{"$multiply": ["$present_count", 20000]}, "$headcount"]},
            {"$multiply": ["$headcount", 2]},
        ]}},
        None,
    ]}
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": present_credit(top)}, "late_count": {"$sum": late_flag(top)}}},
        {"$project": {"_id": 0, "day": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        # seed row so the series exists even when no log matched
        {"$unionWith": {"pipeline": [{"$documents": [{"day": lo, "present_count": 0, "late_count": 0}]}]}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": {"$ifNull": ["$present_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
        }},
        {"$set": {"day": "$_id", "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}}}},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {"$set": {"is_working_day": working, "rate_e4": rate_e4}},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "ma_sum": {"$sum": "$rate_e4", "window": {"documents": [-6, 0]}},     # $sum skips nulls
                "ma_n": {"$sum": {"$cond": [{"$eq": ["$rate_e4", None]}, 0, 1]}, "window": {"documents": [-6, 0]}},
            },
        }},
        {"$set": {"ma_e4": {"$cond": [
            {"$gt": ["$ma_n", 0]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$ma_sum", 2]}, "$ma_n"]}, {"$multiply": ["$ma_n", 2]}]}},
            None,
        ]}}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "rate_e4": 1, "ma_e4": 1,
        }},
    ]


def e4_to_float(v) -> Optional[float]:
    return None if v is None else float(Decimal(int(v)) / 10000)


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: date = Query(..., alias="from"),
    end: date = Query(..., alias="to"),
):
    check_trend_range(start, end)
    emp_codes = db.employees.distinct("emp_code", {"department": department})
    if not emp_codes:
        raise HTTPException(404, "unknown department")
    items = [
        {
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": e4_to_float(r.get("rate_e4")),
            "moving_avg_7d": e4_to_float(r.get("ma_e4")),
        }
        for r in db.attendance_logs.aggregate(department_trend_pipeline(department, emp_codes, start, end))
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
