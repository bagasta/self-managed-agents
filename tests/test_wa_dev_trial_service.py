from app.core.domain.wa_dev_trial_service import (
    looks_like_wa_dev_trial_code,
    normalize_wa_dev_trial_code,
)


def test_normalize_wa_dev_trial_code_accepts_compact_code():
    assert normalize_wa_dev_trial_code(" ab-12c3 ") == "AB12C3"
    assert looks_like_wa_dev_trial_code("AB12C3")


def test_normalize_wa_dev_trial_code_rejects_short_code():
    assert normalize_wa_dev_trial_code("A12") == "A12"
    assert not looks_like_wa_dev_trial_code("A12")

