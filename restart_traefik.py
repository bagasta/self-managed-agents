import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker restart traefik || sudo -S docker restart root-traefik-1 || sudo -S docker restart proxy")
print("Restart Traefik:", stdout.read().decode())
ssh.close()
