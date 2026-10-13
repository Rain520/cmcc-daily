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

MODULES = {"sign": "cmcc_sign", "wed": "cmcc_wed", "rate": "cmcc_rate", "fee": "cmcc_fee"}


def task_args(task, path, options, dry_run):
    args = ["--config", str(path)]
    if dry_run and task != "fee":
        args.append("--dry-run")
    if task == "sign":
        if not options.get("claim", True):
            args.append("--no-claim")
        args.extend(f"--{flag}" for flag in ("tasks", "games", "wed") if options.get(flag))
    return args


def main():
    task, config_path, options_json, dry_run = sys.argv[1:]
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(config))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    results.start(task, json.loads(options_json), dry_run == "1" and task != "fee")
    code = 1
    try:
        module = importlib.import_module(MODULES[task])
        sys.argv = [module.__file__, *task_args(task, config_path, json.loads(options_json), dry_run == "1")]
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
