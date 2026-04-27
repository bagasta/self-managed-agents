def normalize_phone(phone: str) -> str:
    """Strip leading '+' and '@domain' suffix from a WhatsApp JID or phone number."""
    return phone.lstrip("+").split("@")[0]
