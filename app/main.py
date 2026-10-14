import json
"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator, field_validator, StrictInt
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from pymongo import ASCENDING

import re
from datetime import date
from pydantic import field_validator
from decimal import Decimal, ROUND_HALF_UP
import calendar
from datetime import timedelta

from bson import json_util
from fastapi.responses import Response

from pymongo.errors import DuplicateKeyError



load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))


def serialize_for_api(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    if isinstance(value, list):
        return [serialize_for_api(item) for item in value]

    if isinstance(value, dict):
        return {
            key: serialize_for_api(item)
            for key, item in value.items()
            if key != "_id"
        }

    return value

def serialize_attendance_for_api(document):
    result = serialize_for_api(document)
    defaults = {
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    for field, default in defaults.items():
        result.setdefault(field, default)
    return result



app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")



def get_attendance_date(punch_time, shift_start, shift_end):
    if punch_time.tzinfo is None:
        punch_time = punch_time.replace(tzinfo=timezone.utc)

    punch_ist = punch_time.astimezone(IST)
    attendance_date = punch_ist.date()

    if shift_end <= shift_start:
        punch_clock = punch_ist.strftime("%H:%M")

        if punch_clock < shift_end:
            attendance_date -= timedelta(days=1)

    return attendance_date.isoformat()


def recalculate_attendance(
    employee, attendance_date, status, punch_in, punch_out
):
    if status in ("ABSENT", "LEAVE"):
        if punch_in is not None or punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="Absent or leave records cannot have punch times"
            )

        return {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }

    if punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="Present status requires punch_in"
        )

    actual_date = get_attendance_date(
        punch_in,
        employee["shift_start"],
        employee["shift_end"]
    )

    if actual_date != attendance_date:
        raise HTTPException(
            status_code=422,
            detail="Punch-in does not match attendance date"
        )

    late = compute_late_minutes(
        punch_in, employee["shift_start"], attendance_date
    )

    if punch_out is None:
        return {
            "late_minutes": late,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False
        }

    duration = (punch_out - punch_in).total_seconds()

    if duration <= 0 or duration > 86400:
        raise HTTPException(
            status_code=422,
            detail="Invalid punch duration"
        )

    hours = compute_work_hours(punch_in, punch_out)

    overtime = compute_overtime(
        punch_out,
        employee["shift_end"],
        attendance_date,
        employee["shift_start"]
    )

    return {
        "late_minutes": late,
        "work_hours": hours,
        "overtime_minutes": overtime,
        "half_day": hours < 4.50
    }


def calculate_working_days(joined_on, month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "Month must be YYYY-MM")

    try:
        first_day = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "Invalid month")

    last_number = calendar.monthrange(
        first_day.year, first_day.month
    )[1]

    last_day = date(
        first_day.year, first_day.month, last_number
    )

    start_day = max(
        first_day,
        date.fromisoformat(joined_on)
    )

    if start_day > last_day:
        return 0

    working_days = 0
    current_day = start_day

    while current_day <= last_day:
        if current_day.weekday() < 5:
            working_days += 1

        current_day += timedelta(days=1)

    return working_days


def get_month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "Invalid month")

    try:
        first_day = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "Invalid month")

    last_day_number = calendar.monthrange(
        first_day.year,
        first_day.month
    )[1]

    last_day = date(
        first_day.year,
        first_day.month,
        last_day_number
    )

    return first_day.isoformat(), last_day.isoformat()



def build_department_summary_pipeline(month, department=None):
    first_day, last_day = get_month_bounds(month)

    match_query = {
        "joined_on": {"$lte": last_day}
    }

    if department is not None:
        match_query["department"] = department

    working_day = {
        "$in": [
            {
                "$isoDayOfWeek": {
                    "$dateFromString": {
                        "dateString": "$logs.date",
                        "format": "%Y-%m-%d",
                        "onNull": None,
                        "onError": None
                    }
                }
            },
            [1, 2, 3, 4, 5]
        ]
    }

    present_status = {
        "$in": [
            "$logs.status",
            ["PRESENT", "WFH", "ON_DUTY"]
        ]
    }

    pipeline = [
        {"$match": match_query},

        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": first_day,
                                "$lte": last_day
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },

        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },

        {
            "$group": {
                "_id": "$department",

                "employees": {
                    "$addToSet": "$emp_code"
                },

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    present_status,
                                    working_day
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$ifNull": [
                                            "$logs.half_day",
                                            False
                                        ]
                                    },
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },

                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            present_status,
                            "$logs.work_hours",
                            None
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$logs.late_minutes", 0]
                    }
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        },

        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {
                    "$size": "$employees"
                },
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },

        {"$sort": {"department": 1}}
    ]

    return pipeline


def get_department_summary(month, department=None):
    pipeline = build_department_summary_pipeline(
        month,
        department
    )

    items = list(db.employees.aggregate(pipeline))

    for item in items:
        avg = item["avg_work_hours"]

        if avg is not None:
            item["avg_work_hours"] = float(
                Decimal(str(avg)).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )
            )

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(...),
    department: Optional[str] = None
):
    return get_department_summary(month, department)




# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in, shift_start, attendance_date=None):
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    punch_ist = punch_in.astimezone(IST)
    if attendance_date is None:
        attendance_date = punch_ist.date().isoformat()

    h, m = map(int, shift_start.split(":"))
    start = datetime.combine(
        date.fromisoformat(attendance_date),
        datetime.min.time(),
        tzinfo=IST
    ).replace(hour=h, minute=m)

    elapsed_seconds = (punch_ist - start).total_seconds()
    return int(elapsed_seconds // 60) if elapsed_seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = (punch_out - punch_in).total_seconds()

    hours = Decimal(str(seconds)) / Decimal("3600")

    rounded_hours = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    return float(rounded_hours)


def build_monthly_pipeline(emp_code, month):
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month + "-01",
                    "$lte": month + "-31"
                }
            }
        },
        {
            "$group": {
                "_id": None,

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date",
                                                        "format": "%Y-%m-%d"
                                                    }
                                                }
                                            },
                                            5
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },

                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },

                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    
    return pipeline



def get_monthly_log_totals(emp_code, month):
    pipeline = build_monthly_pipeline(emp_code, month)

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        return results[0]

    return {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0
    }



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(...)):

    # Validate the month
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="Month must be YYYY-MM"
        )

    try:
        date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Invalid month"
        )

    # Find employee
    employee = db.employees.find_one({
        "emp_code": emp_code
    })

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    # Calculate working days
    working_days = calculate_working_days(
        employee["joined_on"], month
    )

    # MongoDB aggregation
    totals = get_monthly_log_totals(emp_code, month)

    present_days = totals["present_days"]

    # Calculate attendance percentage
    if working_days == 0:
        attendance_pct = None
    else:
        percentage = (
            Decimal(str(present_days))
            / Decimal(working_days)
            * Decimal("100")
        )

        attendance_pct = float(
            percentage.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals["leave_days"],
        "late_count": totals["late_count"],
        "total_late_minutes": totals["total_late_minutes"],
        "total_overtime_minutes": totals["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }




def compute_overtime(punch_out, shift_end, date_str, shift_start):
    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)

    punch_ist = punch_out.astimezone(IST)

    h, m = map(int, shift_end.split(":"))

    end = datetime.fromisoformat(date_str).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    start_h, start_m = map(int, shift_start.split(":"))

    if (h, m) < (start_h, start_m):
        end += timedelta(days=1)

    minutes = int((punch_ist - end).total_seconds() // 60)

    return minutes if minutes >= 30 else 0



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$"
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Date must be YYYY-MM-DD")

        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("Invalid calendar date")

        return value


    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self




class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_provided_timestamp(cls, value):
        if value is None:
            raise ValueError("punched_at cannot be null when provided")
        return value



class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000
    )

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_provided_timestamp(cls, value):
        if value is None:
            raise ValueError("punched_at cannot be null when provided")
        return value



class RegularizeIn(BaseModel):
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None

    punch_in: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )

    punch_out: Optional[StrictInt] = Field(
        default=None, ge=100000000000, le=4102444800000
    )

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_changes(self):
        provided = self.model_fields_set

        for field in ("status", "punch_in", "punch_out"):
            if field in provided and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")

        if self.status in ("ABSENT", "LEAVE"):
            if "punch_in" in provided or "punch_out" in provided:
                raise ValueError(
                    "ABSENT or LEAVE cannot include punch times"
                )

        return self



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}

    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()

    doc["created_at"] = datetime.now(
        timezone.utc
    ).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    return serialize_for_api(doc)



@app.get("/employees")

def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [serialize_for_api(item) for item in items], "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})


    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    
    ts = (
        datetime.fromtimestamp(body.punched_at // 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    
    d = get_attendance_date(
        ts,
        emp["shift_start"],
        emp["shift_end"]
    )

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date"
        )

    return serialize_attendance_for_api(doc)





@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):

    employee = db.employees.find_one({
        "emp_code": body.emp_code
    })

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    if body.punched_at is None:
        punched_at = datetime.now(timezone.utc).replace(
            microsecond=0
        )
    else:
        punched_at = datetime.fromtimestamp(
            body.punched_at // 1000,
            tz=timezone.utc
        )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at}
        },
        sort=[("punch_in", -1)]
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found"
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out"
        )

    punch_in_time = record["punch_in"]

    if punch_in_time.tzinfo is None:
        punch_in_time = punch_in_time.replace(
            tzinfo=timezone.utc
        )

    duration = (punched_at - punch_in_time).total_seconds()

    if duration <= 0 or duration > 86400:
        raise HTTPException(
            status_code=422,
            detail="Invalid punch-out time"
        )

    hours = compute_work_hours(
        punch_in_time, punched_at
    )

    overtime = compute_overtime(
        punched_at,
        employee["shift_end"],
        record["date"],
        employee["shift_start"]
    )

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": hours < 4.50
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out"
        )

    return serialize_attendance_for_api(updated)


def build_attendance_query(
    emp_code=None,
    date_from=None,
    date_to=None,
    status=None
):
    q = {}
    for field_name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise HTTPException(422, f"Invalid {field_name}")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(422, f"Invalid {field_name}")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "Invalid date range")
    if status is not None and status not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
        raise HTTPException(422, "Invalid status")


    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    
    q = build_attendance_query(
        emp_code=emp_code,
        date_from=date_from,
        date_to=date_to,
        status=status
    )

    
    total = db.attendance_logs.count_documents(q)
    page_docs = list(db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    
    page_docs = [serialize_attendance_for_api(d) for d in page_docs]

    for d in page_docs:
        d.setdefault("history", [])
        d.setdefault("half_day", False)

    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):

    # Validate attendance date
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(422, "Invalid attendance date")

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(422, "Invalid calendar date")

    # Find employee and attendance record
    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(404, "Employee not found")

    old = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if old is None:
        raise HTTPException(404, "Attendance record not found")

    # Convert MongoDB datetimes to UTC-aware datetimes
    def as_utc(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    provided = body.model_fields_set

    status = body.status if "status" in provided else old["status"]
    punch_in = as_utc(old.get("punch_in"))
    punch_out = as_utc(old.get("punch_out"))

    if status in ("ABSENT", "LEAVE"):
        punch_in = None
        punch_out = None
    else:
        if "punch_in" in provided:
            punch_in = datetime.fromtimestamp(
                body.punch_in // 1000, tz=timezone.utc
            )

        if "punch_out" in provided:
            punch_out = datetime.fromtimestamp(
                body.punch_out // 1000, tz=timezone.utc
            )

    # Recalculate derived attendance values
    calculated = recalculate_attendance(
        employee, date, status, punch_in, punch_out
    )

    new_values = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        **calculated
    }

    # Record only fields that actually changed
    changes = {}

    for field, new_value in new_values.items():
        old_value = old.get(field, False if field == "half_day" else None)
        old_value = as_utc(old_value) if isinstance(old_value, datetime) else old_value

        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value
            }

    if not any(
        field in changes
        for field in ("status", "punch_in", "punch_out")
    ):
        raise HTTPException(422, "No attendance changes provided")

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Prevent concurrent corrections from overwriting history
    condition = {
        "_id": old["_id"],
        "status": old["status"],
        "punch_in": old.get("punch_in"),
        "punch_out": old.get("punch_out")
    }

    if "history" in old:
        condition["history"] = old["history"]
    else:
        condition["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        condition,
        {
            "$set": new_values,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if updated is None:
        raise HTTPException(
            409, "Attendance was modified by another request"
        )

    return serialize_attendance_for_api(updated)



def build_late_leaderboard_pipeline(
    month,
    limit=10,
    department=None
):
    first_day, last_day = get_month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first_day,
                    "$lte": last_day
                }
            }
        },

        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$late_minutes", 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },

        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },

        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },

        {"$unwind": "$employee"}
    ]

    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {
                    "total_late_minutes": -1
                },
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },

        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },

        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },

        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        }
    ])

    
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(
        month, limit, department
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }




@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    return _department_trend_impl(
        department, from_date, to_date
    )


def _department_trend_impl(
    department, from_date, to_date, return_pipeline=False
):

    # Validate dates
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_date):
            raise ValueError("Invalid from date")

        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_date):
            raise ValueError("Invalid to date")

        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)

    except ValueError:
        raise HTTPException(422, "Invalid date format")

    total_days = (end_day - start_day).days + 1

    if total_days < 1 or total_days > 92:
        raise HTTPException(422, "Date range must be 1 to 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Department not found")

    start = datetime.combine(
        start_day,
        datetime.min.time(),
        tzinfo=timezone.utc
    )

    end_exclusive = datetime.combine(
        end_day + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc
    )

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"date": {"$literal": start}}},

        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start, end_exclusive]
                }
            }
        },

        {
            "$match": {
                "date": {
                    "$gte": start,
                    "$lt": end_exclusive
                }
            }
        },

        {
            "$set": {
                "day": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date"
                    }
                },
                "is_working_day": {
                    "$lte": [
                        {"$isoDayOfWeek": "$date"},
                        5
                    ]
                }
            }
        },

        # Count employees who joined by each day
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$day"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department
                        }
                    },
                    {
                        "$match": {
                            "$expr": {
                                "$lte": ["$joined_on", "$$day"]
                            }
                        }
                    },
                    {"$count": "total"}
                ],
                "as": "staff"
            }
        },

        # Calculate present and late attendance
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$day"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$day"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee"
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                [
                                                    "PRESENT",
                                                    "WFH",
                                                    "ON_DUTY"
                                                ]
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$ifNull": [
                                                        "$half_day",
                                                        False
                                                    ]
                                                },
                                                0.5,
                                                1
                                            ]
                                        },
                                        0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$late_minutes",
                                                0
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "daily"
            }
        },

        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$staff.total", 0]},
                        0
                    ]
                },
                "present_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily.present_count",
                                0
                            ]
                        },
                        0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$daily.late_count",
                                0
                            ]
                        },
                        0
                    ]
                }
            }
        },

        # Rate only for working days with employees
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount"
                            ]
                        },
                        None
                    ]
                }
            }
        },

        # MongoDB calculates the rolling average
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },

        {
            "$project": {
                "_id": 0,
                "date": "$day",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },

        {"$sort": {"date": 1}}
    ]


    if return_pipeline:
        return pipeline


    items = list(db.employees.aggregate(pipeline))

    # ROUND_HALF_UP to 4 decimal places
    for item in items:
        for field in ["attendance_rate", "moving_avg_7d"]:
            value = item[field]

            if value is not None:
                item[field] = float(
                    Decimal(str(value)).quantize(
                        Decimal("0.0001"),
                        rounding=ROUND_HALF_UP
                    )
                )

    return {
        "department": department,
        "items": items
    }


@app.on_event("startup")
def create_database_indexes():

    # Employee code must be unique
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="unique_employee_code"
    )

    # Faster department and joining-date queries
    db.employees.create_index(
        [
            ("department", ASCENDING),
            ("joined_on", ASCENDING)
        ],
        name="department_joined_index"
    )

    # One attendance record per employee per date
    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING)
        ],
        unique=True,
        name="unique_employee_date"
    )

    # Faster date-based attendance analytics
    db.attendance_logs.create_index(
        [
            ("date", ASCENDING),
            ("emp_code", ASCENDING)
        ],
        name="date_employee_index"
    )

    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="joined_on_index"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="date_desc_employee_index"
    )

    print("MongoDB indexes ready")



@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    limit: int = Query(10, ge=1, le=50),

    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),

):

    if endpoint not in {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}:
        raise HTTPException(422, "Unsupported explain endpoint")

    if endpoint == "attendance_list":

        q = build_attendance_query(
            emp_code=emp_code,
            date_from=date_from,
            date_to=date_to,
            status=status
        )

        command = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {
                "date": -1,
                "emp_code": 1
            },
            "skip": (page - 1) * page_size,
            "limit": page_size
        }

        
        result = db.command(
            "explain",
            command,
            verbosity="executionStats"
        )

        return {
            "endpoint": endpoint,
            "collection": "employees" if endpoint in ("department_summary", "department_trend") else "attendance_logs",
            "explain": json.loads(json_util.dumps(result))
        }
    
    if endpoint == "employee_monthly":

        if not emp_code:
            raise HTTPException(
                status_code=422,
                detail="emp_code is required"
            )

        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        # Validate the month
        get_month_bounds(month)

        # Reuse the exact monthly analytics pipeline
        pipeline = build_monthly_pipeline(
            emp_code,
            month
        )

        result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        return {
            "endpoint": endpoint,
            "collection": "employees" if endpoint in ("department_summary", "department_trend") else "attendance_logs",
            "explain": json.loads(json_util.dumps(result))
        }
    
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        pipeline = build_department_summary_pipeline(
            month,
            department
        )

        result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        return {
            "endpoint": endpoint,
            "collection": "employees" if endpoint in ("department_summary", "department_trend") else "attendance_logs",
            "explain": json.loads(json_util.dumps(result))
        }
    
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required"
            )

        pipeline = build_late_leaderboard_pipeline(
            month, limit, department
        )

        result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        return {
            "endpoint": endpoint,
            "collection": "employees" if endpoint in ("department_summary", "department_trend") else "attendance_logs",
            "explain": json.loads(json_util.dumps(result))
        }

    
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required"
            )

        pipeline = _department_trend_impl(
            department,
            from_date,
            to_date,
            return_pipeline=True
        )

        result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        return {
            "endpoint": endpoint,
            "collection": "employees" if endpoint in ("department_summary", "department_trend") else "attendance_logs",
            "explain": json.loads(json_util.dumps(result))
        }






    raise HTTPException(
        status_code=501,
        detail="Explain for this endpoint is not implemented yet"
    )






# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
