import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("echo 'Humancentric4725.' | sudo -S docker stop new-langchain-api-app-1 new-langchain-api-wa-service-1 new-langchain-api-scheduler-1 new-langchain-api-app_replica-1")
print("STOPPED:", stdout.read().decode())
ssh.close()
