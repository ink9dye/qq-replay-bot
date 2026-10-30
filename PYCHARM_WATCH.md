# PyCharm统一观战联动

用户要求：开始观战时自动在PyCharm打开并运行watch.py，Run窗口控制全部观战，停止后没有后台观战继续。

已准备的改变：

- `watch · 全部观战` 固定运行配置，只运行项目watch.py；不允许并行启动。
- 普通启动入口通过PyCharm官方本机MCP请求打开watch.py、启动该配置。接口未配置或拒绝时明确失败，不退回隐藏后台录制。
- watch.py在IDE内启动QQ、分享页面和同一条观战循环；独立名单与授权QQ订阅合并去重，同一房间不重复连接。
- 全部观战与Run进程同生命周期。已有实例由同一运行锁阻止重复。
- MCP客户端仅连接回环地址，只请求打开watch.py和执行固定配置，不发送账号密码，不执行终端工具，不传动态命令参数。

用户已于2026年10月24日明确授权启用PyCharm本机MCP与自动执行运行配置。接口只使用设置界面提供的回环地址；本程序仅调用打开watch.py和执行固定运行配置，不注册全局Codex客户端连接。

启动：双击start-watch.bat、start.bat或start-all.bat，或使用旧命令python -m bot.main，都会转到PyCharm中的固定watch运行配置。在PyCharm中也可直接运行watch.py。已有统一观战在运行时只打开文件，不重复执行、不重启现有Run。

停止：使用PyCharm Run窗口的停止按钮。只关闭watch.py编辑器标签不代表停止程序。停止后全部观战、QQ指令和录像网页服务共同结束，不留下另一个后台观战实例；再次双击启动入口可恢复。停止期间已保存的录像与索引保留。

配置：项目data/pycharm-link.json保存实际HTTP接口地址和已安装PyCharm路径。接口不可用时明确报错，不退回隐藏后台观战。不增加开机自启或定时任务。

相关官方文档：https://www.jetbrains.com/help/pycharm/mcp-server.html
