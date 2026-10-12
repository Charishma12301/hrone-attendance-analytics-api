
"""Employee Attendance & Analytics API for the HROne engineering assignment."""

import os
import re
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENT + ("ABSENT", "LEAVE")

MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------- Common validation and conversion ----------

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def from_ms(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_MS <= value <= MAX_MS
    ):
        raise HTTPException(
            422,
            "timestamp must be an integer epoch-milliseconds value in range",
        )

    return datetime.fromtimestamp(value / 1000, UTC).replace(
        microsecond=0
    )


def mongo_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def api_dt(value):
    """Convert Mongo datetimes and legacy integer timestamps to epoch ms."""
    if isinstance(value, datetime):
        return to_ms(value)

    # Legacy records may contain Unix seconds instead of BSON datetimes.
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 < value < MIN_MS:
            return value * 1000
        if MIN_MS <= value <= MAX_MS:
            return value

    return None


def parse_date(value, field="date"):
    try:
        if not isinstance(value, str):
            raise ValueError
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(
            422,
            f"{field} must be a valid YYYY-MM-DD date",
        )


def parse_month(month):
    if (
        not isinstance(month, str)
        or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month)
    ):
        raise HTTPException(422, "month must be YYYY-MM")

    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end = date(year, mon, calendar.monthrange(year, mon)[1])
    return start, end


def half_up(value, places=2):
    if value is None:
        return None

    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def shift_minutes(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
    ):
        raise ValueError("must be HH:MM in 24-hour time")

    hours, minutes = map(int, value.split(":"))
    return hours * 60 + minutes


def attendance_date(punch_in, employee):
    local = mongo_utc(punch_in).astimezone(IST)
    day = local.date()

    start = shift_minutes(employee["shift_start"])
    end = shift_minutes(employee["shift_end"])
    local_minutes = local.hour * 60 + local.minute

    if end <= start and local_minutes < end:
        day -= timedelta(days=1)

    return day.isoformat()


def late_minutes(punch_in, shift_start, shift_end=None):
    local = mongo_utc(punch_in).astimezone(IST)
    hours, minutes = map(int, shift_start.split(":"))

    shift = local.replace(
        hour=hours,
        minute=minutes,
        second=0,
        microsecond=0,
    )

    if (
        shift_end is not None
        and shift_minutes(shift_end) <= shift_minutes(shift_start)
        and local.hour * 60 + local.minute < shift_minutes(shift_end)
    ):
        shift -= timedelta(days=1)

    elapsed_seconds = (local - shift).total_seconds()

    # Late only when strictly more than 10 minutes late.
    if elapsed_seconds <= 600:
        return 0

    return max(0, int(elapsed_seconds // 60))


def shift_end_datetime(date_str, employee):
    day = parse_date(date_str)
    hours, minutes = map(int, employee["shift_end"].split(":"))

    start = shift_minutes(employee["shift_start"])
    end = shift_minutes(employee["shift_end"])

    end_day = day + timedelta(days=1) if end <= start else day

    return datetime.combine(
        end_day,
        time(hours, minutes),
        IST,
    ).astimezone(UTC)


def derived(punch_in, punch_out, employee, date_str):
    if punch_in is None:
        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    late = late_minutes(
        punch_in,
        employee["shift_start"],
        employee["shift_end"],
    )

    if punch_out is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

    seconds = int(
        (
            mongo_utc(punch_out) - mongo_utc(punch_in)
        ).total_seconds()
    )

    hours = half_up(Decimal(seconds) / Decimal(3600), 2)

    extra = int(
        (
            mongo_utc(punch_out)
            - shift_end_datetime(date_str, employee)
        ).total_seconds()
        // 60
    )

    overtime = extra if extra >= 30 else 0

    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": max(0, overtime),
        "half_day": hours < 4.50,
    }


def serialize_employee(doc):
    out = {
        key: value
        for key, value in doc.items()
        if key not in ("_id", "id")
    }

    if "created_at" in out:
        out["created_at"] = api_dt(out["created_at"])

    return out


def serialize_attendance(doc):
    out = {
        key: value
        for key, value in doc.items()
        if key not in ("_id", "id")
    }

    for field in ("punch_in", "punch_out"):
        out[field] = api_dt(out.get(field))

    out["history"] = out.get("history") or []

    for entry in out["history"]:
        if "at" in entry:
            entry["at"] = api_dt(entry.get("at"))

        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                value = change.get(side)
                if isinstance(value, datetime):
                    change[side] = api_dt(value)
                elif isinstance(value, int) and not isinstance(value, bool):
                    change[side] = api_dt(value)

    out.setdefault("late_minutes", 0)
    out.setdefault("overtime_minutes", 0)
    out.setdefault("half_day", False)
    out.setdefault("work_hours", None)

    return out


# ---------- Database indexes ----------

def ensure_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )
    db.employees.create_index(
        [
            ("department", ASCENDING),
            ("joined_on", ASCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_employee_department_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_joined_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_code",
    )
    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING),
        ],
        name="ix_attendance_employee_date_status",
    )
    db.attendance_logs.create_index(
        [
            ("date", ASCENDING),
            ("status", ASCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_attendance_date_status_code",
    )


@app.on_event("startup")
def startup():
    ensure_indexes()


# ---------- Request models ----------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift(cls, value):
        shift_minutes(value)
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined(cls, value):
        parse_date(value, "joined_on")
        return value

    @field_validator("name", "department")
    @classmethod
    def validate_nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_ts(cls, value):
        if value is not None:
            from_ms(value)
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_ts(cls, value):
        if value is not None:
            from_ms(value)
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_ts(cls, value):
        if value is not None:
            from_ms(value)
        return value

    @field_validator("reason", "regularized_by")
    @classmethod
    def validate_nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


# ---------- Basic endpoints ----------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}

    total = db.employees.count_documents(query)

    docs = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_employee(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, employee)

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(
            ts,
            employee["shift_start"],
            employee["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if not employee:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(404, "no punch-in found")

    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_time = mongo_utc(record["punch_in"])

    if ts <= punch_in_time or ts > punch_in_time + timedelta(hours=24):
        raise HTTPException(
            422,
            "punch-out must be after punch-in and within 24 hours",
        )

    fields = derived(
        record["punch_in"],
        ts,
        employee,
        record["date"],
    )

    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
    )

    if result.modified_count != 1:
        raise HTTPException(409, "record was already punched out")

    record.update({"punch_out": ts, **fields})
    return serialize_attendance(record)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")

    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    query = {}

    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)

    projection = {
        "_id": 1,
        "emp_code": 1,
        "date": 1,
        "status": 1,
        "punch_in": 1,
        "punch_out": 1,
        "work_hours": 1,
        "late_minutes": 1,
        "overtime_minutes": 1,
        "half_day": 1,
        "history": 1,
    }

    docs = list(
        db.attendance_logs.find(query, projection)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")

    old = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )

    if not old:
        raise HTTPException(404, "attendance record not found")

    new_status = body.status if body.status is not None else old["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422,
                "ABSENT/LEAVE cannot include punch times",
            )
        new_in = None
        new_out = None
    else:
        new_in = (
            from_ms(body.punch_in)
            if body.punch_in is not None
            else old.get("punch_in")
        )
        new_out = (
            from_ms(body.punch_out)
            if body.punch_out is not None
            else old.get("punch_out")
        )

        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")

        if attendance_date(new_in, employee) != date:
            raise HTTPException(
                422,
                "punch_in must belong to the record attendance date",
            )

        if new_out is not None and (
            new_out <= new_in
            or new_out > new_in + timedelta(hours=24)
        ):
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours",
            )

    calculations = derived(new_in, new_out, employee, date)

    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **calculations,
    }

    changes = {}

    for key, value in new_values.items():
        before = old.get(key)

        if isinstance(before, datetime):
            before = mongo_utc(before)
        if isinstance(value, datetime):
            value = mongo_utc(value)

        if before != value:
            changes[key] = {
                "from": old.get(key),
                "to": value,
            }

    if not changes:
        raise HTTPException(422, "request does not change the record")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": old["_id"],
            "history": old.get("history", []),
        },
        {
            "$set": new_values,
            "$push": {"history": entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409,
            "record changed concurrently; retry",
        )

    old.update(new_values)
    old.setdefault("history", []).append(entry)

    return serialize_attendance(old)


# ---------- Analytics helpers ----------

def month_filter(month):
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def weekday_count(start, end):
    if end < start:
        return 0

    days = (end - start).days + 1
    weeks, remainder = divmod(days, 7)
    total = weeks * 5

    for offset in range(remainder):
        if (start.weekday() + offset) % 7 < 5:
            total += 1

    return total


def month_log_match(emp_code, first, last):
    return {
        "emp_code": emp_code,
        "date": {"$gte": first, "$lte": last},
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last, start, end = month_filter(month)

    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "joined_on": 1},
    )

    if not employee:
        raise HTTPException(404, "employee not found")

    effective_start = max(start, parse_date(employee["joined_on"]))

    working = (
        weekday_count(effective_start, end)
        if effective_start <= end
        else 0
    )

    pipeline = [
        {"$match": month_log_match(emp_code, first, last)},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT)]},
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7],
                                                ]
                                            }
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    data = result[0] if result else {}

    present = half_up(data.get("present_days", 0), 2)
    percentage = (
        half_up(present / working * 100, 4)
        if working
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": present,
        "leave_days": data.get("leave_days", 0),
        "late_count": data.get("late_count", 0),
        "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0),
        "attendance_pct": percentage,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    first, last, _, _ = month_filter(month)

    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department

    pipeline = [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "code": "$emp_code",
                    "dept": "$department",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", first]},
                                    {"$lte": ["$date", last]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$logs.date", None]},
                                            None,
                                        ]
                                    },
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$logs.date"
                                                            }
                                                        }
                                                    },
                                                    [1, 7],
                                                ]
                                            }
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                    {
                                        "$ne": [
                                            {"$ifNull": ["$logs.work_hours", None]},
                                            None,
                                        ]
                                    },
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid": {
                                "$filter": {
                                    "input": "$hours",
                                    "as": "h",
                                    "cond": {"$ne": ["$$h", None]},
                                }
                            }
                        },
                        "in": {
                            "$cond": [
                                {"$gt": [{"$size": "$$valid"}, 0]},
                                {"$avg": "$$valid"},
                                None,
                            ]
                        },
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        item["present_days"] = half_up(item.get("present_days", 0), 2)
        item["avg_work_hours"] = half_up(item.get("avg_work_hours"), 2)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last, _, _ = month_filter(month)

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first, "$lte": last},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline += [
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]

    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(..., alias="to"),
):
    date_from = parse_date(from_, "from")
    date_to = parse_date(to, "to")

    if date_to < date_from or (date_to - date_from).days + 1 > 92:
        raise HTTPException(
            422,
            "invalid date range; maximum 92 days",
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")

    start_dt = datetime.combine(date_from, time.min, UTC)
    end_exclusive = datetime.combine(
        date_to + timedelta(days=1),
        time.min,
        UTC,
    )

    pipeline = [
        {"$match": {"department": department}},
        {"$set": {"date": start_dt}},
        {
            "$densify": {
                "field": "date",
                "partitionByFields": ["emp_code"],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date_str": {
                    "$dateToString": {
                        "date": "$date",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                }
            }
        },
        {
            "$match": {
                "date_str": {"$gte": from_, "$lte": to}
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code", "day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$eq": ["$date", "$$day"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$date_str",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$lte": ["$joined_on", "$date_str"]},
                            1,
                            0,
                        ]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$joined_on", "$date_str"]},
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$joined_on", "$date_str"]},
                                    {
                                        "$gt": [
                                            {"$ifNull": ["$logs.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "date_obj": {"$first": "$date"},
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {"$dayOfWeek": "$date_obj"},
                                [1, 7],
                            ]
                        }
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$trunc": [
                                        {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                "$present_count",
                                                                "$headcount",
                                                            ]
                                                        },
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    ]
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_obj": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$trunc": [
                                        {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$moving_avg_7d",
                                                        10000,
                                                    ]
                                                },
                                                0.5,
                                            ]
                                        }
                                    ]
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    return {
        "department": department,
        "items": list(db.employees.aggregate(pipeline)),
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")

    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code
        if status:
            if status not in STATUSES:
                raise HTTPException(422, "invalid status")
            query["status"] = status

        if date_from:
            parse_date(date_from, "date_from")
        if date_to:
            parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")

        first, last, _, _ = month_filter(month)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": first, "$lte": last},
                }
            },
            {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}},
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        first, last, _, _ = month_filter(month)

        pipeline = [
            {"$match": {"date": {"$gte": first, "$lte": last}}},
            {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}},
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        first, last, _, _ = month_filter(month)

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": first, "$lte": last},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total": {"$sum": "$late_minutes"},
                }
            },
        ]

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain,
        }

    if not department or not from_ or not to:
        raise HTTPException(
            422,
            "department, from and to are required",
        )

    date_from_value = parse_date(from_, "from")
    date_to_value = parse_date(to, "to")

    if date_to_value < date_from_value:
        raise HTTPException(422, "from must be <= to")

    pipeline = [
        {
            "$match": {
                "department": department,
                "date": {"$gte": from_, "$lte": to},
            }
        },
        {"$group": {"_id": "$date", "n": {"$sum": 1}}},
    ]

    explain = db.command(
        "explain",
        {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {},
        },
        verbosity="executionStats",
    )

    return {
        "endpoint": endpoint,
        "collection": "employees",
        "explain": explain,
    }
