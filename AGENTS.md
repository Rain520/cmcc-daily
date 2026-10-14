# AGENTS.md

本文件记录本仓库的开发约定，供参与本项目的人和自动化助手共同遵守。

## 数据与隐私

- **示例数据必须完全虚构**：预览实例、界面截图、文档示例、测试夹具一律使用自己编写的示例号码
  （如 `13800000011`、`13700000022`）和占位凭证（如 `preview-token`），不得使用真实手机号、
  真实 App Token、真实 Webhook 地址，也不得使用从真实数据推导出来的片段（包括掩码形式）。
- 测试版先在 Docker 里起审核预览：`docker compose -f compose.preview.yaml up -d --build`（容器内
  `CMCC_PREVIEW=1`，见 `cmcc_app/preview.py`），确认通过后再发布。预览数据由 `tools/preview_seed.py`
  生成，不要手工塞入真实数据，也不要在预览里发送通知或导入真实备份。
- 不要提交 `.env`、`data/`、`dist/`、真实抓包结果或任何凭证；提交邮箱使用 GitHub noreply 地址。

## 代码约定

- 面向 Python 3.12。新增依赖前先确认 `requirements.txt`（Flask、waitress、requests、cryptography、tzdata）。
- 新增或修改行为时同步补测试：提交前运行 `python -m unittest discover -s tests -q`，
  涉及容器行为时再运行 `python tests/container_smoke.py`。
- 前端只用仓库内资源（不引入 CDN）：模板 `cmcc_app/templates/index.html`、脚本
  `cmcc_app/static/app.js`、样式 `cmcc_app/static/styles.css`。改动界面元素时，脚本里的
  `$("#id")` 选择器必须能在模板中找到，测试会校验。
- 容器约束（只读根文件系统、非 root 运行、数据目录权限自动初始化、能力最小化）不要随意放宽。

## 文档与发布

- 行为变化同步更新文档：部署写入 `docs/FEINIU.md`，发布流程与镜像摘要写入
  `docs/PUBLISHING.md`，验证结论写入 `docs/VALIDATION.md`，功能总览写入 `README.md`。
- 版本号同时出现在 `cmcc_app/__init__.py`、`Dockerfile` 的 LABEL、`compose.image.yaml`
  和 `.github/workflows/publish.yml` 的默认标签，四处要一起改。
- 发布前先跑测试与容器冒烟测试，再在本机 Docker 预览里自检一遍；未得到确认前不要
  `git push`、也不要把镜像推到 Docker Hub。飞牛部署包用 `python tools/package_feiniu.py` 生成。
