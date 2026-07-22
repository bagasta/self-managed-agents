-- RESET USER TOTAL: membuat user WhatsApp benar-benar dianggap user baru.
-- PostgreSQL. Jalankan seluruh script sekaligus.
--
-- Ganti nilai pada SATU tempat di bawah ini:
--     62895626765423
-- contoh:
--     62895626765423
--
-- Yang dihapus:
--   * user, subscription, top-up, dan link code
--   * semua agent milik user beserta data turunannya
--   * session Arthur/user, messages, runs, dan scheduled jobs
--   * scoped memory milik Arthur untuk user (termasuk last_agent_id/BeeChat)
--   * Google OAuth tokens dan pending OAuth states
--
-- Catatan: ini menghapus token OAuth lokal, tetapi tidak mencabut grant dari
-- halaman keamanan akun Google milik user.

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';

DROP TABLE IF EXISTS _reset_target;
DROP TABLE IF EXISTS _reset_identities;
DROP TABLE IF EXISTS _reset_user_ids;
DROP TABLE IF EXISTS _reset_agent_ids;
DROP TABLE IF EXISTS _reset_session_ids;
DROP TABLE IF EXISTS _reset_counts;

CREATE TEMP TABLE _reset_target (
    raw_input text NOT NULL,
    canonical_phone text NOT NULL
) ON COMMIT DROP;

-- Hanya bagian ini yang perlu diubah.
INSERT INTO _reset_target (raw_input, canonical_phone)
SELECT
    raw_input,
    CASE
        WHEN digits LIKE '0%' THEN '62' || substr(digits, 2)
        WHEN digits LIKE '8%' THEN '62' || digits
        ELSE digits
    END
FROM (
    SELECT
        btrim(raw_input) AS raw_input,
        regexp_replace(split_part(btrim(raw_input), '@', 1), '[^0-9]', '', 'g') AS digits
    FROM (VALUES ('62895626765423'::text)) input(raw_input)
) normalized;

DO $$
DECLARE
    target_phone text;
BEGIN
    SELECT canonical_phone INTO target_phone FROM _reset_target;
    IF target_phone IS NULL OR target_phone !~ '^62[0-9]{7,15}$' THEN
        RAISE EXCEPTION
            'Nomor target belum valid. Gunakan format 628xxxxxxxxxx pada 62895626765423';
    END IF;
END $$;

CREATE TEMP TABLE _reset_identities (
    identity text PRIMARY KEY
) ON COMMIT DROP;

CREATE TEMP TABLE _reset_user_ids (
    user_id uuid PRIMARY KEY
) ON COMMIT DROP;

CREATE TEMP TABLE _reset_agent_ids (
    agent_id uuid PRIMARY KEY
) ON COMMIT DROP;

CREATE TEMP TABLE _reset_session_ids (
    session_id uuid PRIMARY KEY
) ON COMMIT DROP;

CREATE TEMP TABLE _reset_counts (
    item text PRIMARY KEY,
    deleted_count bigint NOT NULL
) ON COMMIT DROP;

-- Seed semua variasi umum dari nomor yang diberikan.
INSERT INTO _reset_identities (identity)
SELECT DISTINCT candidate
FROM _reset_target target
CROSS JOIN LATERAL (
    VALUES
        (target.raw_input),
        (target.canonical_phone),
        ('+' || target.canonical_phone),
        ('0' || substr(target.canonical_phone, 3)),
        (target.canonical_phone || '@s.whatsapp.net'),
        (target.canonical_phone || '@c.us')
) variants(candidate)
WHERE candidate IS NOT NULL AND btrim(candidate) <> ''
ON CONFLICT DO NOTHING;

-- Temukan row user dari external_id, phone_number, atau WA LID.
INSERT INTO _reset_user_ids (user_id)
SELECT DISTINCT u.id
FROM users u
CROSS JOIN _reset_target target
CROSS JOIN LATERAL (
    VALUES (u.external_id), (u.phone_number), (u.wa_lid)
) user_identity(candidate)
WHERE candidate IS NOT NULL
  AND (
      btrim(candidate) IN (SELECT identity FROM _reset_identities)
      OR CASE
          WHEN regexp_replace(split_part(candidate, '@', 1), '[^0-9]', '', 'g') LIKE '0%'
              THEN '62' || substr(
                  regexp_replace(split_part(candidate, '@', 1), '[^0-9]', '', 'g'),
                  2
              )
          WHEN regexp_replace(split_part(candidate, '@', 1), '[^0-9]', '', 'g') LIKE '8%'
              THEN '62' || regexp_replace(split_part(candidate, '@', 1), '[^0-9]', '', 'g')
          ELSE regexp_replace(split_part(candidate, '@', 1), '[^0-9]', '', 'g')
      END = target.canonical_phone
  )
ON CONFLICT DO NOTHING;

-- Ambil semua alias yang pernah disimpan pada row user yang sama.
INSERT INTO _reset_identities (identity)
SELECT DISTINCT btrim(candidate)
FROM users u
JOIN _reset_user_ids target_user ON target_user.user_id = u.id
CROSS JOIN LATERAL (
    VALUES (u.external_id), (u.phone_number), (u.wa_lid)
) user_identity(candidate)
WHERE candidate IS NOT NULL AND btrim(candidate) <> ''
ON CONFLICT DO NOTHING;

-- Tambahkan bentuk JID/bare dari semua alias. Ini penting ketika users.wa_lid
-- menyimpan digit polos tetapi sessions.external_user_id menyimpan suffix @lid.
INSERT INTO _reset_identities (identity)
SELECT DISTINCT candidate
FROM (
    SELECT split_part(identity, '@', 1) AS bare
    FROM _reset_identities
) known
CROSS JOIN LATERAL (
    VALUES
        (known.bare),
        (known.bare || '@lid'),
        (known.bare || '@s.whatsapp.net'),
        (known.bare || '@c.us')
) variants(candidate)
WHERE candidate IS NOT NULL AND btrim(candidate) <> ''
ON CONFLICT DO NOTHING;

-- Tangkap agent milik user sebelum row apa pun dihapus.
INSERT INTO _reset_agent_ids (agent_id)
SELECT DISTINCT a.id
FROM agents a
CROSS JOIN _reset_target target
WHERE btrim(COALESCE(a.owner_external_id, '')) IN (
        SELECT identity FROM _reset_identities
    )
   OR CASE
        WHEN regexp_replace(
            split_part(COALESCE(a.owner_external_id, ''), '@', 1),
            '[^0-9]', '', 'g'
        ) LIKE '0%'
            THEN '62' || substr(
                regexp_replace(
                    split_part(COALESCE(a.owner_external_id, ''), '@', 1),
                    '[^0-9]', '', 'g'
                ), 2
            )
        WHEN regexp_replace(
            split_part(COALESCE(a.owner_external_id, ''), '@', 1),
            '[^0-9]', '', 'g'
        ) LIKE '8%'
            THEN '62' || regexp_replace(
                split_part(COALESCE(a.owner_external_id, ''), '@', 1),
                '[^0-9]', '', 'g'
            )
        ELSE regexp_replace(
            split_part(COALESCE(a.owner_external_id, ''), '@', 1),
            '[^0-9]', '', 'g'
        )
    END = target.canonical_phone
ON CONFLICT DO NOTHING;

-- Tangkap session pada Arthur maupun agent lain berdasarkan seluruh alias WA.
-- Session milik agent yang akan dihapus juga ikut ditangkap seluruhnya.
INSERT INTO _reset_session_ids (session_id)
SELECT DISTINCT s.id
FROM sessions s
CROSS JOIN _reset_target target
CROSS JOIN LATERAL (
    VALUES
        (s.external_user_id),
        (s.channel_config ->> 'phone_number'),
        (s.channel_config ->> 'user_phone'),
        (s.channel_config ->> 'from_phone'),
        (s.channel_config ->> 'sender_phone'),
        (s.channel_config ->> 'user_jid'),
        (s.channel_config ->> 'sender_jid'),
        (s.channel_config ->> 'wa_lid')
) session_identity(candidate)
WHERE s.agent_id IN (SELECT agent_id FROM _reset_agent_ids)
   OR btrim(COALESCE(candidate, '')) IN (
        SELECT identity FROM _reset_identities
    )
   OR CASE
        WHEN regexp_replace(
            split_part(COALESCE(candidate, ''), '@', 1),
            '[^0-9]', '', 'g'
        ) LIKE '0%'
            THEN '62' || substr(
                regexp_replace(
                    split_part(COALESCE(candidate, ''), '@', 1),
                    '[^0-9]', '', 'g'
                ), 2
            )
        WHEN regexp_replace(
            split_part(COALESCE(candidate, ''), '@', 1),
            '[^0-9]', '', 'g'
        ) LIKE '8%'
            THEN '62' || regexp_replace(
                split_part(COALESCE(candidate, ''), '@', 1),
                '[^0-9]', '', 'g'
            )
        ELSE regexp_replace(
            split_part(COALESCE(candidate, ''), '@', 1),
            '[^0-9]', '', 'g'
        )
    END = target.canonical_phone
ON CONFLICT DO NOTHING;

-- Ambil juga alias yang hanya pernah muncul di session target. Jangan mengambil
-- alias dari seluruh customer pada agent milik user: session mereka memang ikut
-- terhapus bersama agent, tetapi identitas mereka bukan identitas owner.
WITH identity_sessions AS (
    SELECT DISTINCT s.id
    FROM sessions s
    CROSS JOIN _reset_target target
    WHERE EXISTS (
        SELECT 1
        FROM (
            VALUES
                (s.external_user_id),
                (s.channel_config ->> 'phone_number'),
                (s.channel_config ->> 'user_phone'),
                (s.channel_config ->> 'from_phone'),
                (s.channel_config ->> 'sender_phone'),
                (s.channel_config ->> 'user_jid'),
                (s.channel_config ->> 'sender_jid'),
                (s.channel_config ->> 'wa_lid')
        ) session_identity(candidate)
        WHERE btrim(COALESCE(candidate, '')) IN (
                SELECT identity FROM _reset_identities
            )
           OR CASE
                WHEN regexp_replace(
                    split_part(COALESCE(candidate, ''), '@', 1),
                    '[^0-9]', '', 'g'
                ) LIKE '0%'
                    THEN '62' || substr(
                        regexp_replace(
                            split_part(COALESCE(candidate, ''), '@', 1),
                            '[^0-9]', '', 'g'
                        ), 2
                    )
                WHEN regexp_replace(
                    split_part(COALESCE(candidate, ''), '@', 1),
                    '[^0-9]', '', 'g'
                ) LIKE '8%'
                    THEN '62' || regexp_replace(
                        split_part(COALESCE(candidate, ''), '@', 1),
                        '[^0-9]', '', 'g'
                    )
                ELSE regexp_replace(
                    split_part(COALESCE(candidate, ''), '@', 1),
                    '[^0-9]', '', 'g'
                )
            END = target.canonical_phone
    )
)
INSERT INTO _reset_identities (identity)
SELECT DISTINCT btrim(candidate)
FROM sessions s
JOIN identity_sessions target_session ON target_session.id = s.id
CROSS JOIN LATERAL (
    VALUES
        (s.external_user_id),
        (s.channel_config ->> 'phone_number'),
        (s.channel_config ->> 'user_phone'),
        (s.channel_config ->> 'from_phone'),
        (s.channel_config ->> 'sender_phone'),
        (s.channel_config ->> 'user_jid'),
        (s.channel_config ->> 'sender_jid'),
        (s.channel_config ->> 'wa_lid')
) all_session_identities(candidate)
WHERE candidate IS NOT NULL AND btrim(candidate) <> ''
ON CONFLICT DO NOTHING;

-- 1. Hapus state Google yang tidak punya FK/cascade ke user atau agent.
WITH deleted AS (
    DELETE FROM oauth_states oauth
    WHERE btrim(oauth.external_user_id) IN (
            SELECT identity FROM _reset_identities
        )
       OR oauth.agent_id IN (
            SELECT agent_id::text FROM _reset_agent_ids
        )
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('oauth_states', (SELECT count(*) FROM deleted));

WITH deleted AS (
    DELETE FROM google_integrations integration
    WHERE btrim(integration.external_user_id) IN (
            SELECT identity FROM _reset_identities
        )
       OR integration.agent_id IN (
            SELECT agent_id::text FROM _reset_agent_ids
        )
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('google_integrations', (SELECT count(*) FROM deleted));

-- 2. Ini bagian yang sebelumnya tertinggal: memory milik Arthur menggunakan
-- agent_id Arthur, tetapi scope-nya adalah nomor/identitas user.
WITH deleted AS (
    DELETE FROM agent_memories memory
    USING _reset_target target
    WHERE btrim(COALESCE(memory.scope, '')) IN (
            SELECT identity FROM _reset_identities
        )
       OR CASE
            WHEN regexp_replace(
                split_part(COALESCE(memory.scope, ''), '@', 1),
                '[^0-9]', '', 'g'
            ) LIKE '0%'
                THEN '62' || substr(
                    regexp_replace(
                        split_part(COALESCE(memory.scope, ''), '@', 1),
                        '[^0-9]', '', 'g'
                    ), 2
                )
            WHEN regexp_replace(
                split_part(COALESCE(memory.scope, ''), '@', 1),
                '[^0-9]', '', 'g'
            ) LIKE '8%'
                THEN '62' || regexp_replace(
                    split_part(COALESCE(memory.scope, ''), '@', 1),
                    '[^0-9]', '', 'g'
                )
            ELSE regexp_replace(
                split_part(COALESCE(memory.scope, ''), '@', 1),
                '[^0-9]', '', 'g'
            )
        END = target.canonical_phone
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('arthur_scoped_memories', (SELECT count(*) FROM deleted));

-- 3. Menghapus session meng-cascade messages, runs, dan scheduled_jobs.
WITH deleted AS (
    DELETE FROM sessions s
    WHERE s.id IN (SELECT session_id FROM _reset_session_ids)
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('sessions', (SELECT count(*) FROM deleted));

-- 4. Link code bisa tetap ada pada reset parsial lama; bersihkan berdasarkan
-- user_id maupun claimed identity sebelum users dihapus.
WITH deleted AS (
    DELETE FROM wa_link_codes link
    USING _reset_target target
    WHERE link.user_id IN (SELECT user_id FROM _reset_user_ids)
       OR btrim(COALESCE(link.claimed_identity, '')) IN (
            SELECT identity FROM _reset_identities
        )
       OR CASE
            WHEN regexp_replace(
                split_part(COALESCE(link.claimed_identity, ''), '@', 1),
                '[^0-9]', '', 'g'
            ) LIKE '0%'
                THEN '62' || substr(
                    regexp_replace(
                        split_part(COALESCE(link.claimed_identity, ''), '@', 1),
                        '[^0-9]', '', 'g'
                    ), 2
                )
            WHEN regexp_replace(
                split_part(COALESCE(link.claimed_identity, ''), '@', 1),
                '[^0-9]', '', 'g'
            ) LIKE '8%'
                THEN '62' || regexp_replace(
                    split_part(COALESCE(link.claimed_identity, ''), '@', 1),
                    '[^0-9]', '', 'g'
                )
            ELSE regexp_replace(
                split_part(COALESCE(link.claimed_identity, ''), '@', 1),
                '[^0-9]', '', 'g'
            )
        END = target.canonical_phone
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('wa_link_codes', (SELECT count(*) FROM deleted));

-- 5. Hard-delete agent milik user. FK cascade membersihkan memories agent,
-- documents, skills, custom_tools, manuals, jobs, dan session yang tersisa.
WITH deleted AS (
    DELETE FROM agents a
    WHERE a.id IN (SELECT agent_id FROM _reset_agent_ids)
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('agents', (SELECT count(*) FROM deleted));

-- 6. User delete meng-cascade subscription, token top-up, dan link code.
WITH deleted AS (
    DELETE FROM users u
    WHERE u.id IN (SELECT user_id FROM _reset_user_ids)
    RETURNING 1
)
INSERT INTO _reset_counts VALUES ('users', (SELECT count(*) FROM deleted));

-- Verifikasi keras: bila salah satu residu utama masih ada, seluruh transaksi
-- dibatalkan sehingga tidak menghasilkan kondisi setengah terhapus.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM sessions s
        WHERE s.id IN (SELECT session_id FROM _reset_session_ids)
    ) THEN
        RAISE EXCEPTION 'Reset gagal: session/riwayat percakapan masih tersisa';
    END IF;

    IF EXISTS (
        SELECT 1 FROM agent_memories memory
        CROSS JOIN _reset_target target
        WHERE btrim(COALESCE(memory.scope, '')) IN (
                SELECT identity FROM _reset_identities
            )
           OR regexp_replace(
                split_part(COALESCE(memory.scope, ''), '@', 1),
                '[^0-9]', '', 'g'
            ) = target.canonical_phone
    ) THEN
        RAISE EXCEPTION 'Reset gagal: scoped memory Arthur masih tersisa';
    END IF;

    IF EXISTS (
        SELECT 1 FROM oauth_states oauth
        WHERE btrim(oauth.external_user_id) IN (
                SELECT identity FROM _reset_identities
            )
           OR oauth.agent_id IN (
                SELECT agent_id::text FROM _reset_agent_ids
            )
    ) OR EXISTS (
        SELECT 1 FROM google_integrations integration
        WHERE btrim(integration.external_user_id) IN (
                SELECT identity FROM _reset_identities
            )
           OR integration.agent_id IN (
                SELECT agent_id::text FROM _reset_agent_ids
            )
    ) THEN
        RAISE EXCEPTION 'Reset gagal: Google OAuth masih tersisa';
    END IF;

    IF EXISTS (
        SELECT 1 FROM agents a
        WHERE a.id IN (SELECT agent_id FROM _reset_agent_ids)
    ) THEN
        RAISE EXCEPTION 'Reset gagal: agent milik user masih tersisa';
    END IF;

    IF EXISTS (
        SELECT 1 FROM users u
        WHERE u.id IN (SELECT user_id FROM _reset_user_ids)
    ) THEN
        RAISE EXCEPTION 'Reset gagal: row user masih tersisa';
    END IF;
END $$;

-- Tampilkan hasil sebelum commit untuk audit.
SELECT item, deleted_count
FROM _reset_counts
ORDER BY item;

COMMIT;

-- Setelah COMMIT, chat berikutnya akan membuat user/session baru dan Arthur
-- tidak lagi memiliki messages atau scoped memory dari percakapan sebelumnya.
