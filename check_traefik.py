import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker ps --filter label=traefik.http.routers.managed-agents.rule -q | xargs -r docker inspect --format '{{.Name}}: {{index .Config.Labels \"traefik.http.routers.managed-agents.rule\"}}'")
print("Traefik Routers:", stdout.read().decode())
ssh.close()
