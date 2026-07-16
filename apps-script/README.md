# Apps Script 自动化部署与恢复

这份说明面向第一次使用 GitHub Actions 和 Google Apps Script 的维护者。预测、刷新和结算仍由 GitHub Actions 完成；Apps Script 负责按阶段触发工作流、轮询已发布状态、校验日报图片并发送邮件。部署完成后，**Apps Script 是唯一的邮件发送方**，电脑可以关机，也不需要 Google 日历。

本项目仅用于概率分析和模拟记账，不保证盈利或任何比赛结果。不要把报告当作投注指令或收益承诺。

## 运行规则

- `runAutomation` 每 10 分钟运行一次，项目时区必须是 `Asia/Shanghai`。
- `runAutomation` 按北京时间判断业务日期，并在缺少阶段产物时调用 `daily-forecast.yml`、`draw-alert-refresh.yml` 或 `noon-settlement.yml` 的 `workflow_dispatch`。
- 正常邮件发送窗口是北京时间 14:00-18:00。只有当天 `web/report-status.json` 完整、日报 PNG 可下载且实际 SHA-256 与状态文件一致时，才发送附件。
- 到 18:00 时会做最后一次校验：若报告已就绪，发送正常日报；若仍未就绪，只发送一封失败通知，不附带附件，绝不附带昨天或其他旧版本的图片。
- `LAST_SENT_DATE` 和 `LAST_FAILURE_NOTICE_DATE` 保证同一北京时间日期最多出现一封正常日报或一封失败通知。

## 部署前准备

### GitHub 仓库设置

1. 确认生产仓库是 `l18381527760-sketch/sporttery-prediction`，生产分支是 `main`。
2. 打开 `Settings -> Actions -> General`，允许 Actions 运行，并将 `Workflow permissions` 设为 **Read and write permissions**。这是工作流写回报告所需的仓库权限，不是 Apps Script 令牌权限。
3. 打开 `Settings -> Pages`，把 `Source` 设为 **GitHub Actions**。
4. 创建 fine-grained personal access token。`Repository access` 选择 **Only select repositories**，并且只选择 `l18381527760-sketch/sporttery-prediction`；`Repository permissions` 只设置：
   - Metadata: Read-only
   - Actions: Read and write
5. 不要授予其他仓库或额外权限。令牌只能保存到 Apps Script 的 Script Properties；代码、文档、截图、提交信息和日志中不得填写真实令牌或密钥。

### 必须手工配置的 7 项 Script Properties

- `GITHUB_OWNER`：填写仓库所有者 `l18381527760-sketch`。
- `GITHUB_REPO`：填写仓库名 `sporttery-prediction`。
- `GITHUB_TOKEN`：填写上一步创建的 fine-grained token，只能在自己的 Apps Script 项目中填写。
- `REPORT_STATUS_URL`：填写 GitHub Pages 上 `report-status.json` 的完整 HTTPS 地址，例如 `https://l18381527760-sketch.github.io/sporttery-prediction/report-status.json`。
- `REPORT_IMAGE_URL`：填写 GitHub Pages 上 `daily-report.png` 的完整 HTTPS 地址，例如 `https://l18381527760-sketch.github.io/sporttery-prediction/daily-report.png`。
- `REPORT_SITE_URL`：填写报告首页，例如 `https://l18381527760-sketch.github.io/sporttery-prediction/`。
- `RECIPIENT_EMAIL`：填写接收日报的邮箱地址，只在 Script Properties 中保存。

以上正好是必须手工配置的 7 项。不要在 `Code.gs` 中写入这些值，也不要在文档中填写真实令牌、密钥或私人邮箱。

### 测试开关与自动状态

`TEST_MODE` 是临时部署开关，不计入上述 7 项必填配置。首次部署时手工新增 `TEST_MODE` 并设为字符串 `true`；验收完成后改成字符串 `false`。`TEST_MODE=true` 时会记录拟发送日志而不调用 Gmail，但仍会写入当天的去重状态，因此同一天不要期待切换为 `false` 后补发测试邮件。

运行状态属性由 `Code.gs` 自动写入，包括各阶段的 `LAST_FORECAST_DISPATCH_*`、`LAST_REFRESH_DISPATCH_*`、`LAST_SETTLEMENT_DISPATCH_*`，以及 `LAST_SENT_DATE`、`LAST_SENT_IMAGE_SHA256` 和 `LAST_FAILURE_NOTICE_DATE`。这些运行状态不属于手工配置项；日常部署不要创建、修改或复制它们。

## 首次部署顺序

严格按下面顺序操作，先测试、再启用定时触发器、最后进入生产模式。

1. 打开现有 Apps Script 项目，不要新建第二个生产项目。
2. 把仓库中已提交的 `apps-script/Code.gs` 全部粘贴到在线编辑器的 `Code.gs`。随后打开 **Project Settings**，勾选显示 `appsscript.json` 清单文件，并用已提交的 `apps-script/appsscript.json` 更新在线清单；确认时区显示 `Asia/Shanghai`。
3. 在 **Project Settings -> Script Properties** 配置上述 7 项 Script Properties。再次确认源码和日志里没有令牌、密钥或私人邮箱值。
4. 按“GitHub 仓库设置”创建仅限目标仓库的 fine-grained token，并把它放进 `GITHUB_TOKEN`，不要放进 GitHub Actions secrets 或源码。
5. 新增临时属性 `TEST_MODE=true`。回到编辑器，选择并手动运行 `runAutomation`；首次运行时按 Google 提示批准权限，包括外部 HTTPS 请求、管理项目触发器和代表当前 Google 账号发送 Gmail。检查 **Executions** 日志，确认只记录动作，没有实际 Gmail 发送。
6. 手动运行 `installAutomationTrigger`。打开左侧 **Triggers** 页面，确认恰好一个每 10 分钟运行的 `runAutomation` 触发器；若还有 `runAutomation` 或旧的 `sendDailyReport` 触发器，重新运行安装函数并再次检查。
7. 在 GitHub 打开 **Actions**，选中 `Daily Sporttery Forecast`，点击 **Run workflow**，分支选 `main`，通过 `workflow_dispatch` 输入当天北京时间日期（`YYYY-MM-DD`）。按当天缺少的阶段继续手动运行 `Draw Alert Refresh` 和 `Afternoon Sporttery Settlement`。完成后打开 Pages 上的 `web/report-status.json`，确认 `report_date` 是当天日期、阶段状态为 `true`、`build_id` 非空，并下载同一构建的 PNG；在 PowerShell 运行 `Get-FileHash .\daily-report.png -Algorithm SHA256`，确认结果与状态文件中的 `image_sha256`（PNG 的 SHA-256）一致。
8. 回到 Apps Script，多运行几次 `runAutomation`，核对 dry-run 日志中的日期、调度阶段、正常发送或 18:00 失败路径都符合预期。只有日志正确后，才把属性改为 `TEST_MODE=false`。当天若测试模式已写入发送去重状态，让生产发送从下一个北京时间日期开始。
9. 确认 `.github/workflows/email-report.yml` 在 GitHub Actions 中保持 disabled：在 Actions 左侧选择 `Email Daily Betting Report`，页面应显示该工作流已禁用，并提供 **Enable workflow** 而不是 **Disable workflow**。再检查最近运行记录，确认部署后没有新的定时邮件运行。不要只查看仓库中的 YAML，因为文件仍保留用于审计和回滚参考。

## 日常核对

- 在 Apps Script 的 **Executions** 中检查 `runAutomation` 每 10 分钟执行，没有持续报错或重复 Gmail 发送。
- 在 GitHub Actions 中确认阶段工作流使用当天北京时间日期，并最终发布 `web/report-status.json` 与 `web/daily-report.png`。
- 从 14:00 起，Apps Script 会轮询完整状态并校验图片；18:00 前就绪即可发送，不要求恰好在 14:00 完成。
- GitHub 邮件工作流保持禁用。预测工作流不会调用 `email-report.yml`，GitHub 也不保存 Gmail 应用专用密码。

## 故障恢复

### 令牌被撤销

先把 `TEST_MODE` 改为 `true`。创建新的 fine-grained token，仍然只授权 `l18381527760-sketch/sporttery-prediction`，权限仍然只有 Metadata: Read-only 和 Actions: Read and write；替换 `GITHUB_TOKEN` 后手动运行 `runAutomation` 并检查日志。确认成功后设置 `TEST_MODE=false`，再在 GitHub 撤销旧令牌。不要把新旧令牌写进日志或工单。

### 重复触发器

手动运行一次 `installAutomationTrigger`。它会删除处理函数为 `runAutomation` 或 `sendDailyReport` 的现有触发器，再创建一个 10 分钟触发器。到 **Triggers** 页面确认只剩恰好一个 `runAutomation`；不要手工保留第二个同名触发器。

### 状态或哈希不匹配

不要绕过校验，也不要手工发送旧附件。先比较 `web/report-status.json` 的 `report_date`、各阶段状态、`build_id` 和 `image_sha256`，再下载 `REPORT_IMAGE_URL` 并计算实际 SHA-256。按缺失阶段重新运行对应的 `daily-forecast.yml`、`draw-alert-refresh.yml` 或 `noon-settlement.yml`，等待 Pages 发布同一构建后再运行 `runAutomation`。若到 18:00 仍不匹配，正确结果是一封不附带附件的失败通知。

### Gmail 发送失败

`GmailApp.sendEmail` 失败时，代码不会写入当天成功发送状态。到 Apps Script 的 **Executions** 查看错误，检查当前 Google 账号的 Gmail 服务、发送额度、`RECIPIENT_EMAIL` 和授权；必要时以 `TEST_MODE=true` 重新批准权限并验证日志。修复后在 18:00 前再次运行 `runAutomation`，成功发送后再检查 `LAST_SENT_DATE`。不要通过启用 GitHub 邮件工作流来补发。

### 回滚到旧的每日触发器

先禁用并删除 `runAutomation` 触发器，并在 **Triggers** 页面确认 10 分钟触发器已经消失；在这一步完成前绝不能恢复旧触发器。再恢复之前唯一的每日 `sendDailyReport` 触发器，并确认项目时区仍为 `Asia/Shanghai`。`sendDailyReport()` 是已提交在 `Code.gs` 中的兼容入口，会调用 `runAutomation()`；回滚期间仍保留状态与哈希校验。除非另有经过审查的应急方案，`.github/workflows/email-report.yml` 仍保持 disabled，避免两个发送方同时发信。

## 本地验证命令

在仓库根目录依次运行：

```powershell
python -m unittest tests.test_workflow_schedule -v
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
```

本仓库的阶段验收使用 `.superpowers/sdd/runtime/node-v24.18.0-win-x64/node.exe` 运行同一个 Node 测试文件；上面的 `node` 命令适用于已经把 Node 加入 `PATH` 的日常开发环境。
