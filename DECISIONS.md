# DECISIONS.md

1. **Indexes.** All are created in `create_indexes()` at startup. `employees.uniq_emp_code` (unique) blocks duplicate codes and serves every employee lookup. `attendance_logs.uniq_emp_date` (unique) allows one record per employee per day and serves the monthly summary, PATCH and trend matches. `date_desc_emp` matches the list sort (date desc, emp_code asc), so nothing is sorted in memory. `emp_punch_in` finds the latest punch-in for punch-out. `dept_joined` and `joined_on` serve the department summary and trend headcount. I rejected a `status` index: five values, too unselective to help.

2. **Punch-in race.** No check-then-insert. Both requests compute the record and call `insert_one`. The unique (`emp_code`, `date`) index lets the first succeed; the second gets `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$setWindowFields` with `$rank` gives equal totals the same rank (1, 2, 2, 4). `rank <= limit` is applied after ranking, so every employee tied at the cutoff is returned, even if that exceeds `limit`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on` ≤ month end) and looks up their logs. No logs means an empty array, but the employee still counts.

5. **At 100x.** Keep pre-aggregated daily/monthly summaries (updated on punch-out/PATCH or by a scheduled `$merge`) so analytics stop scanning raw logs.
