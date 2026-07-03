PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memberships (
  user_id TEXT PRIMARY KEY,
  plan TEXT NOT NULL DEFAULT 'trial',
  status TEXT NOT NULL DEFAULT 'active',
  trial_started_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  paid_until TEXT,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memberships_expire
  ON memberships(status, expires_at);

CREATE INDEX IF NOT EXISTS idx_memberships_plan
  ON memberships(plan, status);

CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  plan TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'CNY',
  provider TEXT NOT NULL DEFAULT 'manual',
  provider_order_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  paid_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_orders_user_time
  ON orders(user_id, created_at);

CREATE INDEX IF NOT EXISTS idx_orders_provider_order
  ON orders(provider, provider_order_id);

-- 给已经存在的用户补一份 7 天试用。这里使用迁移执行时间作为起点，避免老用户一上线就过期。
INSERT INTO memberships (user_id, plan, status, trial_started_at, expires_at, paid_until, updated_at)
SELECT id, 'trial', 'active', strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now','+7 days'), NULL, strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM users
WHERE id NOT IN (SELECT user_id FROM memberships);
