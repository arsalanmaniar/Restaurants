-- Runs once, the first time the postgres volume is created.
-- A dedicated database for pytest, so a test run can never touch dev data.
CREATE DATABASE abhiaya_test OWNER abhiaya;
