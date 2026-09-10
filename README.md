# 个人求职 Agent

> **English:** A local-first Windows job-search companion for explainable JD matching,
> evidence-based resume tailoring, application tracking, commute checks and interview
> preparation. Bring your own AI/search/map providers or use the offline baseline.

[MIT License](LICENSE) · [Privacy](PRIVACY.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

这是一个面向 Windows、可以复制给不同使用者独立运行的本地优先求职系统。当前已实现 **Phase 1–12 的可运行基础闭环**：

- 维护可追溯的真实经历 Profile
- 读取 TXT、Markdown、DOCX、PDF 简历
- 读取原始 JD
- 生成可解释的匹配评分与硬门槛检查
- 使用可替换的 AI Provider 接口做结构化 JD 分析：本地规则、本机 Codex、OpenAI、OpenAI 兼容 API
- 独立配置博查或 Brave Search；不连接搜索 API 时仍可手动导入 JD
- 搜索候选岗位、去重并保存到 SQLite 岗位库
- 从岗位编号一键生成可追溯的投递材料包，并自动关联已有定向 STAR 简历
- 建立域名白名单投递会话，只打开岗位页并准备专用简历文件，网页操作由本人完成
- 在本地 Dashboard 中完成新用户建档、简历识别、API 连接、路线通勤筛选和投递进度补录
- 在工作台内与 Agent 对话，并把“岗位专属简历草稿 → 本人审阅 → 打开岗位页与简历”绑定到每一个真实职位

系统的真实性规则是：只有标记为 `documented`（有文档依据）或
`user_confirmed`（用户确认）的事实，才能成为投递材料或匹配证据。
`needs_confirmation` 内容只会进入待确认清单，不能写入简历。

## Windows 桌面版（推荐）

桌面发行包是 `dist/PersonalJobAgent-0.8.2-Windows-x64-<构建时间>.zip`。对方不需要安装
Python：完整解压后双击 `PersonalJobAgent.exe`，程序会在独立 Windows 窗口中
打开，不会占用浏览器标签页。

0.8.2 新增本机 Codex 接入、根据完整已确认经历库生成 JD 定向简历、黑白 A4
默认模板，以及已投递岗位筛选与分组。对话可以分析简历并提供带占位符的写作模板；
AI 写作失败时会明确提示本地回退。详细变化见 [更新记录](CHANGELOG.md)。

使用 Codex 时，需要另外安装并登录本机 Codex CLI，并具备可用模型权限与额度。
在设置中选择 Codex，模型可填写 `codex-default`；无需填写或复制 API Key。
程序通过临时 CLI 会话请求文本回答，不读取或复制登录凭据文件。其他 AI Provider
和本地规则仍可独立使用。

每位 Windows 用户的 Profile、简历、API Key、SQLite 岗位库和投递记录默认
保存在自己的 `%LOCALAPPDATA%\PersonalJobAgent`，不会写进程序目录。把发行 ZIP
复制给别人时不会夹带开发者或上一位使用者的数据。

公开仓库只包含程序源码、合成测试数据和空的数据目录占位符，不包含维护者的
简历、姓名、联系方式、地址、API Key、岗位库、浏览器登录状态或旧私有 Git 历史。
每次提交与发行前都会运行 `scripts/privacy_audit.py`；完整边界见 [PRIVACY.md](PRIVACY.md)。

如需重新构建桌面发行包：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-desktop.ps1
```

浏览器投递准备所需的 Chromium 体积较大，因此不塞进主程序。
需要该功能时，对方可双击发行目录中的 `Install-Optional-Browser.cmd` 单独安装；
岗位匹配、简历、API 连接、SQLite 和 Dashboard 不依赖它。

当前 ZIP 未使用商业代码签名证书签名。正式公开分发前建议购买 Windows 代码签名
证书并签名 EXE；这不影响程序本身在其他 64 位 Windows 10/11 电脑上运行。

## 从源码首次安装

1. 从 <https://www.python.org/downloads/windows/> 安装 64 位 Python 3.13
   （项目兼容 3.12/3.13），并在安装器中勾选 **Add python.exe to PATH**。
2. 双击根目录的 **`安装并启动求职Agent.cmd`**。它会创建隔离环境、安装依赖和浏览器运行组件，然后打开本地工作台。
3. 以后只需双击 **`启动求职Agent.cmd`**。

需要排查安装问题时，也可以在项目目录运行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
   ```

检查环境：

   ```powershell
   .\.venv\Scripts\python.exe -m job_agent doctor
   ```

## 每个使用者连接自己的 API

打开 Dashboard 右上角的“连接与 API”，可以独立选择：

- **AI 分析**：本地规则（无需密钥）、OpenAI API、OpenAI 兼容 API
- **岗位搜索**：不连接搜索、博查 Search、Brave Search
- **通勤路线**：不连接地图时手动估算，或使用高德地图 Web 服务计算路线、时间与距离

OpenAI 官方适配器使用 Responses API 的结构化输出；兼容适配器使用 Chat Completions + JSON mode，因此第三方服务必须真正支持这两项兼容能力。OpenAI 官方文档也建议在模型支持时优先使用 JSON Schema Structured Outputs：<https://developers.openai.com/api/reference/cli/resources/beta/subresources/responses>。

保存后的密钥只写入 `data/private/app-settings.json`，Dashboard 只返回“是否已配置”，不会回显密钥。界面同时记录本软件当月成功请求次数；用户可给每个 API 设置本机月度上限并查看可控剩余量。服务商账户真实余额仍以其官方控制台为准。环境变量和 `.env` 仍可用于集中部署，但对普通使用者不是必需步骤。

## 分享源码给另一位使用者

不要把自己的 `.env`、`.venv`、`data/private`、`data/inbox` 或 `data/output` 发给别人。运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\package-release.ps1
```

程序会在 `dist` 生成不含个人资料、密钥、岗位库和浏览器登录状态的源码压缩包。对方解压后双击“安装并启动求职Agent.cmd”，再上传自己的简历并填写自己的 API 即可。普通使用者应优先选择上面的免 Python 桌面发行包。

## Phase 1 命令

```powershell
# 创建空白真实经历库（不会覆盖已有文件）
.\.venv\Scripts\python.exe -m job_agent profile init

# 校验 Profile 及所有事实引用
.\.venv\Scripts\python.exe -m job_agent profile validate

# 读取简历并保存纯文本副本（原文件不修改）
.\.venv\Scripts\python.exe -m job_agent resume import "C:\path\resume.docx"

# 本地体检简历结构、证据与表达（不调用外部服务）
.\.venv\Scripts\python.exe -m job_agent resume audit "C:\path\resume.docx"

# 本地可解释基线评分
.\.venv\Scripts\python.exe -m job_agent match "C:\path\jd.txt" --engine local

# 配置 .env 后启用 AI Provider 结构化评分
.\.venv\Scripts\python.exe -m job_agent match "C:\path\jd.txt" --engine ai
```

简历体检报告默认写入 `data/output/resume-audits`。其中不会保存具体邮箱或手机号；分数只是本地编辑提示，不是 ATS 通过率，也不会要求为了得分编造数字。

## Phase 2：岗位库与去重

岗位库默认保存在 `data/private/job_agent.sqlite3`，不会提交到 Git。初始化：

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs init
```

把 JD 和已有匹配报告一起导入，程序会先按来源链接去重，再按公司、岗位和城市合并跨平台重复项：

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs import "C:\path\jd.txt" `
  --match-result "C:\path\match.json"
```

查看岗位和统计：

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs list
.\.venv\Scripts\python.exe -m job_agent jobs review 1 2
.\.venv\Scripts\python.exe -m job_agent jobs stats
.\.venv\Scripts\python.exe -m job_agent jobs verify
```

`jobs review` 是只读复核：它按公开 ATS、常见招聘平台、直达网站或手工来源归类，并结合发布时间、截止时间和本地最后记录时间给出下一步。这个判断只用于安排核验优先级，不代表岗位一定真实或仍在招聘。

当你更新可到岗时间、技能或确认新的真实经历后，可刷新已入库岗位的本地匹配分：

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs rescore 1 2
```

这会保留历史匹配结果，并把最新结果用于岗位排序和后续投递材料。

配置独立的博查 Web Search API 后，让程序根据 Profile 中的岗位方向生成搜索词并发现候选链接：

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs discover
```

`discover` 默认最多调用 3 次搜索 API，并把结果写入“待核验候选”，不会直接进入正式岗位库：
博查搜索默认限定最近一个月，优先避免过期岗位和陈年汇总页。
如果某个方向临时失败，可只补跑指定方向，例如 `--role 产品运营 --role 数据运营`，不会重复搜索其他方向。

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs candidates --status pending
.\.venv\Scripts\python.exe -m job_agent jobs candidate-status 1 live --detail "页面显示可投递且 JD 完整"
```

只有确认仍在招聘并取得完整 JD 后，候选才进入评分和正式岗位导入流程。搜索与 AI 分析是两个独立 Provider；不配置搜索密钥时，简历读取、本地评分和岗位库仍可正常使用。
候选核验状态包括：`pending`（待核验）、`live`（确认在招）、`expired`（已失效）、`blocked`（页面受阻或可疑）、`irrelevant`（不是岗位或偏离方向）和 `needs_manual_review`（仍需人工复核）。
博查密钥应写入本地 `.env` 的 `BOCHA_API_KEY`，不要发送到聊天或写入代码。Brave 适配器保留为可选备用方案。

个人数据放在 `data/private`、`data/inbox` 和 `data/output`，这些目录默认被
Git 忽略。API Key 只能写入本地 `.env` 或 Windows 环境变量，不要发到聊天中。

## Phase 3：一键投递材料包

先查看岗位编号：

```powershell
.\.venv\Scripts\python.exe -m job_agent jobs list
```

再为指定岗位生成一个全新的版本目录：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications prepare 3
```

该命令会自动完成：

- 读取岗位完整 JD 和最近一次匹配结果；若尚未评分，则自动运行本地评分
- 只从 `documented` 或 `user_confirmed` 的事实中选择岗位相关证据
- 生成 BOSS 招呼语、求职邮件、30 秒自我介绍、Why company、Why role、个人优势和 60 秒面试自我介绍
- 输出硬门槛、能力缺口、薪资/地点/最终提交等人工确认项
- 自动查找与公司和岗位名称一致的定向简历，并复核其中引用的事实 ID
- 若没有现成版本，则从同一份带照片的大厂 STAR 模板生成新的 DOCX/PDF 草稿
- 新草稿先完成事实、哈希、照片、A4、九条 STAR 要点和单页 PDF 机器检查；本人看过 PDF 后才可批准
- 保存 `投递材料.md`、`application-pack.json`、`JD.txt` 和 `match-result.json`

默认输出到 `data/output/application-packs`。每次生成都会建立带时间的新版本，拒绝覆盖旧材料。
这一流程不需要 OpenAI API；搜索 API 只在发现新岗位时消耗额度。

如果希望已有岗位也重新生成一个新版本：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications generate-resume 2
```

程序会显示 DOCX、PDF 和质检清单路径。打开 PDF 检查照片、换行和版面后，再执行程序给出的 `approve-resume` 命令。批准会新建一份记录，不覆盖原质检清单；文件哈希只要发生变化，就会拒绝批准。

## Phase 4：人工投递准备

首次使用前，可从本人已经确认真实性的简历原文导入邮箱和手机号。程序不会在终端显示具体值，并会先备份 Profile：

```powershell
.\.venv\Scripts\python.exe -m job_agent profile import-contact
```

先只生成投递准备计划，不打开网页：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications assist 2 --plan-only
```

用本地模拟招聘表单验证浏览器运行组件、简历上传和提交硬暂停：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications browser-demo 2
```

打开真实岗位页面；命令会同时显示岗位专属 PDF 路径，但不填写或上传网页字段：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications assist 2
```

本人完成最终点击后，可用只读核验命令确认结果并写入投递数据库：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications verify 2
```

核验只读取当前岗位页的可见状态，不填写、不上传、不点击。页面明确显示“已投递”后，岗位状态会更新为 `applied`；再次运行不会重复创建状态事件，后续提交命令也会拒绝重复投递。

日常查看和维护进度：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications list
.\.venv\Scripts\python.exe -m job_agent applications summary
.\.venv\Scripts\python.exe -m job_agent applications history 1
.\.venv\Scripts\python.exe -m job_agent applications status 1 hr_read --detail "站内显示 HR 已读"
```

系统会为真实状态变化追加时间线事件；普通命令拒绝把进度错误回退。只有核对真实情况后，才能显式增加 `--force` 记录回退。

批量检查实习僧反馈时，建议先试运行：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications sync --dry-run
```

确认唯一匹配结果后执行正式同步：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications sync
```

同步过程只打开已有岗位页并读取平台返回的投递状态，不点击页面、不填写表单。只有公司名和岗位名都能唯一对应本地投递记录时才允许更新；旧状态、冲突状态和模糊匹配都会被拦截。私密证据位于 `data/private/status-sync`，其中只保留匹配所需的公司、岗位、状态和时间，不保存站内消息正文。若登录失效，可运行 `applications sync --headed --wait-seconds 60`，在专用浏览器中由本人重新登录。

浏览器会使用 `data/private/browser-profiles` 下的隔离会话目录。Agent 只填写 Profile 中已确认的姓名、邮箱、手机号、学校、专业、学历、毕业时间，并只上传哈希仍与质检清单一致的定向 PDF 简历。选择框、开放题、验证码、测评、薪资、地点、调剂和到岗筛选题都留给本人；最终提交始终由本人点击，验证码和反自动化措施不会被绕过。

每次运行都会把私密投递计划、无字段值的操作审计和页面截图保存在 `data/private/browser-sessions`。如果网页跳转到白名单外域名，或简历在计划生成后发生变化，自动填写会立即停止。

## Phase 6：岗位学习与面试准备包

可随时为一个已入库岗位生成准备材料：

```powershell
.\.venv\Scripts\python.exe -m job_agent applications prep 1
```

程序会基于真实 JD、当前匹配结果和 Profile 中已确认的经历，生成：

- 岗位实际工作、可能的日常任务、常见 KPI、工作流程、工具和行业知识
- `JD 要求 vs 当前能力`，区分已掌握、部分掌握、未找到已确认证据和待确认
- 1 小时、1 天、3 天、7 天四档学习计划
- 动机题、专业题、简历追问、行为题、案例题和回答框架
- 只引用已确认事实的 STAR 素材卡

输出默认保存在 `data/output/preparation-packs`，每次建立新版本，不覆盖旧学习包。JD 没有明确写出的 KPI 和流程会标记为推断，不会伪装成公司公开信息。

当投递状态首次更新为 `hr_read`、`screening`、`assessment`、`written_test` 或任一面试阶段时，系统会自动提高准备优先级并生成新版学习包。`applications summary` 会显示当前“需要优先准备”的岗位数量。

### 面试复盘

面试后可通过本地 Agent 保存复盘。在 `data/private` 下准备 UTF-8 JSON，例如：

```json
{
  "stage": "interview_1",
  "question": "面试官实际提出的问题",
  "answer": "本人当时的真实回答",
  "better_answer": "待审阅的改进草稿",
  "evidence_fact_ids": [],
  "strengths": [],
  "gaps": [],
  "next_actions": ["下次面试前完成的练习"]
}
```

运行 `applications debrief 1 <JSON路径>` 保存、`applications debriefs 1` 回看。
完整调用前缀为 `.\.venv\Scripts\python.exe -m job_agent`。
`applications prep 1` 会将该岗位历史复盘加入新准备包；自由文本会随包输出，分享前需审阅。
复盘存入本地 SQLite，完全相同的内容不会重复保存；改进回答是草稿，
事实 ID 校验不等于全文真实性认证，也不会更新 Profile 或招聘状态。
本次提供 CLI/Agent 入口，尚未增加桌面复盘编辑页面。

## Phase 7：本地 Dashboard

日常使用时，直接双击项目根目录中的 `启动求职Agent.cmd`。程序会打开本地求职工作台，窗口保持开启期间页面可用；关闭启动窗口即可停止。

也可以从项目目录手动启动：

```powershell
.\.venv\Scripts\python.exe -m job_agent dashboard
```

Dashboard 展示：

- 今日发现、候选总数、正式岗位、推荐与强烈推荐岗位
- 待确认投递、已投递、有效回复率、今日新增反馈和优先准备数量（单纯 HR 已读不计有效回复）
- 通勤超限数量；每个岗位的路线、单程时间、距离、适配状态与估算说明
- 从岗位发现到 Offer 的累计求职漏斗
- 先排除通勤超限、再结合通勤适配与匹配分排序的待投岗位
- 候选核验质量、准备优先级与最近状态时间线
- 每项指标的统计定义、数据更新时间和当前平台同步限制

页面仅绑定本机地址 `127.0.0.1`。可以在“求职方向与通勤边界”中填写目标岗位、家庭住址/小区/常用地铁站、可接受的单程上限和交通方式；输入岗位办公地址后，可通过高德地图 Web 服务计算公交地铁、驾车、骑行或步行方案。只有本人主动点击计算时才会发送本次地址。超过上限的岗位仍保留在岗位库，但不会进入“今天值得投什么”；地点不详或尚未估算的岗位不会被误删或自动排除。地图不可用时可手动记录分钟。通勤只影响投递筛选，不改写技能匹配分。

页面还允许本人手动补录真实投递状态、更新 Profile 和上传简历；所有写入操作都需要页面内确认并保存到 `data/private`。它不会代替本人点击招聘网站的最终提交，也不会处理验证码、薪资或主观筛选题。页面每 60 秒自动刷新，也可以点击右上角的“刷新”。

## Phase 12：Agent 对话与岗位推进链

右上角“Agent 对话”是工作台内的操作型对话栏。它只读取脱敏后的本地岗位、匹配和进度上下文；联系方式、API Key 与私有文件路径不会进入对话。没有云端 AI 时仍可用本地规则回答，连接异常时会自动降级，不阻塞工作台。

每个待投岗位都直接展示“简历草稿”和“人工投递准备”：

- “生成简历草稿”只使用 `documented` 或 `user_confirmed` 事实，生成单页 PDF 与 DOCX。
- 草稿必须由本人打开 PDF 检查事实、联系方式、版面和分页；批准前不能用于投递准备。
- 批准后才生成岗位投递材料包并解锁“打开岗位页与简历”；旧的已批准简历也可重建缺失的材料包。
- 程序只打开岗位页面，并在资源管理器中选中一份名称清楚的岗位专用 PDF。登录、作品集、表单、上传与最终提交均由本人完成。

对话中的“生成草稿”“确认草稿可投”“打开岗位页与简历”等建议仍会落回同一组人工确认接口；对话本身不是绕过审批的执行通道。
