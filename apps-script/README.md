# Apps Script 自动化部署与恢复

这份说明面向第一次使用 GitHub Actions 和 Google Apps Script 的维护者。预测、刷新、结算和赛前复核仍由 GitHub Actions 完成；Apps Script 负责按优先级触发工作流、轮询已发布状态、校验报告图片并发送邮件。部署完成后，**Apps Script 是唯一的邮件发送方**，电脑可以关机，也不需要 Google 日历。

本项目仅用于概率分析和模拟记账，不保证盈利或任何比赛结果。不要把报告当作投注指令或收益承诺。

## 运行规则

- `runAutomation` 每 10 分钟运行一次，项目时区必须是 `Asia/Shanghai`。
- `runAutomation` 按北京时间判断业务日期，并在缺少阶段产物时调用 `daily-forecast.yml`、`draw-alert-refresh.yml` 或 `noon-settlement.yml` 的 `workflow_dispatch`。
- 日常阶段只接受 schema 2 的 `web/report-status.json`：`forecast_ready=true` 表示基础预测完成，`initial_report_ready=true` 表示临场赔率已绑定且初选报告完成，`settlement_ready=true` 表示前一日模拟账本结算完成。旧 schema 1 和旧的 `decision_snapshot_ready + plan_ready` 组合不能跳过任何阶段。
- 到期的 `pre-kickoff-revalidation.yml` 高于日常阶段调度；每次执行最多 dispatch 一个工作流。赛前复核传入业务日 `target_date` 和带 `+08:00` 时区的 `now_bjt`，因此北京零点后仍可处理前一业务日。
- Apps Script 仅接受规范 UTF-8 JSON 字节、最多两个业务日的索引、与索引 SHA-256 一致的 `status.json`，以及与状态 SHA-256 一致的不可变 revision PNG。任一字节、schema、路径、日期、revision 或哈希不匹配都不发送更新。
- 赛前更新只有在同一 `report_date` 的初始日报已发送时才能发送。同一 revision 中的新终态候选会合并成一封邮件；即使确认时赔率和金额未变，也仍发一封最终确认。这是模拟报告，不会执行任何投注操作。
- 正常邮件发送窗口是北京时间 14:00-18:00。只有当天 `web/report-status.json` 完整、日报 PNG 可下载且实际 SHA-256 与状态文件一致时，才发送附件。
- 到 18:00 时会做最后一次校验：若报告已就绪，发送正常日报；若仍未就绪，只发送一封失败通知，不附带附件，绝不附带昨天或其他旧版本的图片。
- `LAST_INITIAL_SENT_DATE` 和 `LAST_FAILURE_NOTICE_DATE` 保证同一北京时间日期最多出现一封初始日报或一封失败通知。`LAST_SENT_DATE` 仅作为迁移期只读别名，新代码不再写它。`SENT_REVALIDATION_DIGESTS` 以 `report_date + change_digest` 去重，并仅保留最近 30 个业务日。

14:00 初选是 provisional，provisional 金额不计入盈亏。T-90 只做筛查，T-30 才做最终确认；最终金额只能保持或降低，错过窗口必须取消。更新允许跨北京时间午夜，并继续使用原 `report_date`。预测证据、执行赔率、provisional candidates、confirmed simulated bets 和 observation-only shadow rows 是五类不同数据，不能互相替代；只有已成功导入账本的 confirmed active 候选才进入模拟盈亏。

现有 cron 定时运行与 Apps Script dispatch 彼此独立。Apps Script 在 Pages 更新前可能仍看到阶段缺失，此时 cron 和 dispatch 可以为同一阶段各入队一次，造成额外的排队运行。它们共享并发队列；不可变导入清单、初选 generation 指针、单调候选状态和幂等账本写入保证额外运行不会把旧赔率或 provisional 金额当作已确认模拟投入，但可能增加等待时间。部署时不要删除现有 cron。

### Schema 2 阶段契约

- `daily-forecast.yml` 只完成当天不可变数据导入、历史特征和概率预测。`forecast_ready` 要求不可变导入清单有效，且当前赛程、赔率、评级文件与清单记录的字节数和 SHA-256 完全一致；非零比赛日还要求所有官方场次都有有效国内竞彩赔率。它不再要求本阶段不会生成的 `betting_plan_DATE.csv` 或 `daily_decision_DATE.json`。
- `draw-alert-refresh.yml` 必须重新抓取真实的国内临场赔率，绑定不可变 decision bundle，并发布 provisional generation。刷新完成的唯一调度标志是 `initial_report_ready=true`；旧 `plan_ready`、旧 `decision_snapshot_ready` 和 `plan_locked_at_bjt` 不再作为完成条件。
- `noon-settlement.yml` 只结算已经过 T-30 确认并成功导入模拟账本的候选。provisional 金额不进入盈亏，也不创建日期级方案锁。
- 初始邮件要求 schema 2、`forecast_ready`、`initial_report_ready`、`settlement_ready`、`revalidation_ready`、当前结算日期、有效且在结算阶段重新绑定的 provisional SHA-256、完整官方赔率覆盖、全部新阶段数据质量标志、可下载且通过校验的公开复核索引和日报图片哈希同时通过。有 provisional 候选时，索引必须包含当天 `report_date`；零候选日允许规范空索引。任一条件失败都不附带旧图。
- 竞彩网和经身份映射验证的中国足彩网临场源都不可用时，刷新流程必须失败关闭；不得把中午导入赔率或其他缓存伪装成实时赔率。

Settlement retries may update only pending ledger rows; terminal outcomes remain immutable. The 5000 monthly stake cap and 5000 realized-loss stop are separate controls, even though the stake cap usually triggers first.

## 部署前准备

### GitHub 仓库设置

1. 确认生产仓库是 `l18381527760-sketch/sporttery-prediction`，生产分支是 `main`。
2. 打开 `Settings -> Actions -> General`，允许 Actions 运行，并将 `Workflow permissions` 设为 **Read and write permissions**。这是工作流写回报告所需的仓库权限，不是 Apps Script 令牌权限。
3. 打开 `Settings -> Pages`，把 `Source` 设为 **GitHub Actions**。
4. 创建 fine-grained personal access token。`Repository access` 选择 **Only select repositories**，并且只选择 `l18381527760-sketch/sporttery-prediction`；`Repository permissions` 只设置：
   - Metadata: Read-only
   - Actions: Read and write
5. 不要授予其他仓库或额外权限。令牌只能保存到 Apps Script 的 Script Properties；代码、文档、截图、提交信息和日志中不得填写真实令牌或密钥。

### 必须手工配置的 8 项 Script Properties

- `GITHUB_OWNER`：填写仓库所有者 `l18381527760-sketch`。
- `GITHUB_REPO`：填写仓库名 `sporttery-prediction`。
- `GITHUB_TOKEN`：填写上一步创建的 fine-grained token，只能在自己的 Apps Script 项目中填写。
- `REPORT_STATUS_URL`：仓库路径 `web/report-status.json` 位于 Pages artifact 根目录中，因此填写公开地址 `https://l18381527760-sketch.github.io/sporttery-prediction/report-status.json`，不能在域名后再加 `/web/`。
- `REPORT_IMAGE_URL`：仓库路径 `web/daily-report.png` 同样位于 artifact 根目录中，因此填写 `https://l18381527760-sketch.github.io/sporttery-prediction/daily-report.png`。
- `REVALIDATION_INDEX_URL`：仓库路径 `web/revalidation-index.json` 会发布到 Pages artifact 根目录，因此填写 `https://l18381527760-sketch.github.io/sporttery-prediction/revalidation-index.json`，不要加 `/web/`。索引中的 `web/revalidation/DATE/status.json` 和 revision PNG 会按同一 artifact 根目录解析。
- `REPORT_SITE_URL`：`web/index.html` 是 artifact 首页，因此填写 `https://l18381527760-sketch.github.io/sporttery-prediction/`。
- `RECIPIENT_EMAIL`：填写接收日报的邮箱地址，只在 Script Properties 中保存。

以上正好是必须手工配置的 8 项。不要在 `Code.gs` 中写入这些值，也不要在文档中填写真实令牌、密钥或私人邮箱。`appsscript.json` 继续只声明外部 HTTPS 请求、管理本项目触发器和 `gmail.send` 三类最小权限。

### 测试开关与自动状态

`TEST_MODE` 是临时部署开关，不计入上述 8 项必填配置。首次部署时手工新增 `TEST_MODE` 并设为字符串 `true`；验收完成后改成字符串 `false`。`TEST_MODE=true` 可做同一天安全试运行：它会记录拟发送的初始邮件、赛前更新或失败通知，但不调用 Gmail。它不会写入 `LAST_INITIAL_SENT_DATE`，不会写入 `LAST_SENT_IMAGE_SHA256`，不会写入 `SENT_REVALIDATION_DIGESTS`，不会写入 `LAST_FAILURE_NOTICE_DATE`，也不会写入 `LAST_SENT_DATE`。切换到 `TEST_MODE=false` 后，同一北京时间日期仍可发送真实邮件，成功后才写入对应生产去重状态。

TEST_MODE 不测试 Gmail 实际投递，也不能证明 Gmail 授权、额度、收件地址或投递链路正常。GitHub dispatch 仍会真实执行，并继续写入阶段 dispatch 冷却状态，避免测试模式反复入队同一阶段。

运行状态属性由 `Code.gs` 自动写入，包括各日常阶段和赛前复核的 `LAST_*_DISPATCH_DATE`、`LAST_*_DISPATCH_AT`、`LAST_*_DISPATCH_ATTEMPT_DATE`、`LAST_*_DISPATCH_ATTEMPT_AT`，以及 `LAST_INITIAL_SENT_DATE`、`LAST_SENT_IMAGE_SHA256`、`SENT_REVALIDATION_DIGESTS` 和 `LAST_FAILURE_NOTICE_DATE`。这些运行状态不属于手工配置项；日常部署不要创建、修改或复制它们，也不要删除迁移期已有的只读 `LAST_SENT_DATE`。

## 首次部署顺序

严格按下面顺序操作，先测试、再启用定时触发器、最后进入生产模式。

1. 打开现有 Apps Script 项目，不要新建第二个生产项目。
2. 把仓库中已提交的 `apps-script/Code.gs` 全部粘贴到在线编辑器的 `Code.gs`。随后打开 **Project Settings**，勾选显示 `appsscript.json` 清单文件，并用已提交的 `apps-script/appsscript.json` 更新在线清单；确认时区显示 `Asia/Shanghai`。
3. 在 **Project Settings -> Script Properties** 配置上述 8 项 Script Properties。再次确认源码和日志里没有令牌、密钥或私人邮箱值。
4. 按“GitHub 仓库设置”创建仅限目标仓库的 fine-grained token，并把它放进 `GITHUB_TOKEN`，不要放进 GitHub Actions secrets 或源码。
5. 新增临时属性 `TEST_MODE=true`。回到编辑器，选择并手动运行 `runAutomation`；首次运行时按 Google 提示批准权限，包括外部 HTTPS 请求、管理项目触发器和代表当前 Google 账号发送 Gmail。检查 **Executions** 日志，确认只记录动作，没有实际 Gmail 发送。
6. 手动运行 `installAutomationTrigger`。打开左侧 **Triggers** 页面，确认恰好一个每 10 分钟运行的 `runAutomation` 触发器；若还有 `runAutomation` 或旧的 `sendDailyReport` 触发器，重新运行安装函数并再次检查。
7. 在 GitHub 打开 **Actions**，选中 `Daily Sporttery Forecast`，点击 **Run workflow**，分支选 `main`，通过 `workflow_dispatch` 输入当天北京时间日期（`YYYY-MM-DD`）。按当天缺少的阶段继续手动运行 `Draw Alert Refresh` 和 `Afternoon Sporttery Settlement`。完成后检查仓库路径 `web/report-status.json`，并打开公开地址 `https://l18381527760-sketch.github.io/sporttery-prediction/report-status.json`，确认 `report_date` 是当天日期、阶段状态为 `true`、`build_id` 非空；从 `https://l18381527760-sketch.github.io/sporttery-prediction/daily-report.png` 下载同一构建的 PNG，在 PowerShell 运行 `Get-FileHash .\daily-report.png -Algorithm SHA256`，确认结果与状态文件中的 `image_sha256`（PNG 的 SHA-256）一致。报告首页是 `https://l18381527760-sketch.github.io/sporttery-prediction/`。
8. 选中 `Pre-Kickoff Revalidation`。手动恢复时，`target_date` 必须是要处理的业务日 `YYYY-MM-DD`，`now_bjt` 必须是带时区的北京时间，例如 `2026-07-20T00:10:00+08:00`；生产实时处理通常留空 `now_bjt` 让工作流取当前时间。运行后检查 `web/revalidation-index.json`、`web/revalidation/DATE/status.json` 和状态指定的 `web/revalidation/DATE/revision-N-DIGEST.png`；公开 URL 需去掉路径开头的 `web/`。确认索引的 `status_sha256` 等于状态文件原始字节 SHA-256，状态的 `report_image_sha256` 等于 revision PNG 原始字节 SHA-256。
9. 回到 Apps Script，多运行几次 `runAutomation`，核对 dry-run 日志中的日期、调度优先级、初始邮件、赛前更新和 18:00 失败路径都符合预期。只有日志正确后，才把属性改为 `TEST_MODE=false`。测试模式不会占用生产发送状态；真实投递必须在生产模式单独验证。
10. 确认 `.github/workflows/email-report.yml` 在 GitHub Actions 中保持 disabled：在 Actions 左侧选择 `Email Daily Betting Report`，页面应显示该工作流已禁用，并提供 **Enable workflow** 而不是 **Disable workflow**。再检查最近运行记录，确认部署后没有新的定时邮件运行。不要只查看仓库中的 YAML，因为文件仍保留用于审计和回滚参考。

## 日常核对

- 在 Apps Script 的 **Executions** 中检查 `runAutomation` 每 10 分钟执行，没有持续报错或重复 Gmail 发送。
- 在 GitHub Actions 中确认阶段工作流使用当天北京时间日期，并最终发布仓库路径 `web/report-status.json` 与 `web/daily-report.png`；公开 URL 从 Pages artifact 根目录开始，不包含 `/web/`。
- 检查 `Pre-Kickoff Revalidation` 没有连续失败，索引最多只有当前和前一业务日两项。赛前邮件只是对候选的最终模拟确认或取消：预测证据仍是日报决策时点数据，执行赔率是赛前复核时点数据，初始候选金额是 provisional，只有终态 confirmed 才是已确认模拟金额；shadow 路由始终只进观察记录。
- 从 14:00 起，Apps Script 会轮询完整状态并校验图片；18:00 前就绪即可发送，不要求恰好在 14:00 完成。
- GitHub 邮件工作流保持禁用。预测工作流不会调用 `email-report.yml`，GitHub 也不保存 Gmail 应用专用密码。

## 故障恢复

### 令牌被撤销

先把 `TEST_MODE` 改为 `true`。创建新的 fine-grained token，仍然只授权 `l18381527760-sketch/sporttery-prediction`，权限仍然只有 Metadata: Read-only 和 Actions: Read and write；替换 `GITHUB_TOKEN` 后手动运行 `runAutomation` 并检查日志。确认成功后设置 `TEST_MODE=false`，再在 GitHub 撤销旧令牌。不要把新旧令牌写进日志或工单。

### 重复触发器

手动运行一次 `installAutomationTrigger`。它会删除处理函数为 `runAutomation` 或 `sendDailyReport` 的现有触发器，再创建一个 10 分钟触发器。到 **Triggers** 页面确认只剩恰好一个 `runAutomation`；不要手工保留第二个同名触发器。

### 状态或哈希不匹配

不要绕过校验，也不要手工发送旧附件。先比较 `web/report-status.json` 的 `report_date`、各阶段状态、`build_id` 和 `image_sha256`，再下载 `REPORT_IMAGE_URL` 并计算实际 SHA-256。按缺失阶段重新运行对应的 `daily-forecast.yml`、`draw-alert-refresh.yml` 或 `noon-settlement.yml`，等待 Pages 发布同一构建后再运行 `runAutomation`。若到 18:00 仍不匹配，正确结果是一封不附带附件的失败通知。

### 赛前索引、状态或 revision 图片不匹配

不要修改 `SENT_REVALIDATION_DIGESTS` 来强制补发，也不要把旧 revision 图片改名为新文件。从 `REVALIDATION_INDEX_URL` 下载索引原始字节，确认只有 schema version 1 且最多两个日期；再下载对应 `status.json` 原始字节并核对 `status_sha256`，最后下载状态绑定的不可变 PNG 并核对 `report_image_sha256`。确认 Pages 已发布后，手动运行 `Pre-Kickoff Revalidation`：使用原业务日 `target_date`，实时恢复留空 `now_bjt`，只有确定性演练才填入带时区的 `now_bjt`。等待新索引、状态和 revision PNG 全部发布后再手动运行 `runAutomation`。

一次 GitHub dispatch 网络超时可能表示请求已被接收，因此 `LAST_REVALIDATION_DISPATCH_ATTEMPT_*` 会单独冷却 30 分钟；明确非 204 响应不写成功或尝试状态，下一次可重试。不要手工把 attempt 键复制到 confirmed 键。

### Gmail 发送失败

`GmailApp.sendEmail` 抛错时，生产发送失败不会写入发送状态：初始邮件不会写入 `LAST_INITIAL_SENT_DATE` 或 `LAST_SENT_IMAGE_SHA256`，赛前更新保持原 `SENT_REVALIDATION_DIGESTS` 字节不变，失败通知不会写入 `LAST_FAILURE_NOTICE_DATE`。到 Apps Script 的 **Executions** 查看错误，检查当前 Google 账号的 Gmail 服务、授权、发送额度和 `RECIPIENT_EMAIL`。修复原因后，保持 `TEST_MODE=false`，再手动运行一次 `runAutomation` 做受控重试；前一业务日的有效赛前更新不受当天 18:00 初始邮件失败截止影响。只有 Gmail 调用成功后才检查对应状态。不要用 TEST_MODE 代替真实投递验证，也不要通过启用 GitHub 邮件工作流来补发。

### 回滚邮件触发器与赛前系统

先禁用并删除 `runAutomation` 触发器，并在 **Triggers** 页面确认 10 分钟触发器已经消失；在这一步完成前绝不能恢复旧触发器。如果回滚赛前系统，同时在 GitHub Actions 禁用 `Pre-Kickoff Revalidation` 的 schedule 和 Apps Script 对应 dispatch，但不删除已发布的凭证、不可变 revision 产物，也不清空 `SENT_REVALIDATION_DIGESTS`。回滚后的正确行为是零新增模拟投注，不得恢复把 14:00 provisional 初选直接锁定为投入的旧行为。邮件层需要兼容入口时，再恢复之前唯一的每日 `sendDailyReport` 触发器，并确认项目时区仍为 `Asia/Shanghai`。`sendDailyReport()` 会调用同一个 `runAutomation()`，仍保留状态与哈希校验。除非另有经过审查的应急方案，`.github/workflows/email-report.yml` 仍保持 disabled，避免两个发送方同时发信。

## 本地验证命令

在仓库根目录依次运行：

```powershell
python -m unittest tests.test_workflow_schedule -v
python -m unittest discover -s tests -v
node --test tests/apps_script_orchestrator.test.mjs
```

本仓库的阶段验收使用绑定 Node `C:\Users\87562\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe` 运行同一个 Node 测试文件；上面的 `node` 命令适用于已经把 Node 加入 `PATH` 的日常开发环境。
