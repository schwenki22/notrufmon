-- notrufmon schema (idempotent; applied at every start)
SELECT pg_advisory_xact_lock(911112);

-- a site = one probe (Raspberry Pi + gateway) somewhere in the country
CREATE TABLE IF NOT EXISTS sites (
  id           text PRIMARY KEY,
  name         text NOT NULL,
  region       text NOT NULL DEFAULT '',
  enabled      boolean NOT NULL DEFAULT true,
  token_hash   text UNIQUE,
  testcall_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- a line = one calling number in one network, tested from a site
CREATE TABLE IF NOT EXISTS lines (
  id             serial PRIMARY KEY,
  site_id        text NOT NULL REFERENCES sites(id) ON DELETE CASCADE ON UPDATE CASCADE,
  position       integer NOT NULL DEFAULT 0,
  label          text NOT NULL,
  kind           text NOT NULL DEFAULT 'voip',          -- voip | mobile
  network        text NOT NULL DEFAULT '',              -- POST, Orange, Tango, VoIP provider name
  cli            text NOT NULL,                          -- calling number as it must arrive (E.164)
  target         text NOT NULL DEFAULT '112',
  enabled        boolean NOT NULL DEFAULT true,
  sip_domain     text NOT NULL,
  sip_port       integer NOT NULL DEFAULT 5060,
  sip_proxy      text,
  username       text NOT NULL,
  password       text NOT NULL DEFAULT '',
  auth_username  text,
  register       boolean NOT NULL DEFAULT true,          -- false: gateway / IP trunk without REGISTER
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lines_site ON lines (site_id, position);

-- one row per test: announced by the probe, evaluated when both sides reported
CREATE TABLE IF NOT EXISTS tests (
  id            uuid PRIMARY KEY,
  time          timestamptz NOT NULL,                    -- announcement
  site_id       text NOT NULL,
  line_id       integer,
  line_label    text,
  kind          text,
  network       text,
  expected_cli  text,
  result        text NOT NULL,                           -- ok | no_audio | wrong_cli | not_delivered | misrouted | probe_error
  reason        text,
  available     boolean NOT NULL,
  counted       boolean NOT NULL,
  sip_code      integer,
  setup_ms      real,
  delivered_cli text,
  audio_up      boolean,
  audio_down    boolean,
  finished_at   timestamptz,
  probe         jsonb,
  call_in       jsonb
);
CREATE INDEX IF NOT EXISTS tests_time ON tests (time DESC);
CREATE INDEX IF NOT EXISTS tests_line_time ON tests (line_id, time DESC);

-- every call that reached the termination, matched or not
CREATE TABLE IF NOT EXISTS calls_in (
  time        timestamptz NOT NULL,
  call_id     text,
  src         text,
  cli         text[],
  dialed      text,
  test_id     uuid,
  answered    boolean,
  duration_s  real,
  audio_ok    boolean,
  raw         jsonb
);
CREATE INDEX IF NOT EXISTS calls_in_time ON calls_in (time DESC);

-- sign of life of the probes (every minute)
CREATE TABLE IF NOT EXISTS probe_health (
  time        timestamptz NOT NULL,
  site_id     text NOT NULL,
  version     text,
  remote_ip   text,
  local_ip    text,
  uptime_s    real,
  outbox      integer,
  lines       jsonb                                       -- per line: registered, error
);
CREATE INDEX IF NOT EXISTS probe_health_site_time ON probe_health (site_id, time DESC);

CREATE TABLE IF NOT EXISTS settings (
  key   text PRIMARY KEY,
  value text NOT NULL
);
