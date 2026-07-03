PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  ilink_user_id TEXT NOT NULL UNIQUE,
  default_to_user_id TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  device_name TEXT NOT NULL DEFAULT '',
  agent_token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  UNIQUE(user_id, device_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ilink_credentials (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  bot_id TEXT NOT NULL DEFAULT '',
  bot_token_hash TEXT NOT NULL UNIQUE,
  bot_token_encrypted TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ilink_credentials_user_active
  ON ilink_credentials(user_id, active, updated_at);

CREATE TABLE IF NOT EXISTS ilink_contexts (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  to_user_id TEXT NOT NULL,
  context_token_hash TEXT NOT NULL,
  context_token_encrypted TEXT NOT NULL,
  last_inbound_at TEXT NOT NULL,
  context_expires_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(user_id, to_user_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ilink_contexts_user_expire
  ON ilink_contexts(user_id, context_expires_at);

CREATE TABLE IF NOT EXISTS notify_logs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  device_id TEXT,
  to_user_id TEXT,
  text_preview TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 0,
  ilink_ret TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notify_logs_user_time
  ON notify_logs(user_id, created_at);
