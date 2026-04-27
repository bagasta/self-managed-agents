import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker logs deploy-wa-dev-service-1 2>&1 | tail -n 20")
print("WA DEV LOGS:", stdout.read().decode())
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker inspect deploy-wa-dev-service-1 --format '{{.State.Status}}'")
print("WA DEV STATUS:", stdout.read().decode())
ssh.close()
