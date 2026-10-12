\# DECISIONS.md



1\. \*\*Indexes.\*\* I created a unique index on `employees.emp\_code` to prevent duplicate employee codes. The unique `(emp\_code, date)` index on attendance logs prevents multiple attendance records for the same employee on one date. Other indexes support department, joining-date, attendance-date, employee, and status queries. I would reject a separate attendance index on only `emp\_code` because the existing `(emp\_code, date)` index already supports queries using `emp\_code`.



2\. \*\*Punch-in race.\*\* Two requests may check the employee at the same time and attempt to insert attendance records. MongoDB’s unique index allows only one record for that employee and date. The first insert succeeds; the other raises `DuplicateKeyError`, which the endpoint converts to HTTP 409.



3\. \*\*Ties.\*\* The leaderboard uses `$rank` based on total late minutes. Employees with equal totals receive the same rank. Therefore, if a tie occurs at the `limit` cutoff, the response may contain more than `limit` employees. The final results are sorted by employee code to order ties consistently.



4\. \*\*Headcount.\*\* The department summary starts with employees and looks up their attendance logs. It preserves employees with no matching logs and counts distinct employee codes.



5\. \*\*100x scale.\*\* I would load-test analytics endpoints, inspect MongoDB query plans, and optimize indexes based on measured query performance.

