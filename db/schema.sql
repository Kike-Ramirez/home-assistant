-- =============================================================================
-- Home AI Assistant — Postgres schema
-- Designed to be served by PostgREST (Barbara Marketplace).
-- Conventions: see CLAUDE.md section 7 (JSONB, taxonomy, tenant_id,
-- ISA95 hierarchy, user role/scope, compatibility graph).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS home;

-- -----------------------------------------------------------------------------
-- Role (PostgREST: PGRST_DB_SCHEMA=home, PGRST_DB_ANON_ROLE=app_service)
-- -----------------------------------------------------------------------------
-- Home project, a single user, trusted internal network on the Barbara node:
-- there's NO JWT and no anon/authenticated distinction. PostgREST is
-- configured so that EVERY request uses the app_service role directly — the
-- simple, correct choice for this MVP. If this ever needed real
-- multi-tenant/multi-user support, that's when it'd be worth adding JWT and a
-- second, privilege-less anon role — not before.
-- -----------------------------------------------------------------------------

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_service') THEN
    CREATE ROLE app_service NOLOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA home TO app_service;
ALTER DEFAULT PRIVILEGES IN SCHEMA home GRANT ALL ON TABLES TO app_service;
ALTER DEFAULT PRIVILEGES IN SCHEMA home GRANT ALL ON SEQUENCES TO app_service;

SET search_path TO home;

-- -----------------------------------------------------------------------------
-- Extensions
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()

-- -----------------------------------------------------------------------------
-- Tenant/RLS helper function. MVP phase: always 'home'. The policy is
-- already active so it doesn't need redesigning once real multi-tenancy shows up.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION home.current_tenant() RETURNS text AS $$
  SELECT coalesce(current_setting('request.jwt.claims', true)::json->>'tenant_id', 'home');
$$ LANGUAGE sql STABLE;

-- =============================================================================
-- 1. Device type taxonomy (separate from the instances table)
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.device_type (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       text NOT NULL DEFAULT 'home',
    code            text NOT NULL,               -- 'washing_machine', 'plc' (future), ...
    name            text NOT NULL,
    parent_type_id  uuid REFERENCES home.device_type(id),
    attributes_schema jsonb NOT NULL DEFAULT '{}'::jsonb, -- hint for the expected shape of device.attributes
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code)
);

-- =============================================================================
-- 2. Standards / protocols (Zigbee, Matter, WiFi, Modbus in the future, ...)
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.standard (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   text NOT NULL DEFAULT 'home',
    code        text NOT NULL,   -- 'zigbee', 'matter', 'wifi'
    name        text NOT NULL,
    UNIQUE (tenant_id, code)
);

-- =============================================================================
-- 3. Devices (instances). Location modeled as ISA95 (enterprise/site/area/line)
--    to line up with the Barbara Standard Data Model and the Industrial Data
--    Simulator — flat today (site=area=line=NULL or trivial values), a real
--    industrial hierarchy tomorrow.
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.device (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           text NOT NULL DEFAULT 'home',
    site_id             text NOT NULL DEFAULT 'home',
    device_type_id      uuid REFERENCES home.device_type(id),

    -- deviceDisplayName from the Barbara Standard Data Model / ISA95 topics
    display_name        text NOT NULL,

    -- ISA95 hierarchy (enterprise/site/area/line). In the home MVP:
    -- enterprise='home', site='home', area=<room>, line=NULL.
    isa95_enterprise    text NOT NULL DEFAULT 'home',
    isa95_site          text NOT NULL DEFAULT 'home',
    isa95_area          text,               -- e.g. 'kitchen'
    isa95_line          text,               -- not used in the home MVP

    brand               text,
    model               text,
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb, -- free-form specs (voltage, capacity, ...)
    status              text NOT NULL DEFAULT 'active',     -- active | retired
    owner_user_id       uuid,               -- FK added after creating home.app_user

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_device_tenant ON home.device (tenant_id);
CREATE INDEX IF NOT EXISTS idx_device_type ON home.device (device_type_id);
CREATE INDEX IF NOT EXISTS idx_device_attributes ON home.device USING gin (attributes);

-- =============================================================================
-- 4. Compatibility graph
--    a) device <-> standard/protocol it supports
--    b) device <-> device (explicit compatibility, e.g. "requires hub X")
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.device_standard (
    device_id   uuid NOT NULL REFERENCES home.device(id) ON DELETE CASCADE,
    standard_id uuid NOT NULL REFERENCES home.standard(id) ON DELETE CASCADE,
    PRIMARY KEY (device_id, standard_id)
);

CREATE TABLE IF NOT EXISTS home.device_compatibility (
    device_id_a     uuid NOT NULL REFERENCES home.device(id) ON DELETE CASCADE,
    device_id_b     uuid NOT NULL REFERENCES home.device(id) ON DELETE CASCADE,
    relation_type   text NOT NULL DEFAULT 'compatible', -- 'compatible' | 'requires' | 'replaces'
    notes           text,
    CHECK (device_id_a <> device_id_b),
    PRIMARY KEY (device_id_a, device_id_b, relation_type)
);

-- =============================================================================
-- 4b. Documents attached to a device (manuals, label photos, free-form notes)
--     — the `attach_document` tool Claude can call directly on a device.
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.device_document (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   text NOT NULL DEFAULT 'home',
    device_id   uuid NOT NULL REFERENCES home.device(id) ON DELETE CASCADE,
    kind        text NOT NULL,      -- 'photo' | 'manual' | 'note'
    url_or_ref  text NOT NULL,      -- public URL, data: URI, or a Files API reference
    description text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_device_document_device ON home.device_document (device_id);

-- =============================================================================
-- 5. Users (role + scope, even though there's only one today)
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.app_user (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       text NOT NULL DEFAULT 'home',
    channel         text NOT NULL,           -- 'telegram'
    channel_user_id text NOT NULL,           -- the user's id on that channel
    display_name    text,
    role            text NOT NULL DEFAULT 'owner', -- 'owner' | 'member' | (future) 'operator', 'engineer'
    scope           text NOT NULL DEFAULT 'home',  -- role scope: 'home' today, a specific site/area tomorrow
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, channel_user_id)
);

ALTER TABLE home.device
    ADD CONSTRAINT fk_device_owner FOREIGN KEY (owner_user_id) REFERENCES home.app_user(id);

-- =============================================================================
-- 6. Conversations and their state (outside the orchestrator's memory, so it
--    can scale to replicas — section 4).
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.conversation (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           text NOT NULL DEFAULT 'home',
    channel             text NOT NULL,
    channel_conversation_id text NOT NULL,   -- conversation_id from the MQTT contract (section 5)
    user_id             uuid REFERENCES home.app_user(id),
    intent              text,                -- 'device_onboarding' | 'troubleshooting' | 'course' | 'replacement'
    state               jsonb NOT NULL DEFAULT '{}'::jsonb, -- session context, quiz answers, etc.
    status              text NOT NULL DEFAULT 'open', -- open | closed
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, channel, channel_conversation_id)
);

CREATE TABLE IF NOT EXISTS home.message (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES home.conversation(id) ON DELETE CASCADE,
    direction       text NOT NULL,      -- 'inbound' | 'outbound'
    type            text NOT NULL,      -- 'text' | 'photo' | 'command' (same enum as the MQTT contract)
    content         text,
    attachments      jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_message_conversation ON home.message (conversation_id, created_at);

-- =============================================================================
-- 7. Reminders / alerts (notifier-scheduler)
-- =============================================================================
CREATE TABLE IF NOT EXISTS home.reminder (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       text NOT NULL DEFAULT 'home',
    device_id       uuid REFERENCES home.device(id) ON DELETE CASCADE,
    user_id         uuid REFERENCES home.app_user(id),
    kind            text NOT NULL,      -- 'maintenance' | 'price_alert' | 'firmware_update'
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    scheduled_at    timestamptz NOT NULL,
    recurrence_rule text,               -- optional RRULE for recurring reminders
    status          text NOT NULL DEFAULT 'pending', -- pending | sent | cancelled
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reminder_due ON home.reminder (status, scheduled_at);

-- =============================================================================
-- 8. RPC function: devices compatible with a given one.
--    Exposed by PostgREST as POST /rpc/compatible_devices
--    Combines: (a) explicit device_compatibility entries,
--              (b) devices sharing at least one standard.
-- =============================================================================
CREATE OR REPLACE FUNCTION home.compatible_devices(p_device_id uuid)
RETURNS TABLE (device_id uuid, display_name text, reason text) AS $$
    WITH explicit AS (
        SELECT dc.device_id_b AS device_id, 'explicit: ' || dc.relation_type AS reason
        FROM home.device_compatibility dc
        WHERE dc.device_id_a = p_device_id
        UNION
        SELECT dc.device_id_a AS device_id, 'explicit: ' || dc.relation_type AS reason
        FROM home.device_compatibility dc
        WHERE dc.device_id_b = p_device_id
    ),
    shared_standard AS (
        SELECT ds2.device_id, 'shared_standard: ' || s.code AS reason
        FROM home.device_standard ds1
        JOIN home.device_standard ds2 ON ds2.standard_id = ds1.standard_id AND ds2.device_id <> p_device_id
        JOIN home.standard s ON s.id = ds1.standard_id
        WHERE ds1.device_id = p_device_id
    )
    SELECT d.id, d.display_name, r.reason
    FROM (SELECT * FROM explicit UNION SELECT * FROM shared_standard) r
    JOIN home.device d ON d.id = r.device_id
    WHERE d.status = 'active';
$$ LANGUAGE sql STABLE;

GRANT EXECUTE ON FUNCTION home.compatible_devices(uuid) TO app_service;

-- =============================================================================
-- 9. RLS — enabled on every business table (section 7 principle).
--    MVP phase: a single tenant, 'home', so the policy just compares against
--    home.current_tenant(); once there's real multi-tenancy, all that's
--    needed is for PostgREST's JWT/claims to carry the right tenant_id.
-- =============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['device_type','standard','device','device_standard',
                              'device_compatibility','device_document','app_user',
                              'conversation','message','reminder']
    LOOP
        EXECUTE format('ALTER TABLE home.%I ENABLE ROW LEVEL SECURITY;', t);
    END LOOP;
END
$$;

-- device_standard, device_compatibility, and message don't have their own
-- tenant_id (they inherit the tenant through their device_id/conversation_id),
-- so their policy is based on the parent record's tenant.

CREATE POLICY tenant_isolation ON home.device_type
    USING (tenant_id = home.current_tenant());
CREATE POLICY tenant_isolation ON home.standard
    USING (tenant_id = home.current_tenant());
CREATE POLICY tenant_isolation ON home.device
    USING (tenant_id = home.current_tenant());
CREATE POLICY tenant_isolation ON home.device_document
    USING (tenant_id = home.current_tenant());
CREATE POLICY tenant_isolation ON home.app_user
    USING (tenant_id = home.current_tenant());
CREATE POLICY tenant_isolation ON home.conversation
    USING (tenant_id = home.current_tenant());
CREATE POLICY tenant_isolation ON home.reminder
    USING (tenant_id = home.current_tenant());

CREATE POLICY tenant_isolation ON home.device_standard
    USING (EXISTS (SELECT 1 FROM home.device d WHERE d.id = device_id AND d.tenant_id = home.current_tenant()));
CREATE POLICY tenant_isolation ON home.device_compatibility
    USING (EXISTS (SELECT 1 FROM home.device d WHERE d.id = device_id_a AND d.tenant_id = home.current_tenant()));
CREATE POLICY tenant_isolation ON home.message
    USING (EXISTS (SELECT 1 FROM home.conversation c WHERE c.id = conversation_id AND c.tenant_id = home.current_tenant()));

-- app_service is the only role with grants (see above) — the policies are
-- ready for the day there are multiple tenants behind that same role.
