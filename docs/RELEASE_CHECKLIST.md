# 本地桌面发行验收

发行由以下独立检查组成。某一项成功不能替代其他项；只写“测试通过”不够，需要保存版本、实际包哈希、数据目录及结果文件。

## 源码与主链

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
node --experimental-vm-modules --test tests/test_frontend.mjs tests/test_extension_fill.mjs tests/test_mock_frontend.mjs tests/test_tracking_frontend.mjs
```

- 全量 Python 与前端测试通过，保留命令和退出码。
- 新用户在无 API、空资料目录中完成导入简历、核对 JD、准备、审阅与显式投递补录。模拟故障重试不能丢失输入。
- 批准材料、打开招聘页面都不能自动记为已投递；修改简历使旧确认失效。
- 真实 PDF 响应字节与当前版本摘要一致，渲染后检查所有页面。事实不重写成虚构学校、日期、机构或成果。
- 用户切换中断及恢复再次中断后，Profile、岗位库和材料保持同一用户；无法确认时阻断数据读写。
- 完整备份实际恢复到另一个测试目录后，查询岗位并核对 Profile 和输出字节。

## 构建与实际程序

```powershell
.\scripts\build-desktop.ps1 -SkipDependencyInstall
.\.venv\Scripts\python.exe scripts\verify_packaged_desktop.py --exe '<本次包内的EXE>' --output 'work\release-qa\<唯一目录>'
.\scripts\package-release.ps1
```

可以通过 `-PythonPath` 指定已有构建环境。默认不自动安装依赖；需要安装时由维护者明确使用 `-InstallDependencies`。

必须测试本次 ZIP 解压出的程序，并保存首次运行和第二个进程重启的原生 WebView2 报告。必需步骤缺失或跳过均不能算通过。源码模式的结果不能替代打包程序结果；原生 DOM 检查也不能替代 PDF 目视检查。

源目录、桌面产物和 ZIP 均需通过隐私扫描。新源码包采用项目版本命名、包含测试和文档，并排除个人数据、环境、缓存和浏览器会话。保留每个发行 ZIP 的 SHA256。

## 并排部署与回退演练

普通使用者按 `USER_GUIDE.md` 完整解压、备份资料并并排升级即可。维护者如使用部署工具，必须显式提供已验收 ZIP 和 EXE 的哈希，不再自动选择历史构建：

```powershell
.\scripts\deploy-desktop.ps1 -ArchivePath '<本次ZIP>' -ExpectedZipHash '<64位SHA256>' -ExpectedExeHash '<64位SHA256>' -RehearsalOnly
```

该工具默认只预览；`-Execute` 才会正式部署。不得把自动发现的最新文件当作已验收版本。执行前确保资料没有活动写入者，保留完整资料备份和旧程序，不强制关闭用户进程，不覆盖现有部署目录。

部署工具为维护者流程，依赖 Python；桌面使用者运行已打包程序不需要 Python。更换快捷方式后需核对实际目标，旧窗口不会自动升级。

## 必须如实保留的边界

没有真实用户试用证据时，不宣称陌生人已完成验收；没有真实招聘反馈时，不宣称提高通过率。合成测试、离线演练及截图不能代替真实投递。物理断电、任意外部程序同时写资料、未带恢复记录的旧版本事故需要单独评估。

实站 AI 代填保持暂停。软件不代填验证码、不接受条款、不提交申请；保留用户的最终判断。
