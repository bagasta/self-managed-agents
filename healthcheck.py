import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker exec -i deploy-api-1 curl -f -v http://localhost:8000/health")
print("Healthcheck:", stdout.read().decode())
print("Healthcheck STDERR:", stderr.read().decode())
ssh.close()
