-- Threads & messages
CREATE TABLE IF NOT EXISTS threads (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  thread_key VARCHAR(64) UNIQUE NOT NULL,
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  thread_id BIGINT NOT NULL,
  role ENUM('system','user','assistant','tool') NOT NULL,
  content MEDIUMTEXT NOT NULL,
  created_at TIMESTAMP DEFAULT now(),
  INDEX (thread_id, created_at),
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
);

-- Distilled episodic summaries
CREATE TABLE IF NOT EXISTS thread_summaries (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  thread_id BIGINT NOT NULL,
  segment_start_id BIGINT NOT NULL,
  segment_end_id BIGINT NOT NULL,
  summary TEXT NOT NULL,
  tokens INT NOT NULL,
  created_at TIMESTAMP DEFAULT now(),
  INDEX (thread_id, segment_end_id),
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
);

-- Profile facts (key/value with confidence)
CREATE TABLE IF NOT EXISTS profile_facts (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  subject VARCHAR(64) NOT NULL,
  k VARCHAR(128) NOT NULL,
  v TEXT NOT NULL,
  confidence TINYINT NOT NULL DEFAULT 80,
  source VARCHAR(64) NOT NULL,
  last_seen TIMESTAMP DEFAULT now(),
  UNIQUE KEY uniq_fact (subject,k)
);

-- Semantic memory (vectors kept separate from text)
CREATE TABLE IF NOT EXISTS memory_chunks (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  thread_id BIGINT NULL,
  kind ENUM('message','summary','doc','note') NOT NULL,
  text MEDIUMTEXT NOT NULL,
  embedding LONGBLOB NULL,
  created_at TIMESTAMP DEFAULT now(),
  INDEX (thread_id, kind, created_at),
  FULLTEXT INDEX ft_text (text)
);