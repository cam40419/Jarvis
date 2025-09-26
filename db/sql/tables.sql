-- scripts/bootstrap.sql
CREATE TABLE IF NOT EXISTS conversations (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  type VARCHAR(64) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW() ON UPDATE NOW()
);

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  conversation_id BIGINT NOT NULL,
  role ENUM('system','user','assistant','tool') NOT NULL,
  content MEDIUMTEXT NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  INDEX (conversation_id, created_at),
  FOREIGN KEY (conversation_id) REFERENCES conversations(ID)
);

-- CREATE TABLE IF NOT EXISTS profile_facts (
--   id BIGINT PRIMARY KEY AUTO_INCREMENT,
--   subject VARCHAR(128) NOT NULL,
--   k VARCHAR(128) NOT NULL,
--   v TEXT NOT NULL,
--   confidence TINYINT NOT NULL,
--   source VARCHAR(32) NOT NULL,
--   last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--   UNIQUE KEY uniq_fact (subject, k)
-- );

-- CREATE TABLE IF NOT EXISTS memory_chunks (
--   id BIGINT PRIMARY KEY AUTO_INCREMENT,
--   thread_id BIGINT,
--   kind VARCHAR(32) NOT NULL,
--   text MEDIUMTEXT NOT NULL,
--   embedding LONGBLOB NULL,
--   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--   INDEX(thread_id, created_at)
-- );

-- CREATE TABLE IF NOT EXISTS contacts (
--   contact_id BIGINT PRIMARY KEY AUTO_INCREMENT,
--   display_name VARCHAR(255),
--   email VARCHAR(255) UNIQUE,
--   org VARCHAR(255),
--   tags JSON NULL,
--   last_interacted_at TIMESTAMP NULL
-- );

-- CREATE TABLE IF NOT EXISTS links (
--   link_id BIGINT PRIMARY KEY AUTO_INCREMENT,
--   url TEXT,
--   title TEXT,
--   notes TEXT,
--   source_thread_id BIGINT,
--   first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--   last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
--   confidence DECIMAL(3,2),
--   UNIQUE KEY uniq_url (url(255))
-- );

-- CREATE TABLE IF NOT EXISTS files (
--   file_id VARCHAR(32) PRIMARY KEY,
--   filename TEXT,
--   mime VARCHAR(128),
--   size_bytes BIGINT,
--   storage_uri TEXT,
--   hash_sha256 CHAR(64),
--   source_thread_id BIGINT,
--   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
-- );