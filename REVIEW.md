# REVIEW.md

Defects found in the starter `app/main.py`, how I noticed each one, and the fix.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees`, `skip = page * page_size` | Page 1 skips the first `page_size` records | `GET /employees` returned `items: []` with `total: 6` | `skip = (page - 1) * page_size` |
| 2 | `list_employees`, `count_documents({})` | `total` ignores the department filter | `?department=Sales` returned 2 items but `total: 6` | `count_documents(q)` with the same filter |
| 3 | `list_employees` signature | `page` / `page_size` not validated | `?page=0` returned 200 | `Query(ge=1)` and `Query(ge=1, le=100)`, so 422 |
| 4 | `list_employees` query | No sort; contract requires `emp_code` ascending | Order depends on insertion order | `.sort("emp_code", 1)` |
| 5 | `list_employees` / `create_employee` response | `created_at` returned as a datetime string, not epoch ms | Response showed `"2026-01-05T04:30:00"` | `to_ms()` helper (integer maths) |
| 6 | `create_employee`, `find_one` then `insert_one` | Check-then-insert race | Parallel identical creates could both succeed | Unique index on `emp_code`; `DuplicateKeyError` becomes 409 |
| 7 | `EmployeeIn` model | No input validation | `emp_code: "abc"`, `joined_on: "kal"`, `shift_start: "25:99"` all returned 201 | Pydantic patterns/lengths and `joined_on: date` |
| 8 | `EmployeeIn` model | `shift_start == shift_end` accepted | `09:00`–`09:00` returned 201 | `model_validator`, so 422 |
| 9 | `create_employee`, `datetime.now()` | Naive local time stored as if UTC | `created_at` saved 5.5 hours ahead on an IST machine | `datetime.now(timezone.utc)` |
| 10 | `health` | Never checks MongoDB | `docker stop` on Mongo and `/health` still returned 200 | `ping` the server; 503 on failure (5 s server selection timeout) |
| 11 | `punch_in`, `emp["shift_start"]` | Unknown employee crashes | `emp_code: "EMP9999"` returned 500 | 404 when the employee is missing |
| 12 | `punch_in`, `datetime.fromtimestamp()` | Uses the server's local time zone | `date` and `late_minutes` change with the server's TZ | Convert epoch ms to UTC, compute in IST |
| 13 | `PunchInIn.punched_at` | Seconds, floats and strings accepted; no truncation to seconds | `punched_at: 1784520600` (seconds) returned 201 | `StrictInt` with the contract range (422); truncate to whole seconds (R1) |
| 14 | `PunchInIn.status` | Any status accepted | `status: "ABSENT"` returned 201 | `Literal["PRESENT", "WFH", "ON_DUTY"]`, so 422 |
| 15 | `punch_in`, `find_one` then `insert_one` | Check-then-insert race | Parallel punch-ins could create duplicates | Unique (`emp_code`, `date`) index; `DuplicateKeyError` becomes 409 |
| 16 | `punch_in`, `ts.date()` | Overnight shifts not handled (R1) | 02:30 punch for a 22:00-06:00 shift got the wrong date | Punch-in before `shift_end` belongs to the previous day |
| 17 | `punch_in` response | Leaks `_id` as `id`; `punch_in` not in epoch ms | Response did not match `AttendanceRecord` | Shared `record_out()` serializer |
| 18 | `compute_late_minutes`, `punch_in.replace(hour=h, ...)` | Shift start applied to the UTC time, not IST | Late minutes wrong by the UTC offset | Build shift start in IST on the attendance date |
| 19 | `compute_late_minutes`, `int(...) > 10` | Compares truncated minutes, so 10 min 1 s is not late | 09:40:01 on a 09:30 shift gave 0, R2 says 10 | `seconds > 600`, then `seconds // 60` |
| 20 | `compute_work_hours`, `round()` | Float/banker's rounding, not half-up (R4) | 3 h 54 m 18 s = 3.905 gave 3.9, should be 3.91 | `Decimal` with `ROUND_HALF_UP` |
| 21 | `compute_overtime` | No 30-minute minimum (R3); `int()` is not floor | 5 minutes after shift end gave 5 | Floor minutes; count only if `>= 30` |
| 22 | `compute_overtime` | Overnight shift end not moved to the next day | 22:00-06:00 shift ended on the wrong day | `shift_end_at()` adds a day for overnight shifts |
| 23 | `list_attendance`, `list(find(q))` | Loads every matching document into memory | Would read ~100k documents per request | Sort, skip and limit in MongoDB |
| 24 | `list_attendance`, `docs.sort(...)` | Sorted by `date` only | Order within a day is undefined | Sort `date` desc, `emp_code` asc, backed by a matching index |
| 25 | `list_attendance` parameters | No validation | `status=FOO` and `date_from > date_to` returned 200 | `Status` literal and `date` types; explicit 422 check |
| 26 | `list_attendance` response | Leaks `_id`; datetimes not epoch ms; legacy records miss `half_day` / `history` | Response did not match `AttendanceRecord` | `record_out()` with epoch ms and defaults |

## Not a defect

- Storing `date` as a `"YYYY-MM-DD"` string: this format sorts the same way alphabetically and chronologically, so range filters on strings are correct.
- `IST = timezone(timedelta(hours=5, minutes=30))`: India has no daylight saving time, so a fixed offset is correct.
