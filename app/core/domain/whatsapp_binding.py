"""Release local Meta credentials and exclusive phone ownership."""


def clear_cloud_api_binding(agent) -> None:
    agent.wa_phone_number_id = None
    agent.wa_waba_id = None
    agent.wa_access_token_encrypted = None
    agent.wa_display_phone = None
    agent.wa_business_name = None
    agent.wa_cloud_api_mode = None
    agent.wa_connection_type = "legacy_qr" if agent.wa_device_id else None
    agent.channel_type = "whatsapp" if agent.wa_device_id else None
