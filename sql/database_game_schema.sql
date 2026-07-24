USE hongming_ai;

CREATE TABLE IF NOT EXISTS ai_game_reports (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  mandate JSON NOT NULL,
  master_plan JSON NOT NULL,
  public_context JSON NOT NULL,
  scenario_parameters JSON NOT NULL,
  deal_box JSON NOT NULL,
  red_team_review JSON NOT NULL,
  decision_report JSON NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_game_report_run (run_id),
  KEY idx_game_report_project (project_id, created_at),
  CONSTRAINT fk_game_report_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS ai_game_full_reports (
  id BIGINT NOT NULL AUTO_INCREMENT,
  run_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  structured_report JSON NOT NULL,
  markdown_report LONGTEXT NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_game_full_report_run (run_id),
  KEY idx_game_full_report_project (project_id, created_at),
  CONSTRAINT fk_game_full_report_run FOREIGN KEY (run_id) REFERENCES ai_pipeline_runs(id)
) ENGINE=InnoDB;
