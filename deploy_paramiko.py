import paramiko
import tarfile
import os

print("Membuat tarball...")
tar_filename = "/tmp/managed-agents-deploy.tar.gz"
with tarfile.open(tar_filename, "w:gz") as tar:
    for root, dirs, files in os.walk("."):
        if any(ignored in root for ignored in [".git", "venv", ".venv", "__pycache__", "wa-store", "wa-dev-store", "node_modules"]):
            continue
        for file in files:
            path = os.path.join(root, file)
            # tambahkan filter file
            if "project_deploy.tar.gz" not in path and "managed-agents-deploy.tar.gz" not in path:
                tar.add(path, arcname=path)

print("Tarball berhasil dibuat.")

host = "194.238.23.242"
user = "clevio"
password = "Humancentric4725."

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
print("Menyambungkan ke VPS...")
ssh.connect(host, username=user, password=password)

sftp = ssh.open_sftp()
print("Mengunggah tarball via SFTP...")
remote_tar_path = "/home/clevio/managed-agents-deploy.tar.gz"
sftp.put(tar_filename, remote_tar_path)
sftp.close()

target_dir = "/home/clevio/stack/managed-agents"
print(f"Mengekstrak tarball di {target_dir}...")
ssh.exec_command(f"mkdir -p {target_dir}")

cmd_extract = f"tar -xzf {remote_tar_path} -C {target_dir}"
stdin, stdout, stderr = ssh.exec_command(cmd_extract)
exit_status = stdout.channel.recv_exit_status()
print("Extract STDOUT:", stdout.read().decode())
print("Extract STDERR:", stderr.read().decode())

print("Menjalankan deployment via docker compose...")
# Gunakan sudo untuk menjalankan docker compose. Pass the password to stdin of sudo -S
# Use --build to force rebuild the api container
cmd_deploy = f"echo '{password}' | sudo -S docker compose -f {target_dir}/deploy/docker-compose.prod.yml up -d --build"
stdin, stdout, stderr = ssh.exec_command(cmd_deploy)
exit_status = stdout.channel.recv_exit_status()
print("Docker Compose STDOUT:", stdout.read().decode())
print("Docker Compose STDERR:", stderr.read().decode())

print("Menjalankan database migration (alembic)...")
cmd_migrate = f"echo '{password}' | sudo -S docker exec deploy-api-1 alembic upgrade head"
stdin, stdout, stderr = ssh.exec_command(cmd_migrate)
exit_status = stdout.channel.recv_exit_status()
print("Alembic STDOUT:", stdout.read().decode())
print("Alembic STDERR:", stderr.read().decode())

ssh.close()
print("Deployment Selesai!")
