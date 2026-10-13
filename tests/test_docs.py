"""文档中的图片与链接必须真实存在，避免仓库里的说明失效。"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = ("README.md", "AGENTS.md", "docs/FEINIU.md", "docs/PUBLISHING.md", "docs/VALIDATION.md")
PREVIEW_IMAGES = ("dashboard-accounts.png", "dashboard-history.png",
                  "dashboard-accounts-mobile.jpg", "dashboard-history-mobile.jpg")


def referenced_paths(text):
    targets = re.findall(r"\]\(([^)\s]+)", text)
    targets += re.findall(r'<img[^>]+src="([^"]+)"', text)
    return [target for target in targets if not target.startswith(("http://", "https://", "#"))]


class DocumentLinkTests(unittest.TestCase):
    def test_referenced_files_exist(self):
        checked = 0
        for name in DOCUMENTS:
            document = ROOT / name
            self.assertTrue(document.exists(), name)
            for target in referenced_paths(document.read_text(encoding="utf-8")):
                path = (document.parent / target.split("#")[0]).resolve()
                with self.subTest(document=name, target=target):
                    self.assertTrue(path.exists(), f"{name} 引用了不存在的文件 {target}")
                    checked += 1
        self.assertGreater(checked, 5)

    def test_preview_images_are_present(self):
        for name in PREVIEW_IMAGES:
            image = ROOT / "docs" / "images" / name
            self.assertTrue(image.exists(), name)
            self.assertGreater(image.stat().st_size, 20_000, name)

    def test_readme_shows_the_interface(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## 界面预览", readme)
        for name in PREVIEW_IMAGES:
            self.assertIn(f"docs/images/{name}", readme, name)


if __name__ == "__main__":
    unittest.main()
