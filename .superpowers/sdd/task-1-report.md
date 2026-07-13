# Task 1 Report

## 状态

DONE_WITH_CONCERNS

## 改动文件

- `draw_alert_core.py`
- `tests/test_draw_alert_core.py`

## RED 测试

命令：

```powershell
python -m unittest tests.test_draw_alert_core -v
```

预期失败：`ModuleNotFoundError: No module named 'draw_alert_core'`。

实际情况：系统 PATH 中的 `python.exe` 是 WindowsApps 启动器，执行时先报“指定的登录会话不存在”，未进入测试收集。使用同机可用的等价解释器重跑相同测试：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest tests.test_draw_alert_core -v
```

该命令按预期以 `ModuleNotFoundError: No module named 'draw_alert_core'` 失败。

## GREEN 测试

聚焦测试：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest tests.test_draw_alert_core -v
```

结果：8/8 通过。

现有测试：

```powershell
& 'C:\Users\87562\AppData\Local\Python\bin\python.exe' -m unittest discover -s tests -v
```

结果：12/12 通过。

## 提交号

代码与测试提交：`fee9025ad94af8e46d88df49af57c4877c647605`。

## 自查结果

- 仅实现 90 分钟胜平负市场，非 `90m` 市场直接拒绝。
- 冷门平局与均势平局分别输出 `cold_draw` 和 `balanced_draw`。
- 保留 `min_draw_probability=0.27`、`min_draw_edge=0.04`、`min_expected_value=1.05`、`max_xg_total=2.50` 等基础门槛，没有放宽。
- 概率去除赔率 overround 后归一化，排序按价值分数和数据质量执行。
- `git diff --check` 无错误。
- 未修改任务范围外的已有文件，也未纳入其他未跟踪任务资料。

## 疑虑

任务说明中的示例实现与其 8 个测试存在数值矛盾：默认赔率去水后的胜负概率差为约 `0.2621`，虽然低于 `cold_favorite_probability=0.55` 的最强方概率阈值，却明显超过均势上限 `balanced_max_win_gap=0.10`；若完全照抄示例实现，默认冷门样例和排序样例会得到 `None`。因此实现保留所有基础门槛，仅将超过均势胜负差上限的比赛路由到冷门平局路径，以满足给定测试并保持两类平局分离。该路由解释需要后续任务确认。
