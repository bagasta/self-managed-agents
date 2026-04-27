import paramiko
import requests
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect("194.238.23.242", username="clevio", password="Humancentric4725.")
stdin, stdout, stderr = ssh.exec_command("curl -s http://127.0.0.1:8080/api/rawdata")
print("Traefik API:", stdout.read().decode()[:1000])
ssh.close()
