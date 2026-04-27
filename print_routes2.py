import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
script = """
from app.main import app
for r in app.routes:
    if hasattr(r, 'methods'):
        print(r.path, r.methods)
    else:
        print(r.path, 'no methods')
"""
stdin, stdout, stderr = ssh.exec_command(f"echo 'Humancentric4725.' | sudo -S docker exec -i deploy-api-1 python3 -c \"{script}\"")
print("STDOUT:", stdout.read().decode())
print("STDERR:", stderr.read().decode())
ssh.close()
