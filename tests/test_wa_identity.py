from app.core.utils.wa_identity import (
    resolve_auto_provision_external_id,
    resolve_incoming_wa_phone,
)


def test_resolve_incoming_wa_phone_prefers_resolved_phone():
    assert resolve_incoming_wa_phone("1234567890@lid", "62895619356936") == "62895619356936"


def test_resolve_incoming_wa_phone_accepts_plain_phone():
    assert resolve_incoming_wa_phone("62895619356936", None) == "62895619356936"


def test_resolve_incoming_wa_phone_rejects_lid_without_phone():
    assert resolve_incoming_wa_phone("12345678901234567890@lid", None) is None


def test_resolve_auto_provision_external_id_uses_phone_number_for_whatsapp():
    assert (
        resolve_auto_provision_external_id(
            channel_type="whatsapp",
            channel_config={"phone_number": "62895619356936@s.whatsapp.net"},
            payload_external_user_id=None,
            session_external_user_id=None,
        )
        == "62895619356936"
    )


def test_resolve_auto_provision_external_id_skips_whatsapp_without_phone_number():
    assert (
        resolve_auto_provision_external_id(
            channel_type="whatsapp",
            channel_config={},
            payload_external_user_id="62895619356936",
            session_external_user_id="62895619356936",
        )
        is None
    )


def test_resolve_auto_provision_external_id_allows_non_whatsapp_ids():
    assert (
        resolve_auto_provision_external_id(
            channel_type="webchat",
            channel_config={},
            payload_external_user_id="62895619356936",
            session_external_user_id=None,
        )
        == "62895619356936"
    )
