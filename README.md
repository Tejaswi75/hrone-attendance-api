# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    export MONGO_URI=mongodb://localhost:27017    # or put both in a local .env
    export MONGO_DB=attendance_db
    uvicorn app.main:app --port 8000

Requires Python 3.11+ and MongoDB 6.0+. Indexes are created at startup (idempotent).
`python sample_seed.py` loads the nine sample documents.

## Notes

- Status: complete; every endpoint in `openapi.yaml` is implemented.
- Trend: the 92-day limit counts days inclusively (`from` to `to` may span at most 92 days).
- PATCH: "changes nothing" is judged on the client-editable fields (`status`, `punch_in`, `punch_out`).
  Concurrent corrections are guarded by an optimistic check on the record's current state; the loser gets 409.
