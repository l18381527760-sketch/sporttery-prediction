# 世界杯每日预测

这是一个可在本地运行的足球比赛概率分析工具。它根据赛程、球队数据、竞彩赔率和赛前市场信息，生成胜平负概率、淘汰赛晋级倾向、比分分布、模拟方案和每日网页报告。

结果是概率分析与模拟记账，不保证盈利或任何比赛结果。请把它当作复盘和研究工具，而不是收益承诺。

## 预测内容

- Elo、攻防能力、近期状态、休息时间和主场因素共同形成基础预测。
- 泊松进球分布用于计算比分和 90 分钟胜平负概率；淘汰赛的晋级倾向单独展示，不会混入 90 分钟结算。
- 保存的国内竞彩赔率用于方案展示和结算；外部市场只提供分析证据，不能替换国内方案赔率。
- 主方案由 `generate_betting_plan.py` 生成，所有模拟投入每日合计不超过 500 元。
- 预测概率与是否进入方案是两层决策：系统先计算模型概率，再对竞彩赔率去除返还率影响得到市场公平概率；只有保守概率仍具备足够优势和正期望值时才进入方案。
- 每日预算是上限，不是必须使用的额度。没有通过门槛的比赛时会正式记录“主方案观望”及原因。

## 八项风险控制

- **允许观望**：当天可生成零投注方案，未使用预算不会转移或补投。
- **价值门槛**：原模型概率、联赛校准概率、保守决策概率和去水市场概率分别保存，便于复盘。
- **赔率时点**：保存开盘时点、13:30 决策时点及开赛前一小时内快照；已开赛比赛不会再写入赛前快照。
- **联赛校准**：单个联赛至少积累 30 场已结算样本，并且时间顺序验证集的 Brier 误差确实改善后，才启用最多正负 5 个百分点的收缩修正。
- **防止案例过拟合**：已知经典比赛只用于回归测试和规则解释，不直接给模型加权；任何新规则至少需要 30 场完整样本。
- **限制串关**：默认优先 2 串 1，最多 3 串 1；3 串 1 需先完成 30 个有效模拟日，并且每一腿和整体组合均通过正价值门槛。串关投入不超过 30 元。
- **完整风险指标**：报告展示命中率、ROI、Brier、Log Loss、概率校准误差、CLV、最大回撤、连续未中，以及各玩法独立盈亏。
- **模拟账户闸门**：至少完成 30 个有结算数据的模拟日，且 ROI 与 CLV 同时为正，才显示“可人工复核”；系统不会自动转为真实投注。本月模拟投入上限 5000 元，累计月亏损达到 5000 元后停止当月新增模拟投注。

## 本地运行

在 Windows PowerShell 中预测今天：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\predict_today.ps1
```

预测指定日期：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\predict_today.ps1 -Date 2026-07-12
```

只在控制台查看而不写入结果文件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\predict_today.ps1 -Date 2026-07-12 -NoFiles
```

生成某日的完整本地报告，可按顺序运行：

```powershell
python predict_today.py --date 2026-07-12
python generate_betting_plan.py --date 2026-07-12
python collect_market_heat.py --date 2026-07-12 --offline
python generate_draw_alert.py --date 2026-07-12
python draw_alert_ledger.py --settle
python build_site.py
python build_daily_image.py
```

`--offline` 不访问外部市场，只使用已保存的数据，适合复现和检查。日常在线采集时，`collect_market_heat.py` 会捕获并记录可选市场来源的错误，同时保留仍通过验证的可用来源；后续预警只按当前可用来源和数据质量门槛生成，绝不编造缺失数据。

## 平局预警

每日报告可出现 0 到 4 场平局预警。0 场不是故障，表示当天没有比赛同时通过概率、赔率价值和数据质量门槛。

- **冷门平局**：一方被市场明显看好，但赛前证据显示其 90 分钟内存在被守平的合理风险。
- **均势平局**：双方实力接近、预期总进球偏低，平局被模型判断为相对被低估的结果。
- 排名第 2、3、4 的预警会使用比第 1 场更严格的概率、优势和预期价值门槛；不会为了凑满 4 场放宽标准。
- 同一联赛最多出现 2 场预警，避免风险过度集中。
- 预警和所有相关方案只按常规时间 90 分钟比分结算，加时赛和点球不改变平局结算结果。

### 观察、晋级与去重

冷门平局与均势平局分别独立观察。每个子类型在取得 **30 场已结算样本** 前，始终是零新增金额观察；达到 30 场后，仍需同时通过 ROI、CLV、概率校准、最大回撤和近期稳定性检查，才可能升级。

升级后的独立预警每场只做 10 至 30 元的模拟投入；当日全部预警的新增投入不超过 80 元，连同主方案在内的每日模拟投入不超过 500 元。

若预警与主方案命中同一场比赛，系统复用主方案的金额和结算记录，只添加预警标签和独立观察数据，不会重复投入，也不会重复计算利润。

## 守护式学习

学习模块只使用不可变的赛前快照，避免把赛后数据写回预测特征。新模型先以候选者身份进行影子评估，与当前冠军模型在相同的后续样本上比较。

候选模型要同时证明概率准确度和模拟经济指标均达到要求，才会晋级。系统也会按联赛暂停表现不佳的付费预警，并保留可逆回滚：训练或验证失败时继续使用有效的冠军模型；没有有效冠军时回退到基础预测。学习过程不会承诺改善或盈利。

## 赛前复核

14:00 初选是 provisional，provisional 金额不计入盈亏。它只表示模型在决策时点给出的临时候选，不是已投入金额。系统在每个候选最早开赛时间附近执行 T-90 筛查和 T-30 最终确认；最终金额只能保持或降低，不能比 provisional 金额增加。错过窗口必须取消，不能用赛后或已开赛数据补做确认。

复核允许跨北京时间午夜，业务日仍绑定初选的 `report_date`。预测证据来自不可变的决策时点数据；执行赔率来自 T-90/T-30 新抓取的国内竞彩快照；provisional candidates 只进入临时候选状态；只有 T-30 终态为 confirmed 且凭证已导入账本的项目才是 confirmed simulated bets。`route=shadow` 始终是 observation-only shadow rows，金额为零，不得进入付费账本。

生产配置 `pre_kickoff_revalidation.mode` 首次上线必须保持 `shadow`。完整观察一个包含错峰开赛和跨午夜比赛的业务日并通过人工核对后，才允许用另一笔经过审查的提交改为 `active`；这不会自动启用 value-v4，也不会连接真实投注账户。若复核链路不可用，回滚后的正确行为是零新增模拟投注，同时保留已有凭证和账本，不恢复把 14:00 初选直接锁成投入的旧行为。

## 每日云端流程（北京时间）

| 时间 | 工作内容 |
| --- | --- |
| 12:15 | 导入基础竞彩数据，生成预测、主方案和第一版平局预警，并重建网页与日报图片。 |
| 13:30 | 重新导入竞彩数据并刷新决策时点快照、预测、方案与方案锁；这些必需步骤任一失败都会停止发布。只有市场证据采集和平局预警刷新是可选步骤，失败时保留最近有效结果并继续构建。 |
| 13:45 | 结算前一天的 90 分钟结果，更新指标、训练模型并重建报告。 |
| 14:05 | 再次尝试结算，处理结果延迟或首次任务未完成的情况。 |
| 14:00-18:00 | Apps Script 的 `runAutomation` 每 10 分钟读取当天 `web/report-status.json`，校验日报 PNG 的 SHA-256 后发送；18:00 仍未就绪时只发一封不带附件的失败通知。 |
| T-90 / T-30 | `pre-kickoff-revalidation.yml` 按候选最早开赛时间采集新的国内赔率，完成筛查、确认或取消，并发布按业务日绑定的不可变更新图片。 |
| 每 30 分钟 | 保存仍未开赛比赛的官方赔率快照；开赛前一小时内自动标记为临场快照，供 CLV 与复盘使用。 |

Evidence workflow contract: `--reconcile-days 7` runs oldest-first before
historical features, proven settlement, metrics, and shadow training. New live
captures use strict live snapshot schema 2 with canonical immutable filenames;
decision, T-90, and T-30 remain separate phase evidence. Required steps fail
before status publication or commit when any upstream contract fails.

GitHub Actions retries do not duplicate canonical results, simulated ledger
entries, or mail. Apps Script remains the sole email sender in the Beijing
14:00-18:00 window; GitHub Actions only generates and publishes artifacts.

部署后，Apps Script 是唯一的邮件发送方，`.github/workflows/email-report.yml` 在 GitHub Actions 中保持 disabled。GitHub Actions 负责生成和发布报告，Apps Script 负责调度、轮询、校验和发信；两边都在云端运行，所以电脑可以关机。云端设置请阅读 [CLOUD_SETUP.md](CLOUD_SETUP.md)，Apps Script 的逐步部署与恢复请阅读 [apps-script/README.md](apps-script/README.md)。不需要 Google 日历。

现有工作流 cron 与 Apps Script dispatch 彼此独立，可能在 Pages 尚未更新时为同一阶段各排队一次；共享并发队列、写入前方案锁检查和同日幂等状态使额外运行保持安全。仓库中的 `web/` 是 Pages artifact 根目录，所以公开状态和图片 URL 不包含 `/web/`。

## 常用文件

- `data/fixtures.csv`：赛程。
- `data/team_ratings.csv`：球队评分和状态数据。
- `predict_today.py`：生成比赛预测。
- `generate_betting_plan.py`：生成主方案与模拟记账输入。
- `collect_market_heat.py`：收集或读取赛前市场证据。
- `generate_draw_alert.py`：生成 0 到 4 场平局预警。
- `draw_alert_ledger.py`：按 90 分钟结果结算预警和观察指标。
- `draw_model_learning.py`：执行受保护的候选模型训练和评估。
- `live_odds.py`、`provisional_plan.py`、`revalidation.py`：采集执行赔率、发布 provisional 候选并执行 T-90/T-30 单调复核。
- `revalidation_reporting.py`：发布 `web/revalidation-index.json`、按业务日状态和不可变 revision PNG。
- `build_site.py`、`build_daily_image.py`：生成网页和邮件日报图片。
- `web/index.html`、`web/daily-report.png`：最新网页与日报图片。

## 查看报告

直接打开网页：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\open_website.ps1
```

或启动本地服务后访问 `http://127.0.0.1:8765/`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\serve_website.ps1
```

## Shadow portfolio activation audit

The value-v4 activation gate is a deterministic mechanical-safety audit. It
rebuilds eligible repository dates from their saved predictions, fixtures,
domestic odds evidence, and schema-valid pre-kickoff decision snapshots:

```powershell
$env:OPENBLAS_NUM_THREADS = "1"
.superpowers\sdd\runtime\verify-venv\Scripts\python.exe audit_shadow_portfolio.py --from 2026-07-11 --through 2026-07-18
```

The result is written to
`output/shadow_portfolio_activation_audit.json`. Missing and invalid evidence
is excluded explicitly and is never reconstructed from later odds or match
results. The gate fails closed when no dates qualify. Historical ROI and win
rate do not affect this safety gate.

`betting_config.json` may change from `shadow` to `active` only after the
persisted report is schema-valid, has `passed: true`, and contains at least one
checked date. The strategy remains simulation-only, historical plans, locks,
and ledger rows are immutable, and `real_money_automation` remains `false`.
