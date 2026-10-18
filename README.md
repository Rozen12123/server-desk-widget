# Server Desk

浅色无边框 Windows 桌面小组件。它贴在桌面层，通过系统自带的 OpenSSH 客户端读取多台 Linux 服务器的 CPU、内存、根分区磁盘和运行时间；装有 `nvidia-smi` 的服务器还显示 GPU 利用率。离线和超过 90% 的资源会突出显示。

## 运行

需要 Windows 10/11、Python 3.10+（带 Tkinter）、Pillow 和 OpenSSH 客户端。

1. 将 `servers.example.json` 复制为 `servers.json`，填写服务器的 `name`、`host`、`user`。如 SSH 使用非默认端口或专用私钥，再填写 `port`、`key_file`。
2. 首次连接一台服务器时，在终端运行 `ssh user@host`，核对并接受主机指纹，确认密钥登录可用。小组件要求主机已在 `known_hosts` 中，且不会弹出密码提示。
3. 双击 `start.bat`，或在本目录运行 `python widget.py`。

程序可使用系统 OpenSSH 配置中的服务器别名和密钥，不会复制或保存私钥。本地服务器配置 `servers.json` 会被 Git 忽略，不纳入项目提交。

拖动小组件顶部可改变位置，位置保存在 `widget_state.json`。右键可立即刷新、编辑配置、重新加载配置、切换开机启动或退出。默认每 30 秒更新一次，`refresh_seconds` 可设置为 5–3600 秒。

使用 `python widget.py --install-startup` 为当前 Windows 用户启用登录后自动启动；使用 `python widget.py --remove-startup` 关闭。启动项存放在 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，名称为 `ServerDeskWidget`，不需要管理员权限。`python widget.py --preview` 用普通窗口显示界面，适合调试。

首次运行且没有配置时，界面会提示配置错误，不会显示伪造的实时指标。旧版详细窗口仍可运行 `python server_desk.py`，日常使用请启动 `widget.py`。

## 配置示例

```json
{
  "refresh_seconds": 30,
  "servers": [
    {
      "name": "训练节点 A",
      "host": "192.0.2.10",
      "user": "monitor",
      "port": 22,
      "key_file": "C:/Users/you/.ssh/id_ed25519"
    }
  ]
}
```

推荐在服务器上使用权限受限的只读普通账户。程序不会保存密码；`servers.json` 已加入 `.gitignore`。SSH 使用 `BatchMode=yes` 和 `StrictHostKeyChecking=yes`，每次采集 12 秒超时。Linux 主机需要 `/proc/stat`、`/proc/meminfo`、`/proc/uptime`、`df`、`awk` 和 `sh`；GPU 指标可选。

目前磁盘百分比针对根分区 `/`。CPU 百分比基于约 250 ms 的两次 `/proc/stat` 采样，因此适合快速查看趋势，不用于计费或长期统计。GPU 利用率取所有 GPU 的最大值，卡片显示 GPU 数量。