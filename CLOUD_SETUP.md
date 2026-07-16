# 云端自动运行设置

本项目由 GitHub Actions 生成和发布每日预测、平局预警、结算、学习结果与日报图片，由 Google Apps Script 调度缺失阶段、校验当天报告并发送邮件。部署完成后，Apps Script 是唯一的邮件发送方；`runAutomation` 每 10 分钟运行，在北京时间 14:00-18:00 校验并尝试发送。电脑可以关机，也不需要 Google 日历或任何日历集成。

系统仅做概率分析和模拟记账，不保证盈利或任何比赛结果。竞彩和外部市场来源可能暂时不可用，缺失数据不能当作有效市场证据。

## 云端职责

| 组件 | 北京时间 | 作用 |
| --- | --- | --- |
| `.github/workflows/daily-forecast.yml` | 12:15 或 Apps Script 补调度 | 导入 Sporttery 数据，生成预测、主方案、第一版平局预警、网页与日报图片。 |
| `.github/workflows/draw-alert-refresh.yml` | 13:30 或 Apps Script 补调度 | 刷新决策快照、市场数据和平局预警，并重建报告。 |
| `.github/workflows/noon-settlement.yml` | 13:45、14:05 或 Apps Script 补调度 | 结算前一天的 90 分钟结果、更新指标、训练模型并重建报告。 |
| `apps-script/Code.gs` 的 `runAutomation` | 每 10 分钟 | 按 `Asia/Shanghai` 判断日期，触发缺失工作流，读取 `web/report-status.json`，下载并校验 PNG，然后发送邮件。 |
| `.github/workflows/odds-snapshot.yml` | 每 30 分钟 | 保存官方赔率快照。 |
| `.github/workflows/email-report.yml` | 保持 disabled | 旧 GitHub Gmail 发送工作流仅保留为历史文件，不是生产发送方。 |

正常发送不再固定在 14:00。Apps Script 从 14:00 起轮询，报告在 18:00 前任何一次检查中完整且哈希匹配即可发送；到 18:00 仍不完整时，只发送一封不附带附件的失败通知，避免把旧图片当成今天的报告。

## 启用 GitHub 功能

1. 确认远程仓库为 `l18381527760-sketch/sporttery-prediction`，生产分支为 `main`。
2. 打开 `Settings -> Actions -> General`，允许 Actions 运行，并把 `Workflow permissions` 设为 **Read and write permissions**。工作流需要写回报告、结算与赛前快照。
3. 打开 `Settings -> Pages`，把 `Source` 设为 **GitHub Actions**。工作流发布 `web/` 后，Apps Script 才能读取状态、图片和报告首页。
4. 不要再配置 `GMAIL_APP_PASSWORD`。Gmail 由 Apps Script 的当前 Google 账号授权发送，GitHub 不发送邮件，也不需要 Gmail 密钥。
5. 打开 Actions 左侧的 `Email Daily Betting Report`，通过右上角菜单选择 **Disable workflow**。随后再次打开该页面，确认页面显示已禁用并提供 **Enable workflow**，且最近运行记录中没有部署后的定时邮件运行。这就是验证 `.github/workflows/email-report.yml` 在 GitHub Actions 中保持 disabled 的方法。

Apps Script 需要一个 fine-grained token 来调用工作流。它只能授权 `l18381527760-sketch/sporttery-prediction`，权限只能是 Metadata: Read-only 和 Actions: Read and write。完整的创建步骤、7 项 Script Properties、`TEST_MODE` 验收和恢复方法见 [apps-script/README.md](apps-script/README.md)。不要把真实令牌、密钥、私人邮箱或截图写进源码、文档、提交信息和日志。

## 可靠日报路径

1. `runAutomation` 按北京时间读取 GitHub Pages 上的 `web/report-status.json`。
2. 若当天 forecast、decision 或 settlement 阶段缺失，Apps Script 通过相应工作流的 `workflow_dispatch` 和 `target_date` 补调度；GitHub 原有 cron 仍是生成任务的后备触发。
3. 从 14:00 起，Apps Script 只接受 `report_date` 为当天、所有就绪字段有效、时间顺序有效且构建信息完整的状态。
4. Apps Script 使用状态中的 `build_id` 下载 `web/daily-report.png`，计算实际 SHA-256，并与 `image_sha256` 比较。
5. 只有状态和哈希都通过才由 Apps Script 发送正常附件；不匹配时继续等待或重跑生成阶段，不会发送旧附件。
6. 18:00 做最后一次检查。报告完整时仍可发送正常日报；否则只发送当天唯一一封无附件失败通知。

预测、刷新、结算和赔率快照仍共享 `sporttery-repository` 并发队列，避免多个写入任务互相覆盖。可选市场来源失败时，采集器记录错误并保留仍通过验证的来源；独立可选步骤失败不会补造数据。状态文件和图片哈希在发送前提供最终的一致性检查。

## 手动运行和验收

1. 打开仓库的 **Actions** 页面。
2. 选择 `Daily Sporttery Forecast`、`Draw Alert Refresh` 或 `Afternoon Sporttery Settlement`。
3. 点击 **Run workflow**，分支选择 `main`，`target_date` 输入当天北京时间日期（`YYYY-MM-DD`）。不要选择已禁用的 `Email Daily Betting Report`。
4. 等待任务完成并由 Pages 发布后，检查 `web/report-status.json` 的 `report_date`、阶段状态、`build_id` 与 `image_sha256`。
5. 下载同一构建的 `web/daily-report.png`，使用 SHA-256 工具计算哈希并与状态文件比较。
6. 首次部署保持 `TEST_MODE=true`，手动运行 Apps Script 的 `runAutomation` 并检查执行日志；确认工作流日期、状态和哈希均正确后才设置 `TEST_MODE=false`。

Apps Script 的完整首次部署顺序还包括粘贴已提交的 `Code.gs`、更新 `appsscript.json`、批准 Google 权限、运行 `installAutomationTrigger`、确认恰好一个 10 分钟触发器，以及验证 GitHub 邮件工作流仍为 disabled。请按 [apps-script/README.md](apps-script/README.md) 操作，不要跳步。

## 恢复入口

- fine-grained token 被撤销或过期：在 `TEST_MODE=true` 下按原仓库范围和原最小权限轮换 `GITHUB_TOKEN`，验证后再关闭测试模式。
- 出现重复触发器：重新运行 `installAutomationTrigger`，然后确认只剩一个 `runAutomation` 10 分钟触发器。
- 日期、状态或图片哈希不匹配：重跑缺失阶段并等待 Pages 发布，禁止绕过校验或手工附加旧图。
- Gmail 失败：检查 Apps Script 执行日志、授权、额度和收件地址；修复后在窗口内重试，不要启用 GitHub 邮件工作流。
- 需要回滚：必须先禁用并删除 `runAutomation`，确认 10 分钟触发器停止后，才能恢复之前唯一的每日 `sendDailyReport` Apps Script 触发器。

具体排障步骤和安全回滚顺序见 [apps-script/README.md](apps-script/README.md)。

## 时间换算说明

GitHub Actions 的 cron 使用 UTC，Apps Script 业务时间固定为 `Asia/Shanghai`：

- `15 4 * * *`：12:15 基础预测。
- `30 5 * * *`：13:30 决策与平局预警刷新。
- `45 5 * * *`：13:45 结算与学习。
- `5 6 * * *`：14:05 结算重试。
- `*/30 * * * *`：每 30 分钟赔率快照。

`.github/workflows/email-report.yml` 中保留的 `0 6 * * *` 不再是生产发送计划；该工作流在 GitHub Actions UI 中必须保持 disabled。生产邮件由每 10 分钟运行的 Apps Script 在 14:00-18:00 窗口内决定。

## 模拟使用说明

日报中的平局预警可以是 0 到 4 场；没有符合门槛的预警是正确结果。冷门平局和均势平局独立观察，每个子类型至少需要 30 场已结算样本，并通过 ROI、CLV、校准、回撤和近期稳定性检查后，才可能有 10 至 30 元的独立模拟投入。每日预警新增模拟投入最多 80 元，所有每日模拟投入最多 500 元。

预警与主方案命中同一场比赛时复用金额和结算，不重复投入或计算利润。所有预警和结算采用 90 分钟比分；加时赛和点球不改变结果。学习仅使用不可变赛前快照，候选模型经过影子评估后才能晋级；训练失败时继续使用有效冠军模型或基础预测。这些机制都不构成盈利承诺，也不会自动转为真实投注。
