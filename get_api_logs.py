import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker compose -f /home/clevio/stack/managed-agents/deploy/docker-compose.prod.yml logs --tail 100 api")
print(stdout.read().decode())
print(stderr.read().decode())
ssh.close()
