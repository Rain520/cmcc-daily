r"""预览 / 截图用的示例数据（完全虚构，不联网）。

约定：预览实例与截图里**只能用完全虚构的数据**，不得使用真实手机号、真实 App Token
或真实通知地址；示例号码统一为 138/137/139 开头的示例号，凭证写成 preview-token。

数据生成逻辑放在 `cmcc_app/preview.py`：容器预览（`CMCC_PREVIEW=1`，见
`compose.preview.yaml`）启动时自动调用它；这个脚本用于给**空的独立目录**手动生成示例数据。

用法：

    .\.venv\Scripts\python.exe tools\preview_seed.py --data-dir test-results/preview-1.6.7 --status-demo
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cmcc_app.preview import seed as seed_preview


def main(days=25, seed=20261013, data_dir="/data", status_demo=False):
    seed_preview(data_dir, days=days, random_seed=seed, status_demo=status_demo)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="向空的独立预览目录写入完全虚构的示例数据")
    parser.add_argument("--data-dir", default="/data", help="独立预览数据目录，已有账号或记录时会拒绝写入")
    parser.add_argument("--days", type=int, default=25, help="生成多少天的示例执行记录")
    parser.add_argument("--seed", type=int, default=20261013, help="随机种子，保证示例数据可复现")
    parser.add_argument("--status-demo", action="store_true",
                        help="改为 6 个覆盖「正常／凭证失效／部分异常／正在执行／排队中／尚未验证」的账号")
    args = parser.parse_args()
    main(days=args.days, seed=args.seed, data_dir=args.data_dir, status_demo=args.status_demo)
