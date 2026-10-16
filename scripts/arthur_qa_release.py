"""Back up, test an isolated clone, then explicitly replace or roll back the app.

Only the production/rollback-production actions replace live app containers.
This script never restores a backup into production. Private state stays outside Git.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "sha256:d9ab842ad9326502b1c85a18e4c08d3a02542ea5b9826bc96ef020c231317469"
LABEL = "id.clevio.arthur-qa-release"


def run(args, *, env=None, input=None):
    result = subprocess.run(args, env=env, input=input, capture_output=True)
    if result.returncode:
        # Do not echo command arguments, inspect output, or provider credentials.
        raise RuntimeError(f"{args[0]} operation failed (exit {result.returncode})")
    return result.stdout


def private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect(name):
    return json.loads(run(["docker", "inspect", name]))[0]


def prepare(candidate_image=CANDIDATE, candidate_commit="c768a25", preserve_runtime_settings=False):
    os.umask(0o077)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    folder = Path.home() / "backups" / ("arthur-qa-" + stamp)
    folder.mkdir(parents=True, mode=0o700)
    if shutil.disk_usage(folder).free < 8 * 1024**3:
        raise RuntimeError("At least 8 GiB free is required before backup")
    containers = [inspect("deploy-api-1"), inspect("deploy-scheduler-1")]
    if containers[0]["Image"] != containers[1]["Image"]:
        raise RuntimeError("Production API and scheduler images differ; review first")
    old = containers[0]["Image"]
    candidate_image = json.loads(run(["docker", "image", "inspect", candidate_image]))[0]["Id"]
    private_json(folder / "production-containers.private.json", containers)
    config = folder / "configuration"
    config.mkdir(mode=0o700)
    for path in (ROOT / "deploy").glob("*.yml"):
        shutil.copy2(path, config / path.name)
        (config / path.name).chmod(0o600)
    for name in (".env", ".env.prod"):
        path = ROOT / "deploy" / name
        if path.exists():
            shutil.copy2(path, config / name)
            (config / name).chmod(0o600)
    tag = "managed-agents-app:rollback-qa-" + stamp
    run(["docker", "image", "tag", old, tag])
    env = dict(item.split("=", 1) for item in containers[0]["Config"]["Env"])
    url = urlsplit(env["DATABASE_URL"].replace("postgresql+asyncpg", "postgresql"))
    pg_env = {**os.environ, "PGHOST": url.hostname, "PGPORT": str(url.port or 5432),
              "PGUSER": unquote(url.username or ""), "PGPASSWORD": unquote(url.password or ""),
              "PGDATABASE": unquote(url.path.lstrip("/")), "PGCONNECT_TIMEOUT": "15"}
    dump = folder / "database.dump"
    print("Creating consistent database archive", flush=True)
    command = ["docker", "run", "--rm", "--network", "deploy_internal", "--add-host",
               "host.docker.internal:host-gateway"]
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGCONNECT_TIMEOUT"):
        command += ["-e", key]
    with dump.open("xb") as output, (folder / "pg_dump.private.log").open("xb") as error:
        result = subprocess.run(command + ["postgres:16-alpine", "pg_dump", "--format=custom",
                                "--no-owner", "--no-acl"], env=pg_env, stdout=output, stderr=error)
    if result.returncode or dump.stat().st_size < 1000:
        raise RuntimeError("Database backup failed; see private log. Production was not changed")
    with dump.open("rb") as source:
        result = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none",
                                 "postgres:16-alpine", "pg_restore", "--list"],
                                stdin=source, capture_output=True)
    if result.returncode:
        raise RuntimeError("Backup archive listing failed")
    print("Saving both application images for rollback", flush=True)
    with (folder / "images.tar.gz").open("xb") as output:
        process = subprocess.Popen(["docker", "image", "save", tag, candidate_image],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with gzip.GzipFile(fileobj=output, mode="wb", compresslevel=1) as zipped:
            shutil.copyfileobj(process.stdout, zipped)
        if process.wait():
            raise RuntimeError("Image archive failed")
    state = {"release": stamp, "folder": str(folder), "old_image": old, "candidate_image": candidate_image,
             "candidate_commit": candidate_commit, "preserve_runtime_settings": preserve_runtime_settings,
             "rollback_tag": tag, "database_sha256": digest(dump),
             "images_sha256": digest(folder / "images.tar.gz"), "archive_entries": len(result.stdout.splitlines()),
             "production_unchanged": True, "staging_password": secrets.token_hex(24),
             "staging_api_key": secrets.token_hex(32)}
    private_json(folder / "state.private.json", state)
    print(json.dumps({"backup_directory": str(folder), "database_bytes": dump.stat().st_size,
                      "archive_entries": state["archive_entries"], "old_image": old,
                      "candidate_image": CANDIDATE, "production_unchanged": True}), flush=True)


def save(state):
    private_json(Path(state["folder"]) / "state.private.json", state)


def stage_environment(state):
    return {"DATABASE_URL": f"postgresql+asyncpg://qa:{state['staging_password']}@{state['pg_name']}:5432/arthur_qa_clone",
            "API_KEY": state["staging_api_key"], "ENVIRONMENT": "test", "REDIS_URL": "",
            "EMBEDDED_SCHEDULER_ENABLED": "false", "CHANNEL_SECRET_KEY": secrets.token_hex(32),
            "GOOGLE_INTEGRATION_SERVICE_URL": "http://blocked.invalid", "WA_SERVICE_URL": "http://blocked.invalid",
            "WA_DEV_SERVICE_URL": "http://blocked.invalid", "APP_COMMIT_SHA": state.get("candidate_commit", "c768a25"),
            "SUBSCRIPTION_EXPIRY_ENFORCEMENT_ENABLED": "false", "ARTHUR_CONFIRMATION_OWNER_IDS": "[]",
            "DEMO_CUSTOMER_PREVIEW_AGENT_IDS": "[]", "SUBSCRIPTION_EXPIRY_ENFORCEMENT_USER_IDS": "[]"}


def start_stage_apps(state, image):
    redis_name = "arthur-qa-redis-" + state["release"].replace("-", "")
    if not state.get("redis_name"):
        run(["docker", "run", "-d", "--name", redis_name, "--network", state["network"],
             "--label", LABEL + "=" + state["release"], "--memory", "128m", "redis:8.8.2",
             "redis-server", "--save", "", "--appendonly", "no"])
        state["redis_name"] = redis_name
    state["staging_environment"]["REDIS_URL"] = f"redis://{state['redis_name']}:6379/0"
    save(state)
    env = {**os.environ, **state["staging_environment"]}
    for role in ("api", "scheduler"):
        name = state[role + "_name"]
        command = ["docker", "run", "-d", "--name", name, "--network", state["network"],
                   "--label", LABEL + "=" + state["release"], "--restart", "unless-stopped",
                   "--memory", "768m", "--cpus", "1"]
        for key in state["staging_environment"]:
            command += ["-e", key]
        if role == "api":
            entry = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
        else:
            entry = ["python", "-m", "app.scheduler_worker"]
        run(command + ["--entrypoint", entry[0], image] + entry[1:], env=env)
    for attempt in range(45):
        result = subprocess.run(["docker", "exec", state["api_name"], "python", "-c",
            "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200"], capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Staging health failed; production remains unchanged")
    code = """import os,urllib.request
for path in ('/v1/subscriptions/plans', '/v1/agents?limit=1'):
 request=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'X-API-Key':os.environ['API_KEY']})
 assert urllib.request.urlopen(request,timeout=10).status==200
"""
    run(["docker", "exec", "-i", state["api_name"], "python", "-"], input=code.encode())
    if inspect(state["scheduler_name"])["State"]["Status"] != "running":
        raise RuntimeError("Staging worker exited")
    state["staging_image"] = image
    state.setdefault("staging_checks", []).append({"image": image, "health": "passed", "authenticated_reads": "passed"})
    save(state)
    print(json.dumps({"staging_image": image, "health": "passed", "worker": "running",
                      "network": "internal-only", "production_unchanged": True}), flush=True)


def staging(state):
    suffix = state["release"].replace("-", "")
    state.update(network="arthur-qa-stage-" + suffix, pg_name="arthur-qa-pg-" + suffix,
                 api_name="arthur-qa-api-" + suffix, scheduler_name="arthur-qa-worker-" + suffix, port=18016)
    run(["docker", "network", "create", "--internal", "--label", LABEL + "=" + state["release"], state["network"]])
    env = {**os.environ, "POSTGRES_PASSWORD": state["staging_password"]}
    run(["docker", "run", "-d", "--name", state["pg_name"], "--network", state["network"],
         "--label", LABEL + "=" + state["release"], "--memory", "512m", "-e", "POSTGRES_PASSWORD",
         "-e", "POSTGRES_USER=qa", "-e", "POSTGRES_DB=arthur_qa_clone", "pgvector/pgvector:pg16"], env=env)
    save(state)
    for attempt in range(30):
        result = subprocess.run(["docker", "exec", state["pg_name"], "pg_isready", "-U", "qa"], capture_output=True)
        if not result.returncode:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Staging PostgreSQL not ready")
    with (Path(state["folder"]) / "database.dump").open("rb") as source:
        result = subprocess.run(["docker", "exec", "-i", state["pg_name"], "pg_restore", "--exit-on-error",
                                 "--no-owner", "--no-acl", "-U", "qa", "-d", "arthur_qa_clone"],
                                stdin=source, capture_output=True)
    if result.returncode:
        (Path(state["folder"]) / "restore.private.log").write_bytes(result.stderr)
        raise RuntimeError("Restore drill failed; see private log")
    # Quarantine only the CLONED scheduled jobs/outbox before a stage worker starts.
    run(["docker", "exec", state["pg_name"], "psql", "-U", "qa", "-d", "arthur_qa_clone", "-v", "ON_ERROR_STOP=1", "-c",
         "UPDATE scheduled_jobs SET status='paused' WHERE status IN ('active','running'); "
         "UPDATE outbound_messages SET status='cancelled' WHERE status IN ('queued','pending','sending');"])
    counts = run(["docker", "exec", state["pg_name"], "psql", "-U", "qa", "-d", "arthur_qa_clone", "-At", "-c",
                  "SELECT json_build_object('agents',(SELECT count(*) FROM agents),'documents',(SELECT count(*) FROM documents),'sessions',(SELECT count(*) FROM sessions),'messages',(SELECT count(*) FROM messages));"])
    state["restore_verified"] = True
    state["restored_counts"] = json.loads(counts)
    state["staging_environment"] = stage_environment(state)
    save(state)
    print(json.dumps({"restore_verified": True, "restored_counts": state["restored_counts"]}), flush=True)
    start_stage_apps(state, state["candidate_image"])


def switch_stage(state, image):
    for key in ("api_name", "scheduler_name"):
        name = state[key]
        if inspect(name)["Config"].get("Labels", {}).get(LABEL) != state["release"]:
            raise RuntimeError("Refusing to replace a container outside this staging release")
        run(["docker", "stop", "--time", "15", name])
        run(["docker", "rm", name])
    start_stage_apps(state, image)


def stage_probe(state):
    if state.get("staging_image") != state["candidate_image"]:
        raise RuntimeError("Probe requires the candidate in staging")
    target = state["api_name"] + ":/tmp/arthur_qa_stage_probe.py"
    run(["docker", "cp", str(ROOT / "scripts/arthur_qa_stage_probe.py"), target])
    output = run(["docker", "exec", "-e", "PYTHONPATH=/app", state["api_name"],
                  "python", "/tmp/arthur_qa_stage_probe.py"])
    # Only the last line is the probe's structured receipt, never customer content.
    state["staging_probe"] = json.loads(output.splitlines()[-1])
    save(state)
    print(json.dumps(state["staging_probe"]), flush=True)


def production_specs(state):
    original = json.loads((Path(state["folder"]) / "production-containers.private.json").read_text())
    base = json.loads(run(["docker", "compose", "--env-file", str(ROOT / "deploy/.env.prod"), "-p", "deploy", "-f",
                          str(ROOT / "deploy/docker-compose.prod.yml"), "config", "--format", "json"]))
    for service, container in zip(("api", "scheduler"), original):
        spec = base["services"][service]
        spec.pop("build", None)
        spec["image"] = state["old_image"]
        spec["environment"] = dict(item.split("=", 1) for item in container["Config"]["Env"])
        spec["command"] = container["Config"]["Cmd"]
    rollback = Path(state["folder"]) / "production.rollback.private.json"
    candidate = Path(state["folder"]) / "production.candidate.private.json"
    private_json(rollback, base)
    if state.get("preserve_runtime_settings"):
        # A focused hotfix must retain the currently deployed rollout cohorts,
        # credentials and policy flags, rather than reselecting QA accounts.
        for service in ("api", "scheduler"):
            base["services"][service]["image"] = state["candidate_image"]
            base["services"][service]["environment"]["APP_COMMIT_SHA"] = state["candidate_commit"]
        env = base["services"]["api"]["environment"]
        private_json(candidate, base)
        for path in (rollback, candidate):
            run(["docker", "compose", "-p", "deploy", "-f", str(path), "config", "--quiet"])
        state.update(rollback_compose=str(rollback), candidate_compose=str(candidate),
                     confirmation_owner_count=len(json.loads(env.get("ARTHUR_CONFIRMATION_OWNER_IDS", "[]"))),
                     demo_preview_agent_count=len(json.loads(env.get("DEMO_CUSTOMER_PREVIEW_AGENT_IDS", "[]"))),
                     expiry_enforcement=env.get("SUBSCRIPTION_EXPIRY_ENFORCEMENT_ENABLED", "false").lower() == "true")
        save(state)
        return
    code = """import asyncio,json
from sqlalchemy import select,text
from app.database import AsyncSessionLocal
from app.models.agent import Agent
async def main():
 async with AsyncSessionLocal() as db:
  await db.execute(text('SET TRANSACTION READ ONLY'))
  a=(await db.execute(select(Agent).where(Agent.name=='CS Toko Kue QA',Agent.is_deleted.is_(False)))).scalar_one()
  print(json.dumps({'owner':a.owner_external_id,'agent_id':str(a.id)}))
asyncio.run(main())
"""
    cohort = json.loads(run(["docker", "exec", "-i", "deploy-api-1", "python", "-"], input=code.encode()))
    import re
    if not re.fullmatch(r"62\d{8,13}", cohort.get("owner", "")):
        raise RuntimeError("QA Owner phone is not unambiguous; no deployment performed")
    for service in ("api", "scheduler"):
        spec = base["services"][service]
        spec["image"] = state["candidate_image"]
        spec["environment"].update(APP_COMMIT_SHA="c768a25",
            ARTHUR_CONFIRMATION_OWNER_IDS=json.dumps([cohort["owner"]]),
            DEMO_CUSTOMER_PREVIEW_AGENT_IDS=json.dumps([cohort["agent_id"]]),
            SUBSCRIPTION_EXPIRY_ENFORCEMENT_ENABLED="false", SUBSCRIPTION_EXPIRY_ENFORCEMENT_USER_IDS="[]")
    private_json(candidate, base)
    for path in (rollback, candidate):
        run(["docker", "compose", "-p", "deploy", "-f", str(path), "config", "--quiet"])
    state.update(rollback_compose=str(rollback), candidate_compose=str(candidate),
                 confirmation_owner_count=1, demo_preview_agent_count=1, expiry_enforcement=False)
    save(state)


def production_smoke(state, image):
    code = """import os,json,urllib.request
def get(path):
 req=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'X-API-Key':os.environ['API_KEY']})
 return json.load(urllib.request.urlopen(req,timeout=15))
health=get('/health')
assert health['status']=='ok'
get('/v1/subscriptions/plans')
detail=get('/health/detailed')
assert detail['checks']['database']=='ok' and detail['checks']['scheduler']=='ok'
print(json.dumps({'health':'passed','database':'ok','scheduler':'ok','commit':health.get('commit')}))
"""
    for attempt in range(30):
        result = subprocess.run(["docker", "exec", "-i", "deploy-api-1", "python", "-"],
                                input=code.encode(), capture_output=True)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Production health/database/scheduler check failed")
    if any(inspect(name)["Image"] != image for name in ("deploy-api-1", "deploy-scheduler-1")):
        raise RuntimeError("Production image mismatch")
    if inspect("deploy-scheduler-1")["State"]["Status"] != "running":
        raise RuntimeError("Production worker exited")
    return json.loads(result.stdout.splitlines()[-1])


def production(state, rollback=False):
    if not state.get("restore_verified") or not state.get("staging_probe"):
        raise RuntimeError("Restore drill and staging probe are required first")
    images_checked = {item["image"] for item in state.get("staging_checks", [])}
    if not {state["old_image"], state["candidate_image"]} <= images_checked:
        raise RuntimeError("Both candidate and rollback must pass staging")
    if not rollback:
        if any(inspect(name)["Image"] != state["old_image"] for name in ("deploy-api-1", "deploy-scheduler-1")):
            raise RuntimeError("Production changed since backup; review before rollout")
        production_specs(state)
    desired = state["old_image"] if rollback else state["candidate_image"]
    config = state["rollback_compose"] if rollback else state["candidate_compose"]
    # The same running process exports both active and waiting run counts.
    code = """import urllib.request
raw=urllib.request.urlopen('http://127.0.0.1:8000/metrics',timeout=5).read().decode()
values={line.split()[0]:float(line.split()[1]) for line in raw.splitlines() if line.startswith(('agent_runs_active ','agent_runs_queued '))}
assert values.get('agent_runs_active')==0 and values.get('agent_runs_queued')==0
"""
    if not rollback:
        run(["docker", "exec", "-i", "deploy-api-1", "python", "-"], input=code.encode())
        production_smoke(state, inspect("deploy-api-1")["Image"])
    print("Draining scheduler and API; rollback configuration is ready", flush=True)
    run(["docker", "stop", "--time", "60", "deploy-scheduler-1"])
    try:
        run(["docker", "stop", "--time", "60", "deploy-api-1"])
        run(["docker", "compose", "-p", "deploy", "-f", config, "up", "-d", "--no-deps",
             "--no-build", "--pull", "never", "api", "scheduler"])
        result = production_smoke(state, desired)
    except Exception:
        print("Candidate check failed; restoring the previous application images", flush=True)
        run(["docker", "compose", "-p", "deploy", "-f", state["rollback_compose"], "up", "-d",
             "--no-deps", "--no-build", "--pull", "never", "api", "scheduler"])
        state["automatic_rollback"] = production_smoke(state, state["old_image"])
        save(state)
        raise
    state["production_image"] = desired
    state["production_result"] = result
    state["production_unchanged"] = False
    save(state)
    print(json.dumps({"deployed_image": desired, **result, "rollback_ready": True,
                      "confirmation_owner_count": 0 if rollback else state["confirmation_owner_count"],
                      "demo_preview_agent_count": 0 if rollback else state["demo_preview_agent_count"],
                      "expiry_enforcement": state.get("expiry_enforcement", False)}), flush=True)


def cleanup_staging(state):
    if not state.get("production_result"):
        raise RuntimeError("Retain staging until production validation has passed")
    for key in ("api_name", "scheduler_name", "pg_name", "redis_name"):
        name = state[key]
        if inspect(name)["Config"].get("Labels", {}).get(LABEL) != state["release"]:
            raise RuntimeError("Refusing to remove a container outside this staging release")
        run(["docker", "rm", "-f", "-v", name])
    run(["docker", "network", "rm", state["network"]])
    state["staging_cleaned"] = True
    save(state)
    print("Isolated staging resources removed; production and rollback backups retained", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "staging", "rollback-staging", "candidate-staging",
                                            "probe-staging", "production", "rollback-production", "cleanup-staging"])
    parser.add_argument("--state", type=Path)
    parser.add_argument("--candidate-image", default=CANDIDATE, help="Candidate image/tag for prepare; stored as immutable ID")
    parser.add_argument("--candidate-commit", default="c768a25", help="Application commit reported by the candidate")
    parser.add_argument("--preserve-runtime-settings", action="store_true", help="Prepare a hotfix retaining all live runtime settings")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            prepare(args.candidate_image, args.candidate_commit, args.preserve_runtime_settings)
        else:
            if args.state is None or args.state.resolve().is_relative_to(ROOT):
                raise RuntimeError("Provide the private state file outside the repository")
            state = json.loads(args.state.read_text())
            if args.action == "staging":
                staging(state)
            elif args.action == "probe-staging":
                stage_probe(state)
            elif args.action == "cleanup-staging":
                cleanup_staging(state)
            elif args.action in ("production", "rollback-production"):
                production(state, rollback=args.action == "rollback-production")
            else:
                switch_stage(state, state["old_image"] if args.action == "rollback-staging" else state["candidate_image"])
    except Exception as exc:
        print(f"Release stopped: {type(exc).__name__}: {exc}", flush=True)
        raise SystemExit(1)
