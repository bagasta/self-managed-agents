import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker logs deploy-wa-dev-service-1")
print("LOGS:", stdout.read().decode())
print("ERRORS:", stderr.read().decode())
ssh.close()
