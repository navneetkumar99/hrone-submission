# Employee Attendance & Analytics API

A REST API developed using FastAPI, Python and MongoDB for managing employee attendance, punch-in/punch-out, manual corrections and attendance analytics.

## Tech Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+

## Setup

Clone the repository and open its directory.

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Set the following environment variables to your own MongoDB connection details:

```bash
export MONGO_URI="your_mongodb_connection_string"
export MONGO_DB="hrone_attendance"
```

Start the server:

```bash
uvicorn app.main:app --port 8000
```

Check the application:

- Health check: `http://127.0.0.1:8000/health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`

## Features

- Employee creation and listing
- Attendance punch-in and punch-out
- Attendance filtering and pagination
- Manual attendance regularization with audit history
- Monthly employee attendance analytics
- Department attendance summary
- Late-arrival leaderboard with tie handling
- Daily department attendance trends and moving averages
- MongoDB query execution-plan inspection

## Implementation Notes

Attendance dates and shift timings follow Indian Standard Time (IST). MongoDB stores timestamp values as UTC datetimes. The application computes late minutes, overtime, work hours and half-day status.

Required indexes are created during application startup. A unique index on employee code and attendance date prevents duplicate daily attendance records.

See `REVIEW.md` for starter-code defect analysis and `DECISIONS.md` for engineering trade-offs.

## Security

Database connection details must be supplied through environment variables or a local `.env` file. Do not commit credentials, virtual environments or database dumps.

## Known Limitations

A separate synthetic MongoDB dataset with 100,000 attendance records was benchmarked. All tested endpoints returned HTTP 200; inspected query plans used IXSCAN with no COLLSCAN. Startup completed in 3.41 seconds. The 31-day department trend reached 5.41 seconds (a benchmark warning, not an official cutoff). These measurements are not a guarantee of performance on HROne's hidden dataset.