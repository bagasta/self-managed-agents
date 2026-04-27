import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("curl -sI -k http://localhost:8000/ui/")
print("CURL /ui/:", stdout.read().decode())
print("CURL STDERR:", stderr.read().decode())
ssh.close()
