"""生成飞牛 NAS 用的部署包：compose.yaml + 说明文档 + 界面截图。

用法：python tools/package_feiniu.py [标签 ...]
不带参数时按当前版本号与 latest 各生成一个包，产物放在 dist/。
"""

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def app_version():
    namespace = {}
    exec((ROOT / "cmcc_app" / "__init__.py").read_text(encoding="utf-8"), namespace)  # noqa: S102
    return namespace["__version__"]


def build(tag):
    compose = (ROOT / "compose.fn.yaml").read_text(encoding="utf-8")
    notes = (ROOT / "docs" / "FEINIU.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # 包内完整 README 放在 docs/ 下，图片路径要相应去掉一层前缀
    packaged_readme = readme.replace("](docs/images/", "](images/").replace('src="docs/images/', 'src="images/')
    config = compose.replace("image: rain5201/cmcc-daily:latest", f"image: rain5201/cmcc-daily:{tag}")
    target = ROOT / "dist" / f"cmcc-feiniu-{tag}.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("compose.yaml", config)
        archive.writestr("README.md", notes)
        archive.writestr("docs/FEINIU.md", notes)
        archive.writestr("docs/README-FULL.md", packaged_readme)
        for image in sorted((ROOT / "docs" / "images").glob("*")):
            archive.write(image, f"docs/images/{image.name}")
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert "docs/images/dashboard-accounts.png" in names
        packaged = archive.read("docs/README-FULL.md").decode("utf-8")
        assert "](images/dashboard-accounts.png)" in packaged, "包内 README 的图片路径未调整"
        assert "](docs/images/" not in packaged
    print(f"{target.name}: {target.stat().st_size:,} 字节，{len(names)} 个文件")
    return target


def main():
    tags = sys.argv[1:] or ["latest", app_version()]
    for tag in tags:
        build(tag)


if __name__ == "__main__":
    main()
