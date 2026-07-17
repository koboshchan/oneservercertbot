import os
import json
import docker
import shutil

# Setup Docker client
containers = docker.from_env().containers
host_pwd = os.getenv("HOST_PWD")
# host_cert_path is the Windows path the Docker Engine needs
host_cert_path = f"{host_pwd}/cert"

def certbot(domains):
    print(f"Starting Certbot for: {domains}")
    return containers.run(
        image="certbot/certbot",
        volumes={host_cert_path: {"bind": "/etc/letsencrypt", "mode": "rw"}},
        ports={"80": 80},
        command=[
            "certonly", "-n", "--agree-tos",
            "--preferred-challenges", "http",
            "-d", ",".join(domains),
            "--standalone", "--expand",
        ],
        detach=True,
        remove=True,
    )

def chmod_certs():
    # We use host_cert_path because this is a sibling container
    return containers.run(
        image="alpine",
        volumes={host_cert_path: {"bind": "/etc/letsencrypt", "mode": "rw"}},
        command=["chmod", "-R", "777", "/etc/letsencrypt"],
        detach=True,
        remove=True,
    )

def stream(container):
    try:
        logs = container.logs(stdout=True, stderr=True, stream=True)
        for log in logs:
            print(log.decode(), end="")
    except Exception as e:
        print(f"Log error: {e}")

# 1. Stop the existing server
try:
    containers.get("oneserver").stop()
except Exception:
    pass 

# 2. Update Settings
with open("/oneserver/settings.json", "r") as f:
    content = f.read()

# Safe read stripping comments //
lines = []
for line in content.splitlines():
    pos = line.find("//")
    while pos != -1:
        if pos > 0 and line[pos - 1] == ':':
            pos = line.find("//", pos + 2)
        else:
            line = line[:pos]
            break
    lines.append(line)
settings = json.loads("\n".join(lines))

new_setting = []
domains = []

for setting in settings:
    s = setting.copy()
    connection_type = s.get("type", "https-only")
    
    is_error_handler = ":" in connection_type
    if is_error_handler:
        target_type = connection_type.split(":", 1)[1].strip()
    else:
        target_type = connection_type

    secure_types = ["https", "https-only", "static-https", "static-https-only", "redirect-temp", "redirect-perm"]
    if target_type in secure_types:
        # Support both kebab-case (standard) and snake_case variants if present
        if "ca_bundle" in s or "ca-bundle" in s:
            key_ca = "ca_bundle" if "ca_bundle" in s else "ca-bundle"
            s[key_ca] = "fullchain.pem"
        else:
            s["ca-bundle"] = "fullchain.pem"

        if "private_key" in s or "private-key" in s:
            key_pk = "private_key" if "private_key" in s else "private-key"
            s[key_pk] = "privkey.pem"
        else:
            s["private-key"] = "privkey.pem"

        # Do NOT include in standalone request if it's an error handler OR a wildcard domain
        domain = s.get("domain", "*")
        is_wildcard = domain.startswith("*")
        if not is_error_handler and not is_wildcard:
            domains.append(domain)
    new_setting.append(s)

with open("/oneserver/settings.json", "w") as f:
    json.dump(new_setting, f, indent=4)

folder = '/app/cert'
for filename in os.listdir(folder):
    file_path = os.path.join(folder, filename)
    try:
        if os.path.isfile(file_path) or os.path.islink(file_path):
            os.unlink(file_path)
        elif os.path.isdir(file_path):
            shutil.rmtree(file_path)
    except Exception as e:
        print('Failed to delete %s. Reason: %s' % (file_path, e))

# 3. Run Certbot and Fix Permissions
stream(certbot(domains=domains))
stream(chmod_certs())

# 4. Copy certificates using the 'live' path (Avoids the '1.pem' issue)
# Note: Inside THIS container, the path is /app/cert/...
cert_domain = domains[0]
if cert_domain.startswith("*."):
    cert_domain = cert_domain[2:]
source_dir = f"/app/cert/live/{cert_domain}"
dest_dir = "/oneserver/cert"

os.makedirs(dest_dir, exist_ok=True)

try:
    # We use a simple read/write to avoid symlink issues on Windows hosts
    for filename in ["fullchain.pem", "privkey.pem"]:
        src = os.path.join(source_dir, filename)
        dst = os.path.join(dest_dir, filename)
        with open(src, 'rb') as f_in, open(dst, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    print("Certificates copied successfully.")
except Exception as e:
    print(f"Copy failed: {e}")
    # Fallback: if 'live' fails because of symlink issues, you'd need to 
    # glob the archive folder for the highest number.

# 5. Restart the stack
os.system("cd /oneserver && docker compose up -d --build")