-- Runs only when this dedicated local volume is initialized for the first time.
-- Development data and disposable concurrency fixtures never share a database.
CREATE DATABASE intelligence_ci_local CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON intelligence_ci_local.* TO 'intelligence_local'@'%';
