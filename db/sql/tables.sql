-- conversations + messages
CREATE TABLE IF NOT EXISTS conversations (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  type         VARCHAR(32) NOT NULL DEFAULT 'chat',
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  conversation_id BIGINT NOT NULL,
  role            ENUM('system','user','assistant','tool') NOT NULL,
  content         MEDIUMTEXT NOT NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_messages_conversation_id_created_at (conversation_id, created_at),
  CONSTRAINT fk_messages_conversation
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
    ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS logs (
  id       	BIGINT AUTO_INCREMENT PRIMARY KEY,
  timestamp TIME,
  type 		TEXT,
  message 	TEXT,
  args 		TEXT
);
