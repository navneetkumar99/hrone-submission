# Engineering Decisions

1. **Indexes:** I created a unique index on `employees.emp_code` and `(department, joined_on)` for department filtering. Attendance uses unique `(emp_code, date)` for duplicate prevention and employee-date lookups, plus `(date, emp_code)` for date filtering. I considered a standalone status index but rejected it because status has low selectivity.

2. **Punch-in race:** Both requests can initially see no matching record. Both attempt insertion, but MongoDB's unique index permits only one. The successful request returns 201. The other receives a `DuplicateKeyError`, which my code catches and converts into HTTP 409.

3. **Ties:** I use MongoDB's `$rank` window function. Employees with equal late minutes receive the same rank, with subsequent ranks skipped. Filtering by rank includes everyone tied at the cutoff. Tied results are ordered by employee code.

4. **Headcount:** Department aggregation starts from the employees collection. A lookup attaches attendance logs while preserving employees without matching records. Everyone who joined by the period end contributes to headcount.

5. **100x scale:** I would benchmark production-sized datasets, review execution plans and introduce incremental daily or monthly summaries to reduce repeated aggregation work.