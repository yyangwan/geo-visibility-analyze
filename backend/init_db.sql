-- 智见 (ZhiJian) - Database Init Script
-- For Tencent Cloud MySQL 8.0
-- Character set: utf8mb4

CREATE DATABASE IF NOT EXISTS aiscope
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE aiscope;

-- ============================================
-- 1. Users
-- ============================================
CREATE TABLE IF NOT EXISTS users (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  username        VARCHAR(100)   NOT NULL,
  hashed_password VARCHAR(200)   NOT NULL,
  is_active       TINYINT(1)     NOT NULL DEFAULT 1,
  created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 2. Projects
-- ============================================
CREATE TABLE IF NOT EXISTS projects (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  name             VARCHAR(200)   NOT NULL,
  industry         VARCHAR(100)   NOT NULL DEFAULT 'insurance',
  product_category VARCHAR(200)   NOT NULL DEFAULT '',
  user_id          INT            NOT NULL,
  created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_projects_user_id (user_id),
  CONSTRAINT fk_projects_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 3. Brands
-- ============================================
CREATE TABLE IF NOT EXISTS brands (
  id             INT AUTO_INCREMENT PRIMARY KEY,
  project_id     INT            NOT NULL,
  name           VARCHAR(200)   NOT NULL,
  aliases        JSON,
  is_competitor  TINYINT(1)     NOT NULL DEFAULT 0,
  INDEX ix_brands_project_id (project_id),
  CONSTRAINT fk_brands_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 4. Prompts
-- ============================================
CREATE TABLE IF NOT EXISTS prompts (
  id               INT AUTO_INCREMENT PRIMARY KEY,
  project_id       INT            NOT NULL,
  text             TEXT           NOT NULL,
  category         ENUM('recommend','compare','evaluate','scenario') NOT NULL DEFAULT 'recommend',
  is_auto_generated TINYINT(1)    NOT NULL DEFAULT 1,
  INDEX ix_prompts_project_id (project_id),
  CONSTRAINT fk_prompts_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 5. Audits
-- ============================================
CREATE TABLE IF NOT EXISTS audits (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  project_id      INT            NOT NULL,
  status          ENUM('pending','running','completed','failed','partial') NOT NULL DEFAULT 'pending',
  platforms_json  JSON,
  created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at    DATETIME       NULL,
  error_message   TEXT           NULL,
  INDEX ix_audits_project_id (project_id),
  CONSTRAINT fk_audits_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 6. Platform Response Records (NEW)
-- ============================================
CREATE TABLE IF NOT EXISTS platform_response_records (
  id                 INT AUTO_INCREMENT PRIMARY KEY,
  audit_id           INT            NOT NULL,
  prompt_id          INT            NOT NULL,
  platform           VARCHAR(50)    NOT NULL,
  response_text      TEXT           NULL,
  citations          JSON,
  prompt_tokens      INT            NOT NULL DEFAULT 0,
  completion_tokens  INT            NOT NULL DEFAULT 0,
  response_model     VARCHAR(100)   NOT NULL DEFAULT '',
  finish_reason      VARCHAR(20)    NOT NULL DEFAULT '',
  search_enabled     TINYINT(1)     NOT NULL DEFAULT 0,
  error              TEXT           NULL,
  created_at         DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_prr_audit_id (audit_id),
  UNIQUE INDEX ix_prr_unique (audit_id, prompt_id, platform),
  CONSTRAINT fk_prr_audit  FOREIGN KEY (audit_id)  REFERENCES audits(id)  ON DELETE CASCADE,
  CONSTRAINT fk_prr_prompt FOREIGN KEY (prompt_id) REFERENCES prompts(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 7. Query Results
-- ============================================
CREATE TABLE IF NOT EXISTS query_results (
  id                   INT AUTO_INCREMENT PRIMARY KEY,
  audit_id             INT            NOT NULL,
  prompt_id            INT            NOT NULL,
  brand_id             INT            NOT NULL,
  platform             VARCHAR(50)    NOT NULL,
  response_text        TEXT           NULL,
  mention_found        TINYINT(1)     NOT NULL DEFAULT 0,
  mention_position     INT            NULL,
  mention_context      TEXT           NULL,
  mention_confidence   FLOAT          NULL,
  is_recommended       TINYINT(1)     NOT NULL DEFAULT 0,
  recommendation_rank  INT            NULL,
  error                TEXT           NULL,
  created_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_query_results_audit_id (audit_id),
  INDEX ix_query_results_brand_id (brand_id),
  INDEX ix_query_results_platform (platform),
  CONSTRAINT fk_query_results_audit FOREIGN KEY (audit_id) REFERENCES audits(id) ON DELETE CASCADE,
  CONSTRAINT fk_query_results_prompt FOREIGN KEY (prompt_id) REFERENCES prompts(id) ON DELETE CASCADE,
  CONSTRAINT fk_query_results_brand  FOREIGN KEY (brand_id)  REFERENCES brands(id)  ON DELETE CASCADE,
  response_record_id INT NULL,
  CONSTRAINT fk_query_results_prr FOREIGN KEY (response_record_id) REFERENCES platform_response_records(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 7.5 Source Citations (NEW)
-- ============================================
CREATE TABLE IF NOT EXISTS source_citations (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  project_id      INT            NOT NULL,
  audit_id        INT            NULL,
  domain          VARCHAR(200)   NOT NULL,
  urls            JSON,
  citation_count  INT            NOT NULL DEFAULT 1,
  platform        VARCHAR(50)    NOT NULL,
  created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_source_domain (domain),
  INDEX ix_source_project_audit (project_id, audit_id),
  UNIQUE INDEX ix_source_unique (project_id, audit_id, domain, platform),
  CONSTRAINT fk_sc_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
  CONSTRAINT fk_sc_audit   FOREIGN KEY (audit_id)   REFERENCES audits(id)  ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 7.6 Response Analyses (NEW)
-- ============================================
CREATE TABLE IF NOT EXISTS response_analyses (
  id                   INT AUTO_INCREMENT PRIMARY KEY,
  response_record_id   INT            NOT NULL,
  cited_sources        JSON,
  brand_sentiment      VARCHAR(20)    NULL,
  brand_attributes     JSON,
  topics_covered       JSON,
  answer_structure     VARCHAR(20)    NULL,
  competitor_refs      JSON,
  analysis_model       VARCHAR(100)   NOT NULL DEFAULT '',
  status               VARCHAR(20)   NOT NULL DEFAULT 'pending',
  created_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_ra_response_record_id (response_record_id),
  UNIQUE INDEX ix_ra_prr_unique (response_record_id),
  CONSTRAINT fk_ra_prr FOREIGN KEY (response_record_id) REFERENCES platform_response_records(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 8. Reports
-- ============================================
CREATE TABLE IF NOT EXISTS reports (
  id                      INT AUTO_INCREMENT PRIMARY KEY,
  project_id              INT            NOT NULL,
  audit_id                INT            NOT NULL,
  overall_score           FLOAT          NOT NULL DEFAULT 0,
  mention_rate            FLOAT          NOT NULL DEFAULT 0,
  competitor_rank         INT            NULL,
  sentiment_positive_rate FLOAT          NULL,
  platform_scores         JSON,
  insights                JSON,
  created_at              DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_reports_project_id (project_id),
  UNIQUE INDEX ix_reports_audit_id (audit_id),
  CONSTRAINT fk_reports_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
  CONSTRAINT fk_reports_audit   FOREIGN KEY (audit_id)   REFERENCES audits(id)   ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 9. Suggestions
-- ============================================
CREATE TABLE IF NOT EXISTS suggestions (
  id           INT AUTO_INCREMENT PRIMARY KEY,
  project_id   INT            NOT NULL,
  report_id    INT            NOT NULL,
  category     VARCHAR(50)    NOT NULL,
  title        VARCHAR(200)   NOT NULL,
  description  TEXT           NOT NULL,
  priority     VARCHAR(20)    NOT NULL DEFAULT 'medium',
  is_resolved  TINYINT(1)     NOT NULL DEFAULT 0,
  created_at   DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_suggestions_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
  CONSTRAINT fk_suggestions_report  FOREIGN KEY (report_id)  REFERENCES reports(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================
-- 10. Scheduled Jobs
-- ============================================
CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  project_id      INT            NOT NULL,
  cron_expression VARCHAR(100)   NOT NULL,
  platforms_json  JSON,
  is_active       TINYINT(1)     NOT NULL DEFAULT 1,
  last_run_at     DATETIME       NULL,
  last_audit_id   INT            NULL,
  created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_scheduled_jobs_project FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
