
# Part A — Code Review

| # | Where | What is wrong | How to detect it | How I fixed it |
|---|---|---|---|---|
| 1 | health() | The endpoint always returns 200 without checking MongoDB connectivity. | Stop MongoDB connectivity and request GET /health. The original code still returns 200. | Added a database ping and handled PyMongoError by returning HTTP 503. |



| 2 | list_employees() | The skip calculation used page * page_size, causing the first page to skip records. | Request page=1 and page_size=2. The original code skips the first two employees. | Changed the formula to (page - 1) * page_size. |



| 3 | list_employees() | Employee records were returned without sorting by emp_code. | Insert employees in random order and request GET /employees. The order is not guaranteed. | Added .sort("emp_code", 1) before skip and limit to return employees in ascending order. |

 5 
| 4 | list_employees() | The total count ignored the department filter. | Request GET /employees?department=Sales. The original code returns total=6 instead of 2. | Replaced count_documents({}) with count_documents(q) to count only matching employees. |


| 5 | list_employees() | Pagination parameters had no minimum or maximum validation. | Send page=0 or page_size=101. The original endpoint accepts invalid values. | Added FastAPI Query constraints: page >= 1 and 1 <= page_size <= 100. Verified invalid requests are rejected. |



| 6 | list_attendance() | Pagination parameters had no validation, allowing invalid page and page_size values. | Requests with page=0 or page_size=101 were not rejected by the original endpoint. | Added FastAPI Query constraints: page >= 1 and 1 <= page_size <= 100. Verified both invalid requests return HTTP 422. |



| 7 | list_attendance() | The original code loaded all matching attendance records into Python memory and sorted and paginated them there. This is inefficient for large datasets. | With 100,000 records, even page_size=2 would fetch every matching document. | Moved sorting, skip and limit into the MongoDB query. Verified page=1 and page_size=2 return the expected sample records. Index performance verification is pending. |



| 8 | list_attendance() | MongoDB datetime values were returned as ISO strings instead of epoch milliseconds, and internal IDs were exposed. Legacy records could also omit history and half_day. | GET /attendance returned string timestamps and an id field. Tested a corrected record containing nested history timestamps. | Added recursive serialization to convert datetimes to epoch milliseconds, remove internal _id fields, and supply defaults for missing history and half_day. Verified normal and nested timestamps. |



| 9 | EmployeeIn validation | The original Pydantic model accepted invalid employee data because fields had basic string types without sufficient validation. | Invalid shift time 99:99, nonexistent date 2026-02-30, and identical shift start/end values were accepted or insufficiently checked. | Added field constraints, time-format validation, calendar-date validation, and a cross-field shift validator. Verified invalid inputs raise ValidationError, valid input succeeds, and POST /employees returns HTTP 422 for an invalid shift. |



| 10 | compute_late_minutes() | The original function compared UTC punch-in timestamps directly against IST shift timings and assumed the shift started on the punch-in date. | A 09:42 IST punch-in for a 09:30 shift incorrectly returned 0 instead of 12 late minutes. An overnight punch-in at 00:05 for a previous-day 23:50 shift returned 0 instead of 15. | Converted timestamps to IST before calculating lateness and considered adjacent shift-start dates. Verified UTC conversion, the 10-minute grace period, overnight shifts, and daytime regression cases. Limitation: without an explicit scheduled shift date, the nearest-shift approach can be ambiguous for unusually early or late punches. |



| 11 | compute_work_hours() | The original function used Python's built-in round(), which does not reliably implement the required Decimal ROUND_HALF_UP rule. | A duration of 1 hour and 18 seconds equals 1.005 hours, but the original function returned 1.00 instead of 1.01. | Replaced float-based rounding with Decimal arithmetic and ROUND_HALF_UP to two decimal places. Verified boundary cases of 1 hour 17 seconds, 18 seconds, and 54 seconds. |


