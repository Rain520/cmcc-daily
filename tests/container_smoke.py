"""Docker smoke test. Requires cmcc-daily:test; never calls CMCC activity APIs."""

import http.cookiejar
import http.client
import json
import os
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


def main():
    suffix = secrets.token_hex(5)
    name, volume = f"cmcc-test-{suffix}", f"cmcc-data-test-{suffix}"
    password = "seed-password-1234"
    env = dict(os.environ, CMCC_WEB_PASSWORD=secrets.token_urlsafe(24))
    image = os.environ.get("CMCC_TEST_IMAGE", "cmcc-daily:test")
    platform = ["--platform", os.environ["CMCC_TEST_PLATFORM"]] if os.environ.get("CMCC_TEST_PLATFORM") else []
    # PBKDF2 password changes are substantially slower under QEMU emulation.
    http_timeout = float(os.environ.get("CMCC_TEST_HTTP_TIMEOUT",
                         "30" if os.environ.get("CMCC_TEST_PLATFORM") == "linux/arm64" else "5"))
    uid = os.environ.get("CMCC_TEST_UID", "10001")
    gid = os.environ.get("CMCC_TEST_GID", "10001")
    mask = os.environ.get("CMCC_TEST_UMASK", "077")
    mount_type = os.environ.get("CMCC_TEST_MOUNT", "bind")

    def docker(*args):
        return subprocess.check_output(["docker", *args], env=env, text=True).strip()

    try:
        docker("volume", "create", volume)
        # A temporary Linux directory owned by root reproduces the NAS failure,
        # without using the developer's live data or Windows file permissions.
        docker("run", *platform, "--rm", "--user", "0:0", "-v", f"{volume}:/data",
               "--entrypoint", "python", image, "-c", """
import os
from pathlib import Path
from cmcc_app.auth import AuthStore
data = Path('/data')
AuthStore(data, 'seed-password-1234')
(data / 'app.lock').write_bytes(b'0')
(data / 'cache').mkdir(exist_ok=True)
(data / 'cache' / 'fixture.json').write_bytes(b'{"test":"never-send"}')
for path in (data / 'auth.json', data / 'app.lock', data / 'cache' / 'fixture.json', data / 'cache', data):
    os.chown(path, 0, 0)
    path.chmod(0)
""")
        source = (docker("volume", "inspect", volume, "--format", "{{.Mountpoint}}")
                  if mount_type == "bind" else volume)
        docker("run", *platform, "-d", "--name", name, "--init", "--read-only", "--cap-drop=ALL",
               "--cap-add=CHOWN", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
               "--cap-add=SETUID", "--cap-add=SETGID",
               "--security-opt=no-new-privileges", "--tmpfs", "/tmp:size=32m,mode=1777",
               "-p", "127.0.0.1::8000", "-v", f"{source}:/data", "-e", "CMCC_WEB_PASSWORD",
               "-e", f"PUID={uid}", "-e", f"PGID={gid}", "-e", f"UMASK={mask}",
               image)
        port = docker("port", name, "8000/tcp").split(":")[-1]
        base = f"http://127.0.0.1:{port}"
        client = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, method="GET", body=None, csrf=""):
            req = Request(base + path, method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
            with client.open(req, timeout=http_timeout) as response:
                return json.load(response)

        def wait_ready():
            for _ in range(60):
                try:
                    if request("/healthz")["status"] == "ok":
                        return
                except (URLError, HTTPError, http.client.RemoteDisconnected, ConnectionError, TimeoutError):
                    time.sleep(1)
            raise AssertionError("container did not become healthy")

        wait_ready()
        csrf = request("/api/login", "POST", {"password": password})["csrf"]
        account = request("/api/accounts", "POST", {
            "name": "Docker smoke test", "config": {
                "phone": "13800001234", "app_token": "test-never-send",
                "webhook_url": "https://example.com/test-never-send", "webhook_token": "test-hook-token"},
            "enabled": False,
        }, csrf)["account"]
        assert "app_token" not in account["config"]
        assert "webhook_url" not in account["config"]
        assert "webhook_token" not in account["config"]
        assert account["has_webhook_url"] and account["has_webhook_token"]
        assert account["next_run"] is None
        probe = request(f"/api/accounts/{account['id']}/notify-test", "POST",
                        {"webhook_url": "http://127.0.0.1/never-send"}, csrf)
        assert probe["results"][0]["ok"] is False, "invalid webhook URL must not be sent"
        assert "HTTPS" in probe["results"][0]["message"]
        changed = request("/api/password", "POST",
                          {"current_password": password, "new_password": "smoke-password-1234"}, csrf)
        assert changed["weak_password"] is False
        csrf = changed["csrf"]
        try:
            request("/api/login", "POST", {"password": password})
            raise AssertionError("the previous password still works")
        except HTTPError as error:
            assert error.code == 401
        password = "smoke-password-1234"
        docker("restart", "--time", "30", name)
        port = docker("port", name, "8000/tcp").split(":")[-1]
        base = f"http://127.0.0.1:{port}"
        wait_ready()
        saved = request("/api/accounts")["accounts"]
        assert saved[0]["id"] == account["id"], "account did not survive restart"
        assert saved[0]["has_webhook_url"] and saved[0]["has_webhook_token"]
        try:
            request("/api/login", "POST", {"password": env["CMCC_WEB_PASSWORD"]})
            raise AssertionError("CMCC_WEB_PASSWORD overrode the password stored in the volume")
        except HTTPError as error:
            assert error.code == 401
        csrf = request("/api/login", "POST", {"password": password})["csrf"]
        cleared = request(f"/api/accounts/{account['id']}", "PUT", {
            "clear_secrets": ["webhook_url", "webhook_token"]}, csrf)["account"]
        assert not cleared["has_webhook_url"] and not cleared["has_webhook_token"]
        assert request("/api/runs")["runs"] == [], "disabled account ran unexpectedly"
        request(f"/api/accounts/{account['id']}", "DELETE", csrf=csrf)
        assert request("/api/accounts")["accounts"] == []
        processes = json.loads(docker("exec", name, "python", "-c", """
import json
from pathlib import Path
found = []
for process in Path('/proc').glob('[0-9]*'):
    try:
        args = (process / 'cmdline').read_bytes().split(b'\\0')
        if b'cmcc_app' in args and b'serve' in args:
            found.append(dict(line.split(':', 1) for line in
                              (process / 'status').read_text().splitlines() if ':' in line))
    except (FileNotFoundError, ProcessLookupError):
        pass
print(json.dumps(found))
"""))
        assert len(processes) == 1, processes
        assert processes[0]["Uid"].split() == [uid] * 4, processes
        assert processes[0]["Gid"].split() == [gid] * 4, processes
        assert int(processes[0]["Umask"], 8) == int(mask, 8), processes
        if uid != "0":
            assert int(processes[0]["CapEff"], 16) == 0, processes
        assert processes[0]["NoNewPrivs"].strip() == "1", processes
        mount = json.loads(docker("inspect", name))[0]["Mounts"]
        assert next(item for item in mount if item["Destination"] == "/data")["Type"] == mount_type
        ownership = json.loads(docker("exec", name, "python", "-c", """
import json, stat
from pathlib import Path
paths = [Path('/data'), Path('/data/app.lock'), Path('/data/auth.json'),
         Path('/data/cmcc.sqlite3'), Path('/data/cache/fixture.json')]
print(json.dumps([(p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode)) for p in paths]))
assert paths[-1].read_bytes() == b'{"test":"never-send"}'
"""))
        assert all(owner[:2] == [int(uid), int(gid)] for owner in ownership), ownership
        assert ownership[2][2] == 0o600 and ownership[3][2] == 0o600, ownership
        docker("exec", name, "python", "-m", "cmcc_app.bootstrap", "healthcheck")
        print(f"Docker smoke passed: {mount_type}, UID/GID={uid}:{gid}, UMASK={mask}; "
              "permission repair, login, password change, CRUD, restart persistence, "
              "runtime identity/capabilities, read-only filesystem, healthcheck")
    except Exception:
        subprocess.run(["docker", "logs", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=False)
        subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
