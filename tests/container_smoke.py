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
    password = secrets.token_urlsafe(24)
    env = dict(os.environ, CMCC_WEB_PASSWORD=password)

    def docker(*args):
        return subprocess.check_output(["docker", *args], env=env, text=True).strip()

    try:
        docker("volume", "create", volume)
        docker("run", "-d", "--name", name, "--init", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--tmpfs", "/tmp:size=32m,mode=1777",
               "-p", "127.0.0.1::8000", "-v", f"{volume}:/data", "-e", "CMCC_WEB_PASSWORD",
               "cmcc-daily:test")
        port = docker("port", name, "8000/tcp").split(":")[-1]
        base = f"http://127.0.0.1:{port}"
        client = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def request(path, method="GET", body=None, csrf=""):
            req = Request(base + path, method=method,
                          data=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
            with client.open(req, timeout=5) as response:
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
        assert docker("exec", name, "id", "-u") == "10001"
        docker("exec", name, "python", "-m", "cmcc_app", "healthcheck")
        print("Docker smoke passed: login, password change, CRUD, persistence, non-root, "
              "read-only filesystem, healthcheck")
    except Exception:
        subprocess.run(["docker", "logs", name], check=False)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, check=False)
        subprocess.run(["docker", "volume", "rm", volume], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
