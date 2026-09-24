# Personal Job Agent 0.8.6 源码安全复核交接

这份源码包是 2026-09-23 的本地候选版快照，不包含真实简历、个人资料数据库、密钥、安装包或构建缓存。它尚未推送 GitHub 或发布。请以代码和测试为准独立审查，不要将现有自动化测试通过视为安全证明。

## 建议优先检查

1. 本地 HTTP 服务的 Host、Origin、动作令牌和代理令牌边界：`src/job_agent/services/dashboard.py` 与 `dashboard_routes/`，尤其新增的 `/api/candidates`、`/api/profile/edit`、资料上传和状态变更。
2. 招聘 URL 提取、远端网页读取、跳转及浏览器辅助：`safe_job_fetch.py`、`browser_assist.py`、`dashboard_routes/jobs_api.py`、浏览器扩展目录。检查 SSRF、重定向、浏览器扩展权限与页面脚本注入。
3. 简历原文、联系方式、生成草稿和岗位材料的本地保存及对外传递边界：`profile_onboarding.py`、`resume_*`、`fill_bridge.py`、`services/dashboard_routes/`。重点检查换用户时是否可能串数据、材料版本核对是否可绕过。
4. 大量岗位与候选的数据库查询、分页、事务、去重和状态历史：`job_repository.py`、`match_refresh.py`、`dashboard_api.py`。确认失败时不会误报保存、覆盖已投递状态或把未核验候选提升为正式岗位。
5. 打包内容、运行时依赖和日志：`scripts/build-desktop.ps1`、`scripts/privacy_audit.py`、`packaging/`、`desktop.py`。确认默认运行不会泄露个人资料或自动提交申请。

## 已验证与范围外

- 当前工作机通过 441 项 Python、30 项 Node 检查，以及隔离数据的浏览器流程和打包 EXE 验收；详见 `docs/SIMPLIFICATION_2026-09-23.md`。
- 真实招聘网站、真实账号、外部 PDF 查看器/浏览器联动、长期高并发和比 1200 条岗位更大的规模尚未端到端验证。
- `references/dream-rsi/` 只包含来源说明和固定版本清单。原论文及作者资料存放在本机忽略目录中；因上游未提供可核实的再分发许可，未打进源码包，也未执行或接入运行时。

请按“漏洞位置、可触发条件、实际影响、最小复现步骤、修复建议”输出发现；将确认问题与推测分开。
