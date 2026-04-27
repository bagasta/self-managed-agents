import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("curl -s http://localhost:8000/ui/ | head -n 10")
print("CURL /ui/:", stdout.read().decode())
print("CURL STDERR:", stderr.read().decode())
ssh.close()
