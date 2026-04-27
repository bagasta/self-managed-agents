import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("curl -v http://localhost:8000/ui/app.js")
print("CURL /ui/app.js STDOUT:", stdout.read().decode())
print("CURL /ui/app.js STDERR:", stderr.read().decode())

stdin, stdout, stderr = ssh.exec_command("curl -s http://localhost:8000/ui/")
print("CURL /ui/ STDOUT:", stdout.read().decode())

ssh.close()
