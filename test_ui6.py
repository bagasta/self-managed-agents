import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker exec -i deploy-api-1 curl -s -I http://localhost:8000/ui/")
print("EXEC CURL 1:", stdout.read().decode())
print("EXEC CURL STDERR:", stderr.read().decode())

stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker exec -i deploy-api-1 ls -la /app/UI-DEV")
print("EXEC LS:", stdout.read().decode())
ssh.close()
