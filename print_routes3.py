import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
script = """
import sys
from app.main import app
with open('/tmp/routes.txt', 'w') as f:
    for r in app.routes:
        if hasattr(r, 'methods'):
            f.write(f'{r.path} {r.methods}\\n')
        else:
            f.write(f'{r.path} no methods\\n')
"""
stdin, stdout, stderr = ssh.exec_command(f"echo 'Humancentric4725.' | sudo -S docker exec -i deploy-api-1 python -c \"{script}\"")
print(stdout.read().decode())
print(stderr.read().decode())
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker exec -i deploy-api-1 cat /tmp/routes.txt")
print("ROUTES:", stdout.read().decode())
ssh.close()
