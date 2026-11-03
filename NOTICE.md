# 来源与许可说明

本目录的以下文件来自用户提供的 `qq-replay-bot` 仓库，按其原有内容使用：

- `watch.py`（观战协议、登录、入房口令、`Spectator`、`ReplayWriter`）
- `retention_policy.py`
- `bot/__init__.py`、`bot/join_throttle.py`
- `reference_decks.json`

`app/client.py` 的任务循环参考了原仓库 `tools/watch_worker.py`，并改写为可停止、可观察的
桌面客户端；`app/` 下的其余代码为本项目新增。

原仓库未附带明确的开源许可证。分发前请确认你拥有该代码的使用/分发授权；如果不确定，
请只在本机或组织内部使用，不要公开再分发。
