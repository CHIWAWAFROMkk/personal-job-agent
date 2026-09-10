---
name: 个人求职 Agent
description: 以岗位证据台账组织每日判断与受控行动的本地求职工作台
colors:
  action-carmine: "#9f263d"
  verification-green: "#176d62"
  attention-amber: "#9a5b16"
  slate-ink: "#202329"
  secondary-ink: "#50545e"
  quiet-ink: "#626671"
  workspace-gray: "#e8ebf0"
  work-surface: "#ffffff"
  work-surface-soft: "#f7f8fa"
  hairline: "#dce1e6"
  glass: "rgba(255, 255, 255, .36)"
  glass-edge: "rgba(255, 255, 255, .86)"
typography:
  display:
    fontFamily: "Job Agent Sans, Microsoft YaHei UI, Segoe UI Variable, sans-serif"
    fontSize: "32px"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-0.025em"
  headline:
    fontFamily: "Job Agent Sans, Microsoft YaHei UI, Segoe UI Variable, sans-serif"
    fontSize: "20px"
    fontWeight: 650
    letterSpacing: "-0.03em"
  title:
    fontFamily: "Job Agent Sans, Microsoft YaHei UI, Segoe UI Variable, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    letterSpacing: "-0.015em"
  body:
    fontFamily: "Job Agent Sans, Microsoft YaHei UI, Segoe UI Variable, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.85
  label:
    fontFamily: "Job Agent Sans, Microsoft YaHei UI, Segoe UI Variable, sans-serif"
    fontSize: "12px"
    fontWeight: 500
rounded:
  control: "22px"
  navigation-item: "10px"
  job-choice: "12px"
  search: "14px"
  navigation-surface: "16px"
  work-window: "22px"
  composer: "20px"
  detail-surface: "0px"
  surface: "10px"
  dialog: "20px"
spacing:
  micro: "6px"
  control: "14px"
  section: "22px"
  page-gutter: "12px"
components:
  button-primary:
    backgroundColor: "{colors.slate-ink}"
    textColor: "{colors.work-surface}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "40px"
  button-secondary:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.slate-ink}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "40px"
  input-default:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.slate-ink}"
    rounded: "7px"
    padding: "10px 12px"
    height: "42px"
  work-panel:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.slate-ink}"
    rounded: "{rounded.surface}"
    padding: "14px 16px"
  state-chip:
    backgroundColor: "{colors.work-surface-soft}"
    textColor: "{colors.secondary-ink}"
    rounded: "9px"
    padding: "4px 8px"
  navigation:
    backgroundColor: "{colors.glass}"
    textColor: "{colors.slate-ink}"
    rounded: "16px"
    height: "54px"
  search:
    backgroundColor: "rgba(255,255,255,.48)"
    textColor: "{colors.quiet-ink}"
    rounded: "{rounded.search}"
    height: "42px"
    padding: "0 12px"
  position-toolbar:
    backgroundColor: "rgba(247,250,253,.72)"
    textColor: "{colors.secondary-ink}"
    rounded: "28px"
    padding: "8px 22px"
---

# Design System: 个人求职 Agent

## Overview

**Creative North Star: "岗位证据台账 / The Evidence Ledger"**

工作台像一张每天都会更新的岗位台账：先给出需要判断的岗位，再把发现、推荐、投递与反馈放进同一条受控流程。视觉应当安静、精确、可信，服务于长期使用，而不是像营销首页或杂志封面一样抢走任务注意力。

界面以桃白与雾蓝的柔和环境光承接白色工作面，用深石板文字保持长时间阅读的稳定性。胭脂红只指示当前决定或高价值动作，验证绿和琥珀只描述真实状态。所有自动化都必须在页面上露出证据、岗位绑定关系和人工确认边界。

2026-09-09 按最终源码合并用户确认的 iOS 26 Liquid Glass 方向：相邻职位、详情和 Agent 分栏组成一个工作窗口，玻璃用于顶栏、职位导航、上下文工具条和输入容器；JD 与对话正文保持不透明。表单使用开放分节，弹窗保留稳定标题栏和独立滚动正文。实现使用 CSS 透明度、背景模糊、亮边与柔和阴影，不声称复刻 Apple 原生光学材质。

**Key Characteristics:**

- 高密度但不拥挤的桌面工作区，首屏可见职位、岗位专属简历与安全代填
- 无图片的桃白与雾蓝环境光、相邻工作分栏与轻透控制层
- 自托管中文无衬线字体与表格数字，标题克制而明确
- 状态色稀缺，颜色永远对应判断、验证或等待
- Agent 对话是岗位绑定的常驻侧栏，不是社交聊天界面

## Colors

主色盘由桃白与雾蓝环境光、白色阅读面和深石板文字构成；胭脂红、验证绿与琥珀只承担操作与状态语义。

### Primary

- **行动胭脂红：** 标记选中岗位、焦点、悬停与需要人工确认的节点；主按钮及 Agent 发送按钮常态为深石板色。

### Secondary

- **验证绿：** 已连接、已核验、正向状态与低面积数据条。
- **注意琥珀：** 等待配置、接近限制和需要复核的草稿状态。

### Neutral

- **工作区灰：** HTML 环境底色；正文外的页面使用三层柔和径向渐变承接透明控制层，精确渐变见 sidecar。
- **白色工作面：** 台账、资料条、岗位列表、表单与对话内容的主要承载面。
- **深石板文字：** 标题、正文重点、主按钮和数字。
- **安静文字与发丝线：** 次级事实、时间、来源和结构分隔。

### Named Rules

**The Sparse Accent Rule.** 胭脂红只用于需要决定或执行的少数位置；当一屏出现多处大面积红色时，层级已经失效。

**The State Is Evidence Rule.** 绿色、琥珀与红色必须对应数据库或用户确认得到的真实状态，不能用作装饰。

## Typography

**Display Font:** Job Agent Sans（自托管 Noto Sans SC variable；回退 Microsoft YaHei UI、Segoe UI Variable 与通用无衬线）。

**Body Font:** Job Agent Sans（与展示字体同家族，以字重、字号和间距建立层级）。

**Label/Measurement Font:** 标签沿用 Job Agent Sans；数字使用 `font-variant-numeric: tabular-nums` 保持台账对齐。

**Character:** 单一中文无衬线家族让岗位、状态与表单在桌面环境中保持一致。展示标题只通过紧凑字距和高字重建立判断感，不使用装饰字体或技术感等宽字。

### Hierarchy

- **Display**（650，32px，1.35）：当前岗位标题；手机为 24px。不再设置首页营销大标题。
- **Headline**（650，20px）：职位导航标题；Agent 标题为 18px/600，主要对话框标题为 22px，手机为 20px。
- **Title**（600，18px）：任务区标题；职位列表为 15px、550，选中时为 650，手机为 15px。
- **Body**（400，14px，1.85）：Agent 对话正文；JD 为 14px、1.85，最长 72ch。
- **Label**（12px）：字段、状态、来源和阶段；不得承担长段正文。输入和对话正文为 13–14px，手机输入为 16px。

### Named Rules

**The One Sans Voice Rule.** 所有工作区文字使用同一自托管无衬线家族；通过字重和密度区分判断、内容与注释。

**The Position Leads Rule.** 详情由职位标题领起，公司和编号放在标题下作为来源事实，不作为标题上方的装饰眉题。

## Layout

72px 顶部区域容纳 54px 轻透导航。工作窗口采用 272px 职位导航、弹性岗位详情、328px Agent 相邻分栏；1600px 起两侧为 292px 和 360px。栏间距为零，窗口外边距 12px，内部以发丝线分隔。详情正文内边距 32px 36px，底部预留 100px；宽屏为 40px 48px、底部 110px。资料、偏好、统计与辅助表单位于次级视图。

1190px 以下默认两栏并按需打开 Agent；900px 以下 Agent 替换详情，640px 以下职位、详情、Agent 单屏切换。打开岗位必须退出 Agent 视图并将键盘焦点移到可见标题。根网格行使用 `minmax(0,1fr)`，导航与详情使用 `min-height:0` 和内部滚动，避免底部操作栏被内容撑出视口。

手机顶部区域为 68px、外边距 8px，正文内边距 16px 20px、底部预留 120px；360px 以下收紧顶部间距。高度不超过 550px 时缩短 Agent 输入区并隐藏建议及脚注。共享表单仍有 1080px、900px、720px 断点，720px 以下弹窗全屏。

### Named Rules

**The First Viewport Action Rule.** 桌面首屏先展示职位选择，再展示当前岗位的简历草稿、安全代填与 Agent；辅助统计不抢占主任务。

**The Bound Action Chain Rule.** 简历生成、本人审阅、材料准备与安全代填始终绑定具体岗位并按顺序解锁。

## Elevation & Depth

阅读面依靠不透明白色保持稳定；统一窗口以环境阴影和亮边衬托相邻分栏。职位导航使用 26px 模糊与 125% 饱和度，顶栏使用 24px 模糊与内亮边。底部工具条覆盖滚动正文，使用 12px 模糊、150% 饱和度；Agent 输入容器使用 20px 模糊、125% 饱和度。JD 与 Agent 消息区不参与背景模糊。

### Shadow Vocabulary

- **覆盖层环境阴影**（`0 28px 80px rgba(24, 33, 43, .24)`）：只用于需要中断或保护焦点的对话框。
- **工作窗口阴影**（`0 18px 48px rgba(39,48,66,.1), inset 0 1px 0 #fff`）：统一窗口的外边界，分栏不重复抬升。
- **选中项阴影**（`0 4px 12px rgba(34,45,57,.065), inset 0 -1px 0 rgba(67,75,92,.08)`）：选中职位的轻微状态反馈。
- **工具条阴影**（`0 8px 24px rgba(34,45,57,.1), inset 0 1px 0 #fff, inset 0 -1px 0 rgba(154,174,192,.2)`）：浮在正文之上的底部操作层。

### Named Rules

**The Content Stays Solid Rule.** 玻璃用于导航和控制层；JD 与 Agent 消息保持稳定阅读底面，不把每段正文包成玻璃卡片。

降低透明度偏好或不支持 backdrop-filter 时，控制层降级为不透明浅底；强制色彩模式使用系统 Canvas/CanvasText 并移除模糊和阴影。控件反馈为 160ms，降低动态效果时取消或缩短过渡。

## Shapes

工作窗口使用 22px 圆角，顶栏使用 16px 圆角，相邻分栏为直角；手机窗口为 14px。动作按钮为 22px，搜索为 14px，职位行为 12px，Agent 输入容器为 20px，工具条为 28px。标准字段保持 7px，对话框为 20px；全屏弹窗去掉圆角。表单分节无边框包围和圆角，只用底部分隔线。不使用气泡尾巴、硬投影或装饰性切角。

## Components

### Buttons

- **Shape:** 圆角矩形，常规最低高度 40px、13px/500 文字，水平内边距 18px；行内动作可降为 32–34px，手机主要控制至少 44px。
- **Primary:** 深石板底、白字；当前流程唯一下一步最强。
- **Secondary / Ghost:** 白底、深石板字与清晰中性边框。禁用动作使用可读的灰底灰字，不依赖降低整个按钮透明度。
- **Hover / Focus:** 悬停通过色阶反馈，不上移控件；键盘焦点使用 2px 胭脂红外轮廓。

### Chips

- **Style:** 工作台状态标签 9px 圆角、12px/550 文字、4px 8px 内边距与低饱和浅底。
- **State:** 验证绿表示可用或推荐，琥珀表示等待审阅，胭脂红表示错误或阻断。

### Cards / Containers

- **Corner Style:** 主工作区按 Shapes 的嵌套层级；共享资料面板仍为 10px。
- **Background:** 白色工作面置于柔和环境光背景。
- **Shadow Strategy:** 主工作区按 Elevation & Depth；共享资料面板保留平面样式。
- **Border:** 1px 冷灰边界。

### Inputs / Fields

- **Style:** 白底、1px 中性边框、7px 圆角，最低高度 42px，内边距 10px 12px，正文 14px。
- **Focus:** 边框转胭脂红，并出现低透明度三像素焦点层。
- **Error / Disabled:** 错误使用胭脂红；禁用降低不透明度但保留原位置。

搜索使用半透明白底与细亮边容器与 2px 可见焦点环；Agent 输入使用 20px 圆角轻透容器，聚焦出现胭脂红边界和 2px 低透明度焦点层。手机输入字号为 16px。字段内复选框保持 18px 正方形，不继承文本字段的最小高度。

### Navigation

应用标识采用圆拱形 A，深石板渐变底与白色圆端线条；替换旧 JA 字标与蓝色公文包桌面图标。顶部品牌使用 28px 图块，Agent 入口使用 16px 同形单色 SVG，保留文字标签。Windows ICO 提供 16–256px 七档尺寸，由 scripts/build_desktop_icon.py 生成。

顶栏以轻透底和内亮边提供控制层，不增加独立环境阴影。“职位”是默认入口；选中导航使用透明底与底部两像素墨色线。职位列表以标题领起，公司与评分位于其下；选中标题使用墨色加重，并同时具有边界、背景和 `aria-current`，不只依赖颜色。品牌标记为 28px、9px 圆角。

职位导航顶部使用并列的“实习机会 / 校招机会”双通道控制，百分比直接显示当前投入比例；“全部”退为低强调文字动作。通道只改变查看与处理顺序，不改写岗位的能力匹配证据。

### Position Tasks

当前岗位标题下并列“简历草稿”和“安全代填”，桌面采用开放布局，无包围分隔线，列间距 32px、内边距 4px 0 28px；小屏纵向排列并保留分节线。其下使用 JD、匹配与进度标签切换证据。生成方式等次要说明使用原生 details/summary 折叠；任务、阻断原因和批准边界保持可见。材料生成、PDF 审阅、批准和代填保留既有确认流程；没有准备材料时不得打开无效链接。

“项目工坊”与 JD、匹配、进度并列为详情标签。项目流程固定为看懂、修改、跑通、讲明白；用连续步骤和准入检查表达状态，不做卡片墙。生成和跑通仍为中间态，只有本人完成关键修改、演示确认和文字解释后才显示验证绿的“简历候选”。

底部工具条固定在详情容器内，按内容宽度水平居中，最大宽度为容器减 28px；距底部 18px（手机 10px），内边距为 8px 22px（手机 6px 16px），覆盖同一容器里的滚动内容；保留正文底部空间，确保末尾内容能够完整滚出工具条遮挡区。

### Dialogs

标题栏保持在滚动正文上方；正文内边距为 4px 28px 28px，分节上下 24px，字段网格间距 16px 20px。连接设置同样使用开放分节。手机标题栏内边距 20px，分节上下 20px。

### Agent Sidebar

桌面为右侧 328px 常驻栏，宽屏为 360px，小屏通过入口切换。消息和未发送输入跟随岗位隔离；旧的无岗位消息从“历史”访问。消息使用连续行，不使用聊天气泡。写入和网页动作保留确认，最终提交由本人完成。

## Do's and Don'ts

### 2026-09-08 refinement references

- [Linear 2026 UI refresh](https://linear.app/now/behind-the-latest-design-refresh)：借鉴导航退后、内容优先的层级，移除职位行的重复分隔线，以留白和选中背景组织列表。保留本产品的胭脂红，不复制品牌或深色主题。
- [Microsoft Fluent layout](https://fluent2.microsoft.design/layout)：借鉴相邻信息的间距关系与自适应布局。保留已经验证的三栏和断点，不照搬通用栅格。
- 职位选中状态由边框、白色底面和加重标题共同指示，不只依赖颜色；保留 `aria-current`。
- 搜索容器必须有可见键盘焦点；文本选区、插入光标和滚动条跟随当前色盘。动效限于状态反馈，并尊重减少动态效果。

### Do:

- **Do** 让选职位、简历草稿与安全代填在打开页面后数秒内可见。
- **Do** 让岗位来源、匹配、简历状态、对话与操作始终绑定同一职位。
- **Do** 在桌面、手机与横屏尺寸上保留键盘焦点、错误、加载、空状态和降低动态效果。
- **Do** 把 Agent 的自然对话转成可确认的具体动作，并在写盘后回到人工审阅状态。

### Don't:

- **Don't** 使用巨型封面、营销口号或同尺寸玻璃卡片墙掩盖当天任务。
- **Don't** 为了视觉完整性虚构岗位、数字、经历、反馈或连接状态。
- **Don't** 把简历或代填做成脱离 JD 的全局动作，也不要绕过本人 PDF 审阅。
- **Don't** 给 Agent 消息添加社交聊天气泡，或把最终提交边界藏进帮助文字。
