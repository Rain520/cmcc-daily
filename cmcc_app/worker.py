"""One isolated upstream task. Only the manager creates its private input file."""

import importlib
import json
import logging
import signal
import sys
from pathlib import Path

from .logging_utils import RedactingFormatter
from . import results
from .storage import atomic_json

MODULES = {"sign": "cmcc_sign", "wed": "cmcc_wed", "rate": "cmcc_rate", "fee": "cmcc_fee", "seckill": "cmcc_seckill"}


def task_args(task, path, options, dry_run, settings=None):
    args = ["--config", str(path)]
    if dry_run:
        args.append("--dry-run")
    if task == "sign":
        if not options.get("claim", True):
            args.append("--no-claim")
        args.extend(f"--{flag}" for flag in ("tasks", "games", "wed") if options.get(flag))
    elif task == "seckill":
        # 上游秒杀脚本自带这几个开关，传入活动卡片的配置；缺失或非法时留给脚本用自己的默认值。
        for flag, key, convert in (("--lead", "lead", float), ("--interval", "interval", float),
                                   ("--max-attempts", "max_attempts", int)):
            try:
                value = convert((settings or {})[key])
            except (KeyError, TypeError, ValueError):
                continue
            args.extend([flag, f"{value:g}" if isinstance(value, float) else str(value)])
    return args


def task_settings(extra):
    """秒杀任务的第 5 个参数：活动参数 JSON；其他任务或格式异常时按空配置处理。"""
    if not extra:
        return {}
    try:
        value = json.loads(extra[0] or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def main():
    task, config_path, options_json, dry_run, *extra = sys.argv[1:]
    settings = task_settings(extra)
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(config))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    if task == "seckill" and settings:
        logging.info("秒杀活动：%s（开抢时间 %s，提前量 %s 秒，重试间隔 %s 秒，最多 %s 次）",
                     settings.get("name") or "未命名", settings.get("time") or "默认",
                     settings.get("lead"), settings.get("interval"), settings.get("max_attempts"))
    results.start(task, json.loads(options_json), dry_run == "1" and task != "fee")
    code = 1
    try:
        module = importlib.import_module(MODULES[task])
        sys.argv = [module.__file__,
                    *task_args(task, config_path, json.loads(options_json), dry_run == "1", settings)]
        code = module.main()
        return code
    except KeyboardInterrupt:
        logging.info("任务已停止")
        code = 130
        return code
    except Exception as exc:
        results.error(exc)
        logging.exception("任务执行异常")
        return 1
    finally:
        atomic_json(Path(config_path).with_suffix(".result.json"), results.finish(code))


if __name__ == "__main__":
    raise SystemExit(main())
