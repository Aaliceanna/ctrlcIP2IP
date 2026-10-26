# ClipShare

通过**指定 IP** 的端到端剪贴板共享。Windows / Linux 双端，Ctrl+C 之后自动把剪贴板文本推送到已绑定的对端，对端复制也会回推过来。

- **免 Python 运行**：发布页提供单文件可执行程序（Windows `.exe` / Linux ELF）
- **只认具体 IP**：配对靠一条 `clipshare://IP:端口/令牌?name=主机名` 链接，不做 mDNS、不做广播发现
- **零第三方依赖**：Win32 剪贴板走 ctypes，Linux 走 `xclip`/`wl-clipboard`，服务端是标准库 HTTP
- **低开销**：Windows 用 `GetClipboardSequenceNumber` 监听变化，不轮询内容、不挂全局键盘钩子
- **自动防回环**：对端写入后不会再把同一内容推回来
- **开机自启**：Windows 写 `HKCU\...\Run`（无需管理员）；Linux 用 systemd user 服务或 XDG autostart

---

## 一、下载

到 [Releases](../../releases) 页面下载对应平台的压缩包：

| 平台 | 包 | 解压后 |
|---|---|---|
| Windows x64 | `clipshare-windows-x64.zip` | `clipshare.exe`（带控制台，命令行用）、`clipshared.exe`（无控制台，自启常驻用） |
| Linux x86_64 | `clipshare-linux-x64.tar.gz` | `clipshare`（单个可执行文件） |

Linux 还需要一个剪贴板工具（二选一）：

```bash
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland
```

---

## 二、快速开始

### A 机（发起方，比如 192.168.2.65）

Windows：

```bat
clipshare.exe install --bind 0.0.0.0
clipshare.exe link
```

Linux：

```bash
./clipshare install --bind 0.0.0.0
./clipshare link
```

`install` = 写开机自启 + 后台启动 + 生成配置。放行防火墙：

```bat
:: Windows（管理员 CMD）
netsh advfirewall firewall add rule name="ClipShare 8765" dir=in action=allow protocol=TCP localport=8765
```

```bash
# Linux（ufw）
sudo ufw allow 8765/tcp
```

`link` 会输出类似：

```
局域网 IP : 192.168.2.65
配对链接 : clipshare://192.168.2.65:8765/2f8c...e1?name=PC-A
```

### B 机（对端，比如 192.168.1.184）

```bat
clipshare.exe install
clipshare.exe bind "clipshare://192.168.2.65:8765/2f8c...e1?name=PC-A"
```

绑定时会立刻握手验证，成功显示 `握手成功：对端 PC-A v1.0.0`。

> **双向同步需要互相 bind**：在 B 上执行 `link`，把 B 的链接拿到 A 上 `bind` 一次即可。

---

## 三、日常使用

| 场景 | 操作 |
|---|---|
| 复制文本 | 直接 Ctrl+C，对端剪贴板自动更新 |
| 发送大段文本 / 文件内容 | `clipshare send "内容"` 、`clipshare send --file a.txt` |
| 查看状态 | `clipshare status` |
| 探测对端连通 | `clipshare peers` |
| 查看日志 | `clipshare log -n 50` |
| 重启 / 停止 | `clipshare restart` / `clipshare stop` |
| 移除自启 | `clipshare uninstall` |

改配置：`clipshare config --port 8766`、`--bind 192.168.2.65`、`--max-kb 2048`、`--new-token`（改令牌后所有对端需重新 bind 并 restart）。

`bind` / `unbind` / `config` 会**热加载**，不需要重启服务。

---

## 四、跨网段 / 广域网

只要两台机器 TCP 能互通即可，**不需要同网段**，中间有路由也行。

1. 在 A 的路由器把 `8765` 端口映射（Port Forwarding）到 A 的内网 IP
2. 生成公网链接：`clipshare link --wan`（自动探测公网 IP）
3. 或用 DDNS/域名：`clipshare link --host your.domain.com`
4. 到对端执行 `clipshare peers`，显示 `○ 正常` 即通

---

## 五、安全说明

- 所有 API 要求携带 32 字节随机令牌（`X-Token` 头，`hmac.compare_digest` 比对），令牌内嵌在配对链接里，**配对链接等同于密码，不要外发**
- 传输是明文 HTTP：仅在可信网络或 VPN 内使用。确需暴露公网时建议换随机高端口（`config --port 43xxx`）并定期换令牌
- 只同步**纯文本**，单条超过 `max_kb`（默认 512KB）自动跳过；图片/文件不处理

---

## 六、配置文件

- Windows：`%LOCALAPPDATA%\ClipShare\config.json`
- Linux：`~/.local/share/ClipShare/config.json`
- 环境变量 `CLIPSHARE_HOME` 可覆盖（便于多实例/测试）

```json
{
  "name": "PC-A",
  "bind": "0.0.0.0",
  "port": 8765,
  "token": "32位随机令牌",
  "peers": [
    { "name": "PC-B", "host": "192.168.1.184", "port": 8765, "token": "对方令牌" }
  ],
  "max_kb": 512,
  "poll_ms": 200
}
```

---

## 七、从源码运行 / 构建

源码运行（需 Python 3.9+）：

```bash
python clipshare.py status
```

本地构建：

```bat
build.bat        :: Windows → dist\clipshare.exe + dist\clipshared.exe
```

```bash
./build.sh       # Linux → dist/clipshare
```

CI 构建：推一个 `v*` 标签（如 `v1.0.0`）即触发 [`.github/workflows/release.yml`](.github/workflows/release.yml)，
在 Windows / Ubuntu runner 上**原生**编译两个平台产物并自动发布 Release。PyInstaller 不支持交叉编译，所以必须分平台跑。

---

## 八、常见问题

**Q：`bind` 报握手失败？**
依次查：对方是否开机 → 对方服务是否在跑（`status`）→ 防火墙是否放行端口 → 两台机 `ping`/端口是否通 → 令牌是否过期（重新 `link`）。

**Q：端口被占用？**
`clipshare status` 确认是否已在跑；换端口 `clipshare config --port 8766` 后 `restart`，两边都要改。

**Q：Linux 上提示未找到剪贴板工具？**
装 `xclip`（X11）或 `wl-clipboard`（Wayland）。SSH 无图形会话时无法访问剪贴板，这是 X11/Wayland 的限制。

**Q：复制图片/文件不同步？**
设计如此，只同步纯文本。可用 `send --file` 传文本文件内容。

**Q：开机没自启？**
`clipshare status` 看「自启」一行。Windows 自启在**当前用户登录后**触发（HKCU）；Linux systemd user 服务若要未登录也常驻，需执行一次 `loginctl enable-linger $USER`。

---

MIT License
