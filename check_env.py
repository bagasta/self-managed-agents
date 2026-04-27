import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S cat /home/clevio/stack/managed-agents/deploy/.env.prod")
print("ENV PROD CONTENT:", stdout.read().decode())
ssh.close()
