# 世界杯每日自动预测

这是一个可本地运行的世界杯比赛预测工具。它按日期读取赛程和球队强度数据，自动生成当天比赛的胜平负概率、淘汰赛晋级概率、比分分布和简短结论。

## 预测方法

模型采用足球预测里比较稳健的一套组合：

- Elo 强度差：衡量两队长期实力差距。
- 攻防修正：分别调整进攻火力和防守质量。
- 近期状态：用最近比赛表现修正预期进球。
- 休息天数：考虑体能影响。
- 主场/准主场优势：东道主或明显地理优势可加权。
- 市场赔率融合：如果录入赔率，会把市场隐含概率按权重并入模型。
- Poisson 进球分布：生成比分概率、胜平负概率。
- 淘汰赛晋级模拟：90 分钟打平后，用实力差估计加时/点球晋级倾向。

## 文件

- `data/fixtures.csv`：赛程。每天更新这里即可。
- `data/team_ratings.csv`：球队评分。可每天根据新闻、伤停、赔率更新。
- `config.json`：模型权重。
- `predict_today.ps1`：主程序，Windows 自带 PowerShell 即可运行。
- `predict_today.py`：Python 版本，适合已有 Python 环境时使用。
- `build_site.py`：把预测结果生成成可每天查看的网站。
- `generate_betting_plan.py`：按预算生成模拟投注方案，并根据赛果更新盈亏账本。
- `web/index.html`：世界杯预测看板网页。
- `open_website.ps1`：打开预测网站。
- `serve_website.ps1`：启动本地网站服务，地址为 `http://127.0.0.1:8765/`。
- `settle_bets.ps1`：录入赛果后更新模拟盈亏。
- `run_daily.ps1`：每天运行一次并输出结果。
- `install_daily_task.ps1`：在 Windows 上创建每日自动任务。
- `output/`：生成的预测结果。

## 运行

预测今天：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\predict_today.ps1
```

预测指定日期：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\predict_today.ps1 -Date 2026-07-11
```

只看控制台结果，不生成文件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\predict_today.ps1 -Date 2026-07-11 -NoFiles
```

## 每天自动运行

先确认 `install_daily_task.ps1` 里的时间是否合适，默认每天早上 08:00 运行。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_daily_task.ps1
```

之后每天会在 `output/` 下生成：

- `predictions_YYYY-MM-DD.md`
- `predictions_YYYY-MM-DD.csv`

同时会刷新：

- `web/index.html`

## 云端自动运行

如果不想让电脑一直开机，可以用 GitHub Actions 云端自动运行。设置方法见：

- `CLOUD_SETUP.md`

云端默认时间：

- 北京时间 11:30：生成当天预测和模拟投注方案。
- 北京时间 12:00：抓取前一天赛果并结算盈亏。

## 打开网站

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\open_website.ps1
```

也可以直接打开 `web/index.html`。

## 模拟投注与结算

每天默认模拟预算 300，最高不超过 500。当前分配：

- 总进球：100
- 胜平负：100
- 半全场：50
- 比分串：50，优先 4 串 1；可用比赛不足 4 场时自动降为 2 串 1

每种玩法只选一个方案。赔率来自竞彩网官方接口；如果竞彩网没有给出某个选项赔率，该选项不会进入方案。

第二天把赛果填到 `data/bet_results.csv`，格式如下：

```csv
date,team_a,team_b,home_goals,away_goals,half_home_goals,half_away_goals
2026-07-11,Spain,Belgium,2,1,1,0
```

然后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\settle_bets.ps1
```

网站会更新累计投入、已结算注数、命中率和累计盈亏。

每天中午 12 点自动结算可以安装计划任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_noon_settlement_task.ps1
```

如果 Windows 提示权限不足，请用管理员权限打开 PowerShell 后再运行。

如果想用浏览器地址访问：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\serve_website.ps1
```

然后打开：

```text
http://127.0.0.1:8765/
```

## 数据更新建议

每天比赛前更新两张表：

1. 在 `data/fixtures.csv` 添加当天比赛，淘汰赛的 `stage` 填 `knockout`、`quarterfinal`、`semifinal`、`final` 都可以。
2. 在 `data/team_ratings.csv` 更新：
   - `elo`：球队基础强度。
   - `attack`：进攻修正，通常 -0.20 到 +0.25。
   - `defense`：防守修正，越高表示防守越好，通常 -0.20 到 +0.25。
   - `form`：近期状态，通常 -0.15 到 +0.15。
   - `injury`：伤停惩罚，0 到 -0.20。
   - `rest_days`：距上一场休息天数。
   - `home_adv`：主场或地理优势，通常 0 到 +0.12。

## 重要说明

足球比赛随机性很高。这个工具输出的是概率，不是保证结果。专业使用时，重点看长期复盘的校准度，而不是单场命中。
