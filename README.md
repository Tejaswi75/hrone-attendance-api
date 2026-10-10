# Employee Attendance & Analytics API

FastAPI + MongoDB.

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set MONGO_URI / MONGO_DB
python sample_seed.py       # optional: loads the 9 sample docs
uvicorn app.main:app --port 8000
```
Requires MongoDB 6.0+. Indexes are created at startup.

## Notes
<!-- TODO: anything the reviewers should know; list anything missing -->
