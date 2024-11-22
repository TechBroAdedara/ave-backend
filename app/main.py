
from datetime import datetime
import os
from typing import Annotated, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from icecream import ic
import logging

from sqlalchemy.exc import IntegrityError
from .routes import auth
from .routes.auth import (
    get_current_admin_user,
    get_current_student_user,
    get_current_user,
)
from .schemas.geofence import GeofenceCreate

from sqlalchemy import func
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.models.user import User
from app.models.geofence import Geofence
from app.models.attendanceRecord import AttendanceRecord
from .utils.algorithms import check_user_in_circular_geofence, generate_alphanumeric_code
from .database import get_db

if os.getenv("ENVIRONMENT") == "development":
    load_dotenv()

db_dependency = Annotated[Session, Depends(get_db)]
admin_dependency = Annotated[dict, Depends(get_current_admin_user)]

def get_db():
    db = SessionLocal()  # Create a new session
    try:
        yield db  # Yield the session to be used
    finally:
        db.close()  # Close the session when done



# ----------------------------------------Allowed Origins--------------------------------------------
origins = [
    "http://localhost:3000",
    "http://localhost",
]
# ----------------------------------------FastAPI App Init--------------------------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Just for Development. Would be changed later.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)




# ----------------------------------------Routes--------------------------------------------
@app.get("/")
def index():
    return "Hello! Access our documentation by adding '/docs' to the url above"


@app.get("/user/")
def get_user(user_matric: str, db: db_dependency, _: admin_dependency):
    """Get the user and their records from the database."""
    try:
        user_records = (
            db.query(
                User.user_matric,
                User.username,
                User.role,
                AttendanceRecord.geofence_name,
                AttendanceRecord.timestamp,
            )
            .outerjoin(
                AttendanceRecord, User.user_matric == AttendanceRecord.user_matric
            )
            .filter(User.user_matric == user_matric)
            .all()
        )

        if not user_records:
            raise HTTPException(status_code=404, detail="User not found")

        # Extract user details and attendance records
        attendances = [
            {
                "Class name": geofence_name,
                "Attendance timestamp": timestamp,
            }
            for _, _, _, geofence_name, timestamp in user_records
            if geofence_name is not None and timestamp is not None
        ]

        # Assuming user_records will have at least one record
        record = {
            "user_matric": user_records[0][0],  # user_matric
            "username": user_records[0][1],  # username
            "role": user_records[0][2],  # role
            "Attendances": attendances,
        }

        return record

    except Exception as e:
        logging.error(e)
        raise HTTPException(
            status_code=500,
            detail="Internal Error: Contact Administrator (This wasn't even supposed to happen lol)",
        )

@app.post("/create_geofences/")
def create_geofence(
    geofence: GeofenceCreate, user: admin_dependency, db: db_dependency
):
    """Creates a Geofence with a specific start_time and end_time."""

    start_time = geofence.start_time.replace(tzinfo=ZoneInfo("Africa/Lagos"))
    end_time = geofence.end_time.replace(tzinfo=ZoneInfo("Africa/Lagos"))

    start_time_utc = start_time.astimezone(ZoneInfo("UTC"))
    end_time_utc = end_time.astimezone(ZoneInfo("UTC"))
    # Check if a geofence with the same name and date exists
    db_geofence = (
        db.query(Geofence)
        .filter(
            Geofence.name == geofence.name,
            func.date(Geofence.start_time) == start_time_utc.date(),
        )
        .first()
    )

    if db_geofence:
        raise HTTPException(
            status_code=400,
            detail="Geofence with this name already exists for today",
        )

    try:
        # Check that the start time is before the end time
        if start_time_utc >= end_time_utc:
            raise HTTPException(
                status_code=400,
                detail="Invalid duration for geofence. Please adjust duration and try again.",
            )

        # Ensure that the end time is not in the past
        if end_time_utc < datetime.now(ZoneInfo("UTC")):
            raise HTTPException(
                status_code=400, detail="End time cannot be in the past."
            )

        # Generate a unique code for the geofence
        initial_code = generate_alphanumeric_code()
        code = initial_code.lower()
        # Create a new geofence record
        new_geofence = Geofence(
            fence_code=code,
            name=geofence.name,
            creator_matric=user["user_matric"],
            latitude=geofence.latitude,
            longitude=geofence.longitude,
            radius=geofence.radius,
            fence_type=geofence.fence_type,
            start_time=start_time_utc,  # Save start time in UTC
            end_time=end_time_utc,  # Save end time in UTC
            status=(
                "active"
                if start_time_utc <= datetime.now(ZoneInfo("UTC")) <= end_time_utc
                else "scheduled"
            ),
            time_created=datetime.now(ZoneInfo("UTC")),
        )
        print(start_time, end_time)
        print(start_time_utc, end_time_utc)
        db.add(new_geofence)
        db.commit()
        db.refresh(new_geofence)

        return {"Code": code, "name": geofence.name}

    except IntegrityError as e:
        logging.error(e)
        # if e.errno == 1062:  # Duplicate entry error code
        raise HTTPException(
            status_code=400, detail="Geofence with this code already exists"
        )
        # else:
        #     raise HTTPException(
        #         status_code=500, detail="Internal error. Please try again..."
        #     )


@app.get("/get_attendance/")
def get_attedance(
    course_title: str, date: datetime, db: db_dependency, user: admin_dependency
):
    """Gets the attendance record for a given course.
    User can only see the records if they created the class.
    """
    geofence_exists = (
        db.query(Geofence)
        .filter(
            Geofence.name == course_title, func.date(Geofence.start_time) == date.date()
        )
        .first()
    )

    if not geofence_exists:
        raise HTTPException(
            status_code=404,
            detail="Geofence doesn't exist for specified course and date. No records",
        )

    if geofence_exists.creator_matric != user["user_matric"]:
        raise HTTPException(
            status_code=401,
            detail="No permission to view this class attendances, as you're not the creator of the geofence",
        )

    attendances = (
        db.query(
            User.username, AttendanceRecord.user_matric, AttendanceRecord.timestamp
        )
        .join(User, AttendanceRecord.user_matric == User.user_matric)
        .filter(
            AttendanceRecord.geofence_name == course_title,
            func.date(AttendanceRecord.timestamp) == date,
        )
        .all()
    )

    if not attendances:
        raise HTTPException(status_code=404, detail="No attendance records yet")

    attendance_records = [
            {
                "username": attendance[0],
                "user_matric": attendance[1],
                "timestamp": attendance[2]
            }
            for attendance in attendances
        ]

    return {f"{course_title} attendance records": attendance_records}

# Webhook
# @app.webhooks.post("New attendance")
# def new_attendance():
#     return "Hello"




if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
