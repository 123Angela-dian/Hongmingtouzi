CREATE DATABASE IF NOT EXISTS hongming_ai
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_0900_ai_ci;

USE hongming_ai;

CREATE TABLE IF NOT EXISTS ai_pipeline_runs (
  id BIGINT NOT NULL AUTO_INCREMENT,
  project_id BIGINT NOT NULL,
  run_type VARCHAR(64) NOT NULL,
  status ENUM('running','completed','failed') NOT NULL DEFAULT 'running',
  source_database VARCHAR(128) NOT NULL DEFAULT 'hongming01',
  prompt_version VARCHAR(64) NULL,
  model_name VARCHAR(128) NULL,
  input_counts JSON NULL,
  parameters JSON NULL,
  started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME NULL,
  error_message TEXT NULL,
  PRIMARY KEY (id),
  KEY idx_runs_project (project_id, started_at),
  KEY idx_runs_status (status)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_entities (
  id BIGINT NOT NULL AUTO_INCREMENT,
  project_id BIGINT NOT NULL,
  canonical_name VARCHAR(512) NOT NULL,
  normalized_name VARCHAR(512) NOT NULL,
  entity_type VARCHAR(64) NOT NULL DEFAULT 'unknown',
  credit_code VARCHAR(64) NULL,
  status ENUM('identified','pending','conflict','need_review') NOT NULL DEFAULT 'identified',
  source_method ENUM('rule','ai','rule_ai','manual') NOT NULL DEFAULT 'rule_ai',
  prompt_version VARCHAR(64) NULL,
  model_name VARCHAR(128) NULL,
  confidence DECIMAL(5,4) NULL,
  need_review TINYINT(1) NOT NULL DEFAULT 0,
  review_status ENUM('pending','reviewed','rejected','accepted') NOT NULL DEFAULT 'pending',
  run_id BIGINT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_entity_normalized (project_id, normalized_name),
  KEY idx_entity_project_type (project_id, entity_type),
  KEY idx_entity_review (need_review, review_status),
  CONSTRAINT fk_entity_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_entity_aliases (
  id BIGINT NOT NULL AUTO_INCREMENT,
  project_id BIGINT NOT NULL,
  entity_id BIGINT NOT NULL,
  alias_name VARCHAR(512) NOT NULL,
  normalized_alias VARCHAR(512) NOT NULL,
  alias_type VARCHAR(64) NOT NULL DEFAULT 'mention',
  source_evidence_id BIGINT NULL,
  source_event_id BIGINT NULL,
  source_method ENUM('rule','ai','rule_ai','manual') NOT NULL DEFAULT 'rule_ai',
  confidence DECIMAL(5,4) NULL,
  need_review TINYINT(1) NOT NULL DEFAULT 0,
  review_status ENUM('pending','reviewed','rejected','accepted') NOT NULL DEFAULT 'pending',
  run_id BIGINT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_alias_normalized (project_id, normalized_alias),
  KEY idx_alias_entity (entity_id),
  KEY idx_alias_review (need_review, review_status),
  CONSTRAINT fk_alias_entity FOREIGN KEY (entity_id) REFERENCES ai_entities(id),
  CONSTRAINT fk_alias_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_event_entities (
  id BIGINT NOT NULL AUTO_INCREMENT,
  project_id BIGINT NOT NULL,
  event_id BIGINT NOT NULL,
  entity_id BIGINT NOT NULL,
  entity_role VARCHAR(64) NOT NULL,
  source_field ENUM('subject','object','summary','evidence') NOT NULL,
  source_text VARCHAR(512) NULL,
  source_method ENUM('rule','ai','rule_ai','manual') NOT NULL DEFAULT 'rule_ai',
  confidence DECIMAL(5,4) NULL,
  need_review TINYINT(1) NOT NULL DEFAULT 0,
  review_status ENUM('pending','reviewed','rejected','accepted') NOT NULL DEFAULT 'pending',
  run_id BIGINT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_event_entity_role (project_id, event_id, entity_id, entity_role, source_field),
  KEY idx_event_entity_event (event_id),
  KEY idx_event_entity_entity (entity_id),
  CONSTRAINT fk_event_entity_entity FOREIGN KEY (entity_id) REFERENCES ai_entities(id),
  CONSTRAINT fk_event_entity_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_event_evidences (
  id BIGINT NOT NULL AUTO_INCREMENT,
  project_id BIGINT NOT NULL,
  event_id BIGINT NOT NULL,
  evidence_id BIGINT NOT NULL,
  relation_type ENUM('supporting','conflicting') NOT NULL,
  relation_note TEXT NULL,
  match_reasons JSON NULL,
  source_method ENUM('rule','ai','rule_ai','manual') NOT NULL DEFAULT 'rule_ai',
  prompt_version VARCHAR(64) NULL,
  model_name VARCHAR(128) NULL,
  confidence DECIMAL(5,4) NULL,
  need_review TINYINT(1) NOT NULL DEFAULT 0,
  review_status ENUM('pending','reviewed','rejected','accepted') NOT NULL DEFAULT 'pending',
  run_id BIGINT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_ai_event_evidence (project_id, event_id, evidence_id),
  KEY idx_ai_ee_event (event_id),
  KEY idx_ai_ee_evidence (evidence_id),
  KEY idx_ai_ee_review (need_review, review_status),
  CONSTRAINT fk_ai_ee_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_review_queue (
  id BIGINT NOT NULL AUTO_INCREMENT,
  project_id BIGINT NOT NULL,
  review_type VARCHAR(64) NOT NULL,
  target_table VARCHAR(128) NOT NULL,
  target_id BIGINT NOT NULL,
  issue_type VARCHAR(128) NOT NULL,
  issue_summary TEXT NOT NULL,
  priority ENUM('low','medium','high','critical') NOT NULL DEFAULT 'medium',
  status ENUM('pending','accepted','rejected','resolved') NOT NULL DEFAULT 'pending',
  reviewer VARCHAR(128) NULL,
  review_note TEXT NULL,
  run_id BIGINT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  reviewed_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uk_review_target (project_id, review_type, target_table, target_id, issue_type),
  KEY idx_review_project_status (project_id, status, priority),
  CONSTRAINT fk_review_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_reports (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  pcs_score INT NULL,
  pcs_breakdown JSON NULL,
  asset_patches JSON NULL,
  economic_patches JSON NULL,
  legal_patches JSON NULL,
  financial_patches JSON NULL,
  master_plan JSON NULL,
  coverage_report JSON NULL,
  final_report LONGTEXT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_report_run (run_id),
  KEY idx_report_project (project_id, created_at),
  CONSTRAINT fk_report_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE OR REPLACE VIEW v_ai_event_all_evidences AS
SELECT
  ee.project_id,
  ee.event_id,
  ee.evidence_id,
  ee.relation_type,
  ee.relation_note,
  ee.source_method,
  ee.confidence,
  ee.need_review,
  ee.review_status,
  'source' AS relation_source
FROM hongming01.event_evidences ee
UNION ALL
SELECT
  aee.project_id,
  aee.event_id,
  aee.evidence_id,
  aee.relation_type,
  aee.relation_note,
  aee.source_method,
  aee.confidence,
  aee.need_review,
  aee.review_status,
  'ai' AS relation_source
FROM hongming_ai.ai_event_evidences aee;

CREATE OR REPLACE VIEW v_ai_entity_timeline AS
SELECT
  aee.project_id,
  ae.id AS entity_id,
  ae.canonical_name,
  ae.entity_type,
  aee.entity_role,
  e.id AS event_id,
  e.event_type,
  e.event_date,
  e.event_date_text,
  e.action,
  e.object,
  e.summary,
  aee.confidence,
  aee.need_review,
  aee.review_status
FROM hongming_ai.ai_event_entities aee
JOIN hongming_ai.ai_entities ae ON ae.id = aee.entity_id
JOIN hongming01.events e ON e.id = aee.event_id;

CREATE OR REPLACE VIEW v_ai_event_full_details AS
SELECT
  e.id AS event_id,
  e.project_id,
  e.event_type,
  e.event_date,
  e.event_date_text,
  e.subject AS source_subject,
  e.action,
  e.object AS source_object,
  e.amount,
  e.amount_unit,
  e.summary,
  GROUP_CONCAT(DISTINCT CONCAT(ae.canonical_name, ':', aee.entity_role) SEPARATOR ' | ') AS standardized_entities,
  GROUP_CONCAT(DISTINCT CONCAT(all_ev.evidence_id, ':', all_ev.relation_type, ':', all_ev.relation_source) SEPARATOR ' | ') AS evidence_relations
FROM hongming01.events e
LEFT JOIN hongming_ai.ai_event_entities aee ON aee.event_id = e.id
LEFT JOIN hongming_ai.ai_entities ae ON ae.id = aee.entity_id
LEFT JOIN hongming_ai.v_ai_event_all_evidences all_ev ON all_ev.event_id = e.id
GROUP BY e.id, e.project_id, e.event_type, e.event_date, e.event_date_text,
         e.subject, e.action, e.object, e.amount, e.amount_unit, e.summary;
