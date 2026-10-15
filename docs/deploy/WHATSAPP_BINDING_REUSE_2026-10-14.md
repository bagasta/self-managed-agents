# WhatsApp number reuse repair

Signup failed with HTTP 500 because a soft-deleted LapiBot retained its
`wa_phone_number_id`, which is protected by a database unique index. The active
agent conflict check excluded deleted records, so it missed that binding.

Deletion clears local Meta credentials and phone ownership. Disconnect also
clears stale Meta fields when the transport type is missing or inconsistent.
Both signup completion routes lock the previous phone owner, preserve active
bindings, and release deleted bindings before assigning the phone to the new
agent. Concurrent claims return HTTP 409 after rollback. The signup page
handles non-JSON server responses with a readable Indonesian error.

Validation: 28 tests passed across `test_whatsapp_binding_reuse.py`,
`test_meta_embedded_signup.py`, and `test_agent_deletion_lifecycle.py` in an
isolated container using the production base image.

Image: `managed-agents-app:whatsapp-binding-20261014`.
Build: `docker build -f deploy/Dockerfile.whatsapp-binding-hotfix -t managed-agents-app:whatsapp-binding-20261014 .`
Deploy from `deploy`: `IMAGE_TAG=whatsapp-binding-20261014 docker compose -f docker-compose.prod.yml up -d --no-deps --no-build api`.
Rollback from `deploy`: `IMAGE_TAG=customer-workflow-tools-20261014 docker compose -f docker-compose.prod.yml up -d --no-deps --no-build api`.

The incident's deleted binding was repaired separately before this deployment.
No active customer's phone binding was transferred by this repair.
