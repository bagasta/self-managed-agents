-- Hapus seluruh data user WhatsApp dan data miliknya agar bisa mulai dari nol.
-- Jalankan dengan PostgreSQL/psql. Ganti literal nomor di bawah bila diperlukan.
-- Nomor boleh berisi +, spasi, atau tanda hubung.

-- Pulihkan sesi bila percobaan sebelumnya berhenti pada error 25P02.
-- Script ini dimaksudkan untuk dijalankan sebagai script mandiri.
ROLLBACK;

DROP TABLE IF EXISTS _delete_target_agents;
DROP TABLE IF EXISTS _delete_target_aliases;
DROP TABLE IF EXISTS _delete_target_users;
DROP TABLE IF EXISTS _delete_target_phone;

BEGIN;

CREATE TEMP TABLE _delete_target_phone ON COMMIT PRESERVE ROWS AS
SELECT regexp_replace('+62 821-7304-2001', '[^0-9]', '', 'g') AS phone;

CREATE TEMP TABLE _delete_target_users ON COMMIT PRESERVE ROWS AS
SELECT u.id
FROM users u
CROSS JOIN _delete_target_phone t
WHERE regexp_replace(coalesce(u.phone_number, ''), '[^0-9]', '', 'g') = t.phone
   OR regexp_replace(coalesce(u.external_id, ''), '[^0-9]', '', 'g') = t.phone;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM _delete_target_users) THEN
        RAISE NOTICE 'User dengan nomor target tidak ditemukan; lanjut membersihkan data orphan berdasarkan alias nomor.';
    END IF;
END $$;

CREATE TEMP TABLE _delete_target_aliases(value text PRIMARY KEY) ON COMMIT PRESERVE ROWS;
INSERT INTO _delete_target_aliases(value)
SELECT phone FROM _delete_target_phone
UNION
SELECT unnest(array_remove(array[u.external_id, u.phone_number, u.wa_lid], NULL))
FROM users u
JOIN _delete_target_users target ON target.id = u.id;

CREATE TEMP TABLE _delete_target_agents ON COMMIT PRESERVE ROWS AS
SELECT a.id
FROM agents a
JOIN _delete_target_aliases alias
  ON a.owner_external_id = alias.value;

-- Bersihkan memory scoped user, termasuk memory pada agent Arthur.
DELETE FROM agent_memories m
USING _delete_target_aliases alias
WHERE m.scope = alias.value;

-- API key WA tidak memiliki FK ke users; hapus berdasarkan label.
DELETE FROM user_api_keys k
USING _delete_target_aliases alias
WHERE k.label = 'wa:' || alias.value;

-- Session user pada agent apa pun ikut dihapus. Message, run, dan scheduled
-- job yang memiliki FK CASCADE akan ikut terhapus.
DELETE FROM sessions s
USING _delete_target_aliases alias
WHERE s.external_user_id = alias.value
   OR regexp_replace(coalesce(s.channel_config->>'phone_number', ''), '[^0-9]', '', 'g') =
      (SELECT phone FROM _delete_target_phone)
   OR regexp_replace(coalesce(s.channel_config->>'user_phone', ''), '[^0-9]', '', 'g') =
      (SELECT phone FROM _delete_target_phone);

-- Hapus semua agent yang owner_external_id-nya milik user target.
-- Agent milik user lain yang hanya pernah menerima session user ini tidak ikut.
DELETE FROM agents a
USING _delete_target_agents target
WHERE a.id = target.id;

-- Subscription, top-up, dan WA link code terhapus melalui FK ON DELETE CASCADE.
DELETE FROM users u
USING _delete_target_users target
WHERE u.id = target.id;

COMMIT;

-- Verifikasi: seluruh hasil harus 0.
SELECT
    (SELECT count(*) FROM users u CROSS JOIN _delete_target_phone t
      WHERE regexp_replace(coalesce(u.phone_number, ''), '[^0-9]', '', 'g') = t.phone
         OR regexp_replace(coalesce(u.external_id, ''), '[^0-9]', '', 'g') = t.phone) AS users_remaining,
    (SELECT count(*) FROM agents a
      JOIN _delete_target_aliases alias ON a.owner_external_id = alias.value) AS owned_agents_remaining,
    (SELECT count(*) FROM agent_memories m
      JOIN _delete_target_aliases alias ON m.scope = alias.value) AS scoped_memories_remaining;
