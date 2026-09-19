# Epic 5 · 真实经历面试对练

## 使用

在职位详情的「面试准备」区域点击「开始面试对练」，再选择「开始新练习」。默认本地规则，不需要配置或调用 AI。每场五道问题；输入文字回答后获得即时结构提示，可以在回答至少一道问题后提前结束复盘。

「本岗位练习记录」可读取最近 50 场记录。新练习不会覆盖旧练习；未发送的输入不保存。计时器统计本次打开的练习时间，可以暂停，不是录音或全历史计时。

选择「已配置 AI · 辅助选题」必须明确同意发送最小化 JD、已确认经历和本次回答。使用设置中的现有供应商；没有有效配置或响应无效时退回本地规则。改变供应商、地址或模型后，已有场次不会向新配置发请求；请新建并重新授权。

「查看提词卡」显示真实素材、已有数字、STAR 提纲和反问，可以移动至窗口左右角落或关闭。只用于练习或规则允许的参考，不用于隐蔽代答。

## 数据与真实性

- 会话与幂等请求记录写入现有岗位 SQLite 数据库的 `mock_interview_sessions`、`mock_interview_requests` 表。数据库是本机明文，不是加密保险箱；共享电脑应保护系统账户与数据目录。
- 会话保留开始时的已确认经历事实快照；之后修改档案不会悄悄改写旧会话。需要最新材料时新建练习。
- 待确认经历不进入问题或参考卡。用户回答仅作为未核验练习文本保存，不写回 Profile、不自动成为成果。
- 参考数字必须出现在已确认事实中。回答包含未支持数字时提示核验；规则无法判断所有夸大表述或实际真实性。
- STAR 四项各 0–25 分，用本地关键词结构规则计算。这不是招聘官评分、真伪判断、能力等级或录用概率。
- 复盘逐题提供不同的改进框架，仅引用本题关联的已确认事实；缺失内容明确标为待补充。结构缺项仅表示规则未识别，不代表回答实际没有该元素。

## 云端边界

AI 只能从允许的问题或教学类别 ID 中选择，不能把自由生成的经历、数字或话术直接渲染为事实。返回无效 ID、非 JSON 或请求失败均回退本地，并显示提示。不自动联网、不创建模型名称。

云端传输前隐藏标准邮箱、手机号、链接、Windows 路径、常见密钥，以及当前档案中的姓名和联系方式。自动规则不能识别任意隐私文字；请勿在 JD、经历或回答中放入秘密、账号或不必要的个人信息。

## API

所有接口需要原有 `X-Job-Agent-Token` action token 和可信来源校验。

| 方法与路径 | 输入／结果 |
| --- | --- |
| `POST /api/interview/session/start` | `job_id`, `engine: local/cloud`, `request_id`；云端另需 `cloud_consent: true`。返回会话。 |
| `POST /api/interview/session/reply` | `session_id`, `answer`（1–6000 字符）, `expected_revision`, `request_id`。返回更新会话。 |
| `POST /api/interview/session/score` | `session_id`, `expected_revision`。至少一条回答；完成后再次调用返回相同复盘。 |
| `POST /api/interview/teleprompter` | `job_id`。返回 `intro`, `facts`, `metrics`, `star`, `questions`, `notice`。 |
| `GET /api/interview/session/{uuid}` | 返回完整会话。 |
| `GET /api/interview/jobs/{job_id}/sessions` | 返回最近 50 场摘要。 |

会话状态：`active → ready_to_score → completed`；也可由 `active` 提前复盘至 `completed`。五条回答后停止继续回复。`start/reply` 使用请求 ID 幂等；重复相同请求不重复写入，不同内容复用 ID 或过期版本返回 409。云端调用不持有 SQLite 写锁，最终短事务重新检查版本再提交。

会话返回：`session_id`, `job_id`, `company`, `title`, `engine`, `status`, `revision`, `questions`, `messages`, `score`, `created_at`, `turn_count`, `evidence`, `evidence_note`, `notice`。复盘返回 `total`, `dimensions`, `warnings`, `rewrites`, `disclaimer`。

## 当前限制与验收

- 没有语音识别、录音、实时听取面试或自动口述回答；可自行将口述转成文字。
- 提词卡是应用内浮层，不是跨软件置顶的原生系统窗口。
- 没有自动读取最终 PDF 草稿；本版以选定岗位 JD 和已确认经历库为依据。
- 云端安全选题与本地规则是有意限制，不等同于自由式真人面试官。
- 自动化测试覆盖本地不联网、事实筛选、云端授权和降级、并发版本冲突、幂等、回滚、五轮状态流转、评分与认证。测试通过不代表真实面试效果、任意问题理解或 WCAG AAA 全面认证。
- 源码更新不会自动替换已安装桌面 EXE；发布包需另行构建和验收。

2026-09-20 验证：全量 Python 372 项通过；逐题复盘增强后相关 11 项复测通过。前端相关 11 项 Node 测试通过；1440/Aurora、1266/Cosmic、390/Cosmic 三个视口通过五轮对练、失败保留输入、历史恢复、复盘、事实隔离及提词卡键盘焦点验收。未向真实 AI 服务发送档案，云端分支使用模拟响应验证。
