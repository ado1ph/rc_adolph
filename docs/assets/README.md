# README 图像与证据

- `architecture.json`：逻辑拓扑图源，Fireworks Tech Graph 的 Flat Icon / showcase 格式。
- `architecture.svg`、`architecture.png`：同一架构的矢量与 1920px 位图；展示组件与事务边界，不代表已部署 HA。
- `demo-report.json`：本机真实 PostgreSQL、独立 API/Worker 与 Mock 的脱敏执行报告。
- `demo-results.svg`、`demo-results.png`：根据该报告生成的执行结果可视化，不是应用管理后台截图。

架构图使用 `fireworks-tech-graph` skill 生成。将 `SKILL_ROOT` 指向本机 skill 目录后，在仓库根目录执行：

```sh
python3 "$SKILL_ROOT/scripts/fireworks.py" validate architecture docs/assets/architecture.json
python3 "$SKILL_ROOT/scripts/fireworks.py" render architecture docs/assets/architecture.json docs/assets/architecture.svg
python3 "$SKILL_ROOT/scripts/fireworks.py" check docs/assets/architecture.svg
python3 "$SKILL_ROOT/scripts/fireworks.py" export-png docs/assets/architecture.svg docs/assets/architecture.png --width 1920
```

重新运行并生成 SVG，不需要绘图 Python 依赖：

```sh
.venv/bin/python -m notifier.production.demo --output /tmp/new-demo-report.json
python3 scripts/render_demo_report.py /tmp/new-demo-report.json /tmp/new-demo-results.svg
```

如安装了 librsvg，可以导出与当前 README 相同宽度的 PNG：

```sh
rsvg-convert --width 1920 --output /tmp/new-demo-results.png /tmp/new-demo-results.svg
```

这些输出路径必须尚不存在。报告中的 `observed_statuses` 仅是 API 轮询实际观察到的状态，可能漏过短暂中间态；`attempts` 是持久化尝试记录，不能把两个列表当作同一种时间线。供应商调用数与副作用数来自本地 Mock 内存，仅用于验证该次运行。
