"""A hotfix changes application identity while preserving live rollout settings."""
import json

from scripts import arthur_qa_release as release


def test_hotfix_compose_preserves_each_service_environment_and_rollback(tmp_path, monkeypatch):
    old_image, candidate_image = "sha256:old", "sha256:hotfix"
    environments = [
        {"API_KEY": "api-private", "ARTHUR_CONFIRMATION_OWNER_IDS": '["owner-a"]',
         "DEMO_CUSTOMER_PREVIEW_AGENT_IDS": '["agent-a"]',
         "SUBSCRIPTION_EXPIRY_ENFORCEMENT_ENABLED": "false", "APP_COMMIT_SHA": "old"},
        {"API_KEY": "worker-private", "ARTHUR_CONFIRMATION_OWNER_IDS": "[]",
         "DEMO_CUSTOMER_PREVIEW_AGENT_IDS": "[]", "APP_COMMIT_SHA": "old"},
    ]
    original = [{"Config": {"Env": [f"{k}={v}" for k, v in env.items()], "Cmd": [role]}}
                for role, env in zip(("api-command", "worker-command"), environments)]
    (tmp_path / "production-containers.private.json").write_text(json.dumps(original))
    base = {"services": {role: {"image": "unused", "build": {}, "networks": ["internal"]}
                          for role in ("api", "scheduler")}}
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[-2:] == ["--format", "json"]:
            return json.dumps(base).encode()
        assert args[0:2] == ["docker", "compose"]
        assert args[-2:] == ["config", "--quiet"]
        return b""

    monkeypatch.setattr(release, "run", run)
    state = {"folder": str(tmp_path), "old_image": old_image, "candidate_image": candidate_image,
             "candidate_commit": "hotfix", "preserve_runtime_settings": True}
    release.production_specs(state)
    rollback = json.loads((tmp_path / "production.rollback.private.json").read_text())
    candidate = json.loads((tmp_path / "production.candidate.private.json").read_text())
    for role, env in zip(("api", "scheduler"), environments):
        assert rollback["services"][role]["environment"] == env
        assert candidate["services"][role]["environment"] == {**env, "APP_COMMIT_SHA": "hotfix"}
        assert rollback["services"][role]["image"] == old_image
        assert candidate["services"][role]["image"] == candidate_image
        assert candidate["services"][role]["command"] == rollback["services"][role]["command"]
        assert candidate["services"][role]["networks"] == ["internal"]
        assert "build" not in candidate["services"][role]
    assert len(calls) == 3
    assert state["confirmation_owner_count"] == state["demo_preview_agent_count"] == 1
    assert state["expiry_enforcement"] is False

