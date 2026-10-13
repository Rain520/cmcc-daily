import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from . import __version__
from .auth import AuthStore
from .backup import (MAX_FILE_BYTES, apply_payload, build_payload, check_passphrase,
                      decrypt_payload, encrypt_payload)
from .logging_utils import redact
from .models import (DATE_PATTERN, RUN_SOURCES, RUN_STATUSES, TASKS, Store,
                     normalize_account, public_account)
from .runner import Manager
from .storage import account_key, atomic_json

RUN_PAGE_SIZES = (10, 20, 50, 100)
RUN_SEARCH_LIMIT = 1000


def create_app(settings, start_manager=False):
    app = Flask(__name__)
    store = Store(settings.data_dir)
    secret_path = settings.data_dir / "session-key.json"
    if not secret_path.exists():
        atomic_json(secret_path, {"key": secrets.token_hex(32)})
    # 管理密码保存在数据卷中：首次启动用环境变量初始化，之后在网页修改。
    auth = AuthStore(settings.data_dir, settings.password,
                     reset=os.environ.get("CMCC_PASSWORD_RESET", "").lower() == "true")
    key = json.loads(secret_path.read_text(encoding="utf-8"))["key"]

    def session_secret():
        # 密码变化后签名材料随之变化，旧登录会话立即失效。
        return hmac.new(key.encode(), auth.fingerprint.encode(), hashlib.sha256).hexdigest()

    app.config.update(
        SECRET_KEY=session_secret(),
        # 导入备份需要上传文件，其余接口只接收很小的 JSON 请求。
        MAX_CONTENT_LENGTH=MAX_FILE_BYTES + 64 * 1024, SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=settings.secure_cookie,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), SESSION_REFRESH_EACH_REQUEST=False,
    )
    manager = Manager(store, settings)
    app.extensions.update(cmcc_store=store, cmcc_manager=manager, cmcc_settings=settings,
                          cmcc_auth=auth)
    attempts, attempts_lock = {}, threading.Lock()

    def authenticated(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not session.get("authenticated"):
                return jsonify(error="请先登录"), 401
            if request.method != "GET":
                if not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), session.get("csrf", "!").encode()):
                    return jsonify(error="会话校验失败，请刷新后重试"), 403
            return fn(*args, **kwargs)
        return wrapped

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("请求必须为 JSON 对象")
        return value

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def validation(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error={400: "请求格式有误", 404: "未找到该内容", 413: "请求内容过大",
                              415: "请使用 JSON 请求"}.get(error.code, "请求无法处理")), error.code

    @app.get("/")
    def index():
        # 资源地址带版本号：升级镜像后浏览器一定拿到新的页面与静态文件。
        return render_template("index.html", version=__version__)

    @app.get("/healthz")
    def health():
        return jsonify(status="ok" if manager.healthy() else "unhealthy"), 200 if manager.healthy() else 503

    def failures(address, now):
        with attempts_lock:
            for ip in [ip for ip, (_, until) in attempts.items() if until <= now]:
                attempts.pop(ip, None)
            return attempts.get(address, (0, now + 300))

    def fail(address, count, until):
        with attempts_lock:
            if len(attempts) >= 2048:
                return False
            attempts[address] = (count + 1, until)
            return True

    def clear_failures(address):
        with attempts_lock:
            attempts.pop(address, None)

    def start_session():
        session.clear()
        session.update(authenticated=True, csrf=secrets.token_urlsafe(32))
        session.permanent = True
        return {"csrf": session["csrf"], "timezone": settings.timezone,
                "weak_password": auth.is_default}

    @app.post("/api/login")
    def login():
        password = body().get("password", "")
        if not isinstance(password, str):
            raise ValueError("请输入管理密码")
        address, now = request.remote_addr, time.monotonic()
        count, until = failures(address, now)
        if count >= 5:
            return jsonify(error="尝试次数过多，请 5 分钟后再试"), 429
        if not auth.verify(password):
            if not fail(address, count, until):
                return jsonify(error="登录繁忙，请稍后重试"), 429
            return jsonify(error="管理密码不正确"), 401
        clear_failures(address)
        return jsonify(**start_session())

    @app.get("/api/session")
    @authenticated
    def current_session():
        return jsonify(csrf=session["csrf"], timezone=settings.timezone,
                       weak_password=auth.is_default)

    @app.post("/api/logout")
    @authenticated
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/password")
    @authenticated
    def change_password():
        data = body()
        current, new = data.get("current_password", ""), data.get("new_password", "")
        if not isinstance(current, str) or not isinstance(new, str):
            raise ValueError("请输入当前密码和新密码")
        address, now = request.remote_addr, time.monotonic()
        count, until = failures(address, now)
        if count >= 5:
            return jsonify(error="尝试次数过多，请 5 分钟后再试"), 429
        if not auth.verify(current):
            if not fail(address, count, until):
                return jsonify(error="登录繁忙，请稍后重试"), 429
            return jsonify(error="当前密码不正确"), 401
        clear_failures(address)
        if hmac.compare_digest(current, new):
            raise ValueError("新密码不能与当前密码相同")
        auth.set_password(new)
        app.config["SECRET_KEY"] = session_secret()
        logging.getLogger("cmcc_app").info("管理密码已更新，此前的登录会话已失效")
        return jsonify(ok=True, **start_session())

    @app.get("/api/accounts")
    @authenticated
    def accounts():
        return jsonify(accounts=[public_account(a) for a in store.accounts()],
                       healthy=manager.healthy(), pending=store.pending_account_ids())

    @app.post("/api/accounts")
    @authenticated
    def add_account():
        account = normalize_account(body(), timezone_name=settings.timezone)
        store.save(account)
        return jsonify(account=public_account(account)), 201

    @app.put("/api/accounts/<account_id>")
    @authenticated
    def update_account(account_id):
        old = store.account(account_id)
        if not old:
            return jsonify(error="账号不存在"), 404
        account = normalize_account(body(), old, settings.timezone)
        store.save(account)
        return jsonify(account=public_account(account))

    @app.delete("/api/accounts/<account_id>")
    @authenticated
    def delete_account(account_id):
        account = store.account(account_id)
        if not account:
            return jsonify(error="账号不存在"), 404
        store.delete(account_id)
        (settings.data_dir / "cache" / f"jwt-{account_key(account['config']['phone'])}.json").unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.post("/api/accounts/<account_id>/run")
    @authenticated
    def run_account(account_id):
        data = body()
        task, dry_run = data.get("task", "sign"), data.get("dry_run", False)
        if not isinstance(task, str) or task not in TASKS or not isinstance(dry_run, bool):
            raise ValueError("不支持该任务或参数")
        if not manager.healthy():
            return jsonify(error="调度器未运行，请检查容器状态"), 503
        return jsonify(run_id=store.enqueue(account_id, task, dry_run)), 202

    def page_number(value, default, allowed=None):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if allowed is None or number in allowed else default

    def run_query(args):
        """解析执行记录的筛选参数；日期按设置时区换算为 UTC 边界（结束日期不含当天之后）。"""
        def text(name, limit=64):
            value = args.get(name)
            return value.strip()[:limit] or None if isinstance(value, str) else None

        def day(value, offset=0):
            if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value.strip()):
                return None
            try:
                moment = datetime.strptime(value.strip(), "%Y-%m-%d").replace(
                    tzinfo=ZoneInfo(settings.timezone))
            except ValueError:
                return None
            return (moment + timedelta(days=offset)).astimezone(timezone.utc).isoformat()

        task, status, source = text("task", 16), text("status", 16), text("source", 16)
        return {"account_id": text("account_id", 64),
                "task": task if task in TASKS else None,
                "status": status if status in RUN_STATUSES else None,
                "source": source if source in RUN_SOURCES else None,
                "date_from": day(args.get("date_from")),
                "date_to": day(args.get("date_to"), 1),
                "keyword": text("keyword", 64)}

    @app.get("/api/runs")
    @authenticated
    def runs():
        filters = run_query(request.args)
        page_size = page_number(request.args.get("page_size"), 20, RUN_PAGE_SIZES)
        page = max(1, page_number(request.args.get("page"), 1))
        keyword = filters.pop("keyword")
        extra = {}
        if keyword:
            # 关键字同时匹配记录字段与日志正文；最多扫描最近 RUN_SEARCH_LIMIT 条记录。
            candidates, matched_total = store.runs_page(page=1, page_size=RUN_SEARCH_LIMIT, **filters)
            hits = store.search_run_logs(keyword, candidates)
            folded = keyword.casefold()
            rows = [row for row in candidates if row["id"] in hits
                    or folded in (row["message"] or "").casefold()
                    or folded in (row["account_name"] or "").casefold()]
            total = len(rows)
            page_rows = rows[(page - 1) * page_size: page * page_size]
            extra = {"keyword": keyword, "scanned": len(candidates),
                     "truncated": matched_total > len(candidates)}
        else:
            page_rows, total = store.runs_page(page=page, page_size=page_size, **filters)
        latest = store.runs(limit=1)
        return jsonify(runs=page_rows, total=total, page=page, page_size=page_size,
                       pages=max(1, -(-total // page_size)),
                       latest=latest[0] if latest else None,
                       counts=store.run_status_counts(**filters), **extra)

    @app.post("/api/runs/delete")
    @authenticated
    def delete_runs():
        data = body()
        ids = data.get("ids")
        if isinstance(ids, list):
            if not ids or len(ids) > 500 or any(not isinstance(value, str) or not value for value in ids):
                raise ValueError("请选择要删除的执行记录")
            deleted, pending = store.delete_runs(ids)
            skipped = len(pending)
        elif data.get("scope") == "filtered":
            filters = run_query({key: value for key, value in (data.get("filters") or {}).items()
                                 if isinstance(value, str)})
            filters.pop("keyword", None)  # 关键字命中日志的判定不参与批量删除，避免误删
            deleted, skipped = store.clear_runs(**filters), 0
        else:
            raise ValueError("请选择要删除的执行记录")
        logging.getLogger("cmcc_app").info("已删除 %s 条执行记录，保留进行中的 %s 条", deleted, skipped)
        return jsonify(ok=True, deleted=deleted, skipped=skipped)

    @app.get("/api/runs/<run_id>/log")
    @authenticated
    def run_log(run_id):
        run = store.run(run_id)
        if not run:
            return jsonify(error="任务不存在"), 404
        path = settings.data_dir / "logs" / f"{run['id']}.log"
        text = ""
        if path.exists():
            with path.open("rb") as stream:
                stream.seek(0, 2)
                tail = stream.tell() > 128 * 1024
                stream.seek(max(0, stream.tell() - 128 * 1024))
                text = stream.read().decode("utf-8", errors="replace")
                if tail:
                    text = "（仅显示日志末尾 128 KiB）\n" + text
        account = store.account(run["account_id"])
        return jsonify(log=redact(text, account["config"] if account else None), run=run)

    @app.post("/api/accounts/<account_id>/notify-test")
    @authenticated
    def notify_test(account_id):
        account = store.account(account_id)
        if not account:
            return jsonify(error="账号不存在"), 404
        data = body()
        config = dict(account["config"])
        for field in ("serverchan_sendkey", "bark_url", "webhook_url", "webhook_token"):
            value = data.get(field, "")
            if not isinstance(value, str):
                raise ValueError("测试通知的字段必须为文本")
            # 只用于本次测试：表单里刚填写的值优先于已保存的值。
            if value.strip():
                config[field] = value.strip()
        from cmcc_sign import Config, masked_phone, notify_channels
        notification = Config(None)
        notification.serverchan_sendkey = config.get("serverchan_sendkey", "")
        notification.bark_url = config.get("bark_url", "")
        notification.webhook_url = config.get("webhook_url", "")
        notification.webhook_token = config.get("webhook_token", "")
        detail = (f"这是一条测试通知，说明通知渠道可以正常接收。\n"
                  f"账号：{account['name']}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        results = notify_channels(notification, f"{masked_phone(config.get('phone'))} 通知测试",
                                  detail, timeout=8)
        if not results:
            raise ValueError("请先填写至少一个通知渠道（Server酱、Bark 或 Webhook）")
        return jsonify(results=results)

    @app.post("/api/runs/<run_id>/cancel")
    @authenticated
    def cancel_run(run_id):
        run = store.run(run_id)
        if not run:
            return jsonify(error="任务不存在"), 404
        if run["status"] not in ("queued", "running"):
            raise ValueError("任务已结束")
        manager.cancel(run_id)
        return jsonify(ok=True)

    @app.post("/api/backup/export")
    @authenticated
    def backup_export():
        data = body()
        # 导出文件可以还原全部凭证，因此要求重新输入当前密码并沿用登录限速。
        address, now = request.remote_addr, time.monotonic()
        count, until = failures(address, now)
        if count >= 5:
            return jsonify(error="密码尝试次数过多，请 5 分钟后再试"), 429
        password = data.get("password", "")
        if not isinstance(password, str) or not auth.verify(password):
            fail(address, count, until)
            return jsonify(error="当前管理密码不正确"), 403
        clear_failures(address)
        passphrase = check_passphrase(data.get("passphrase"))
        flags = {}
        for field, default in (("include_password", True), ("include_history", True), ("include_cache", True)):
            value = data.get(field, default)
            if not isinstance(value, bool):
                raise ValueError("导出选项必须为布尔值")
            flags[field] = value
        payload, stats = build_payload(store, auth, settings, **flags)
        raw = encrypt_payload(payload, passphrase)
        logging.getLogger("cmcc_app").info(
            "已导出备份：%s 个账号、%s 条执行记录，包含管理密码=%s", stats["accounts"], stats["runs"], stats["password"])
        filename = f"cmcc-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        return Response(raw, mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/backup/import")
    @authenticated
    def backup_import():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise ValueError("请选择备份文件")
        raw = upload.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("备份文件超过 8 MiB")
        payload = decrypt_payload(raw, check_passphrase(request.form.get("passphrase", "")))
        summary = apply_payload(
            store, auth, payload,
            mode=request.form.get("mode", "merge"),
            restore_password=request.form.get("restore_password", "true") == "true",
            import_history=request.form.get("import_history", "true") == "true",
            timezone_name=settings.timezone)
        if summary["password_restored"]:
            # 会话签名材料随密码变化，当前页面用新会话继续使用，其他会话失效。
            app.config["SECRET_KEY"] = session_secret()
            logging.getLogger("cmcc_app").info("已按备份恢复管理密码，此前的登录会话已失效")
            session_state = start_session()
        else:
            session_state = {"csrf": session["csrf"], "timezone": settings.timezone,
                             "weak_password": auth.is_default}
        logging.getLogger("cmcc_app").info(
            "已导入备份：新增 %s 个账号，跳过 %s 个，历史记录 %s 条",
            summary["added"], summary["skipped_total"], summary["runs"])
        return jsonify(ok=True, **summary, **session_state)

    if start_manager:
        manager.start()
    return app
