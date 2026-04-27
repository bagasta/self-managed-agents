import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
content = """DATABASE_URL=postgresql+asyncpg://postgres:Aiagronomists4725.@host.docker.internal:5432/managed_agents
OPENROUTER_API_KEY=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
MISTRAL_API_KEY=ape5v2zPUF23U7M4H5b4EaEWIKTKbwfZ
API_KEY=42523db14d86f993409fba4984764be01fb169ddf7e5e401efab2f33442c9a7b
MAIN_API_KEY=42523db14d86f993409fba4984764be01fb169ddf7e5e401efab2f33442c9a7b
LOG_LEVEL=INFO

SANDBOX_BASE_DIR=/tmp/agent-sandboxes
DOCKER_SANDBOX_IMAGE=python:3.12-slim
DOCKER_HOST=unix:///run/docker.sock

AGENT_MAX_STEPS=12
AGENT_TIMEOUT_SECONDS=300

WA_SERVICE_URL=http://localhost:8080
"""

# Try using a different way to pass the content
# We will write the content to a temp file and then move it with sudo
with open('temp_env_prod', 'w') as f:
    f.write(content)

sftp = ssh.open_sftp()
sftp.put('temp_env_prod', '/home/clevio/stack/managed-agents/deploy/.env.prod.tmp')
sftp.close()

# Now move it using sudo
ssh.exec_command("echo 'Humancentric4725.' | sudo -S mv /home/clevio/stack/managed-agents/deploy/.env.prod.tmp /home/clevio/stack/managed-agents/deploy/.env.prod")

ssh.close()
