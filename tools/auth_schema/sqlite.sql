-- zkCEX auth + KYC persistence (SQLite backend).
-- This is the canonical SQLite-flavored schema. Idempotent: every CREATE
-- uses IF NOT EXISTS so init_db() is safe to call on every server start.
-- The Postgres mirror is in postgres.sql.

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  pw_hash BLOB NOT NULL,
  pw_salt BLOB NOT NULL,
  name TEXT NOT NULL,
  opex_user TEXT NOT NULL,
  kyc_status TEXT NOT NULL DEFAULT 'none',
  kyc_verified_at INTEGER,
  kyc_name TEXT,
  kyc_phone TEXT,
  kyc_birth TEXT,
  kyc_gender TEXT,
  kyc_carrier TEXT,
  created_at INTEGER NOT NULL,
  kyc_provider_request_id TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kyc_verifications (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  carrier TEXT,
  name TEXT,
  rrn_front TEXT,
  rrn_back1 TEXT,
  phone TEXT,
  created_at INTEGER NOT NULL,
  sms_provider_message_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_kyc_verifications_user ON kyc_verifications(user_id);

CREATE TABLE IF NOT EXISTS sumsub_applicants (
  external_user_id TEXT PRIMARY KEY,
  applicant_id TEXT NOT NULL,
  level_name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_status TEXT,
  last_review_answer TEXT,
  last_synced_at INTEGER
);

CREATE TABLE IF NOT EXISTS sumsub_webhooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  applicant_id TEXT NOT NULL,
  type TEXT,
  body_json TEXT,
  received_at INTEGER NOT NULL,
  signature_valid INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sumsub_applicants_aid ON sumsub_applicants(applicant_id);
CREATE INDEX IF NOT EXISTS idx_sumsub_webhooks_aid ON sumsub_webhooks(applicant_id);

-- Geo-blocking audit trail. Last octet of the IP is redacted at write-time
-- (see geo_provider.redact_ip) so we never persist a full client IP -- this
-- is enough for diagnostics ("which network sent the most rejected
-- requests this week") without being GDPR-grade PII. country may be NULL
-- when the lookup fell through every source.
CREATE TABLE IF NOT EXISTS geo_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  ip_redacted TEXT NOT NULL,
  country TEXT,
  endpoint TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_geo_decisions_ts ON geo_decisions(ts);

-- TOTP recovery codes: 10 single-use 10-char codes per enabled account.
-- Hashed with PBKDF2-SHA256 + per-code salt; the plaintext is shown to the
-- user exactly once at setup.
CREATE TABLE IF NOT EXISTS totp_recovery_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash BLOB NOT NULL,
  code_salt BLOB NOT NULL,
  used_at INTEGER,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_totp_recovery_user ON totp_recovery_codes(user_id);

-- Rolling audit + rate-limit window for TOTP verification attempts. 5 wrong
-- in 15 min -> 30 min lock; see auth_server.h_totp_verify().
CREATE TABLE IF NOT EXISTS totp_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  attempted_at INTEGER NOT NULL,
  result TEXT NOT NULL,
  ip_redacted TEXT
);
CREATE INDEX IF NOT EXISTS idx_totp_attempts_user ON totp_attempts(user_id, attempted_at);

-- WebAuthn / passkey credentials. One row per registered authenticator. The
-- public key is stored in COSE-encoded form (base64), so the verify path can
-- be implemented in any language without an extra parse step. sign_count
-- monotonically increases per assertion; a non-monotonic value is treated as
-- a possible clone and refused.
CREATE TABLE IF NOT EXISTS webauthn_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credential_id TEXT NOT NULL UNIQUE,
  public_key_cose_b64 TEXT NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  attestation_type TEXT,
  aaguid TEXT,
  transports TEXT,
  device_name TEXT,
  created_at INTEGER NOT NULL,
  last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);

-- Short-lived (5 min) challenge nonces issued by /begin endpoints. user_id is
-- present for register flows; email is set for the username-less
-- authenticate flow. purpose is 'register' or 'authenticate'. We mark
-- used=1 instead of deleting so a replayed POST gets a clear error.
CREATE TABLE IF NOT EXISTS webauthn_challenges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  challenge_b64 TEXT NOT NULL UNIQUE,
  purpose TEXT NOT NULL,
  user_id INTEGER,
  email TEXT,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webauthn_challenges_expires ON webauthn_challenges(expires_at);
