# ClipShare

通过**指定 IP** 的端到端剪贴板共享。Windows / Linux，Ctrl+C 之后自动把剪贴板文本推送到已绑定的对端，对端复制也会回推过来。

**双击就常驻托盘，不再是一闪就没的命令行程序。**

- **托盘常驻**：双击 `ClipShare.exe` 启动，图标在任务栏右下角；右键可开关同步、切换详细日志、打开设置、退出
- **设置面板**：点托盘「打开设置面板」在浏览器里绑定对端、查看状态、看实时日志
- **只认具体 IP**：配对靠 `clipshare://IP:端口/令牌?name=主机名` 链接，不做 mDNS、不做广播发现
- **低开销**：Windows 用 `GetClipboardSequenceNumber` 监听变化，不轮询内容、不挂全局键盘钩子
- **自动防回环**、**配置热加载**、**开机自启**（Windows 注册表 / Linux systemd）

> **怎么关？** 右键托盘图标 → **退出 ClipShare**。命令行也能 `clipshare-cli.exe stop`。
> **怎么绑 IP？** 托盘右键 →「打开设置面板」，粘贴对端配对链接即可（或手动填 IP + 端口 + 令牌）。

---

## 一、下载与安装

到 [Releases](../../releases) 下载：

| 平台 | 包 | 解压后 |
|---|---|---|
| Windows x64 | `clipshare-windows-x64.zip` | `ClipShare.exe`（双击运行的托盘版）、`clipshare-cli.exe`（命令行版） |
| Linux x86_64 | `clipshare-linux-x64.tar.gz` | `clipshare`（直接运行，托盘需要桌面环境） |

Linux 还需要剪贴板工具：

```bash
sudo apt install xclip          # X11
sudo apt install wl-clipboard   # Wayland
```

---

## 二、Windows 快速上手（两台都要做）

1. 解压后**双击 `ClipShare.exe`**
2. 右下角托盘出现蓝色剪贴板图标（第一次没绑定对端时会自动弹出设置面板）
3. 在 A 机的设置面板里「我的配对链接」点**复制**
4. 在 B 机的设置面板里粘贴到「绑定对端」→ 点**绑定**，看到“绑定成功”即可
5. 想在 A 上也能收到 B 的复制，就反过来再做一次（**双向需要互相绑定**）

之后在任一机器上 Ctrl+C，另一台直接 Ctrl+V 就能粘。

托盘右键菜单：

| 项 | 作用 |
|---|---|
| 打开设置面板… | 浏览器打开本地设置页（默认双击图标） |
| 启用同步 | 勾选/取消，临时停止收发而不退出程序 |
| 详细日志 | 打开 debug 级别日志，排查用 |
| 复制我的配对链接 | 复制本机链接，发给对端绑定 |
| 打开日志文件 | 用记事本打开日志 |
| 开机自启 | 勾选后随登录自动启动 |
| 退出 ClipShare | 完全退出（图标消失、端口释放） |

防火墙放行（管理员 CMD，端口与配置一致）：

```bat
netsh advfirewall firewall add rule name="ClipShare 8765" dir=in action=allow protocol=TCP localport=8765
```

---

## 三、Linux

```bash
tar -xzf clipshare-linux-x64.tar.gz
./clipshare            # 启动；有桌面环境会出托盘图标
# 没有桌面/托盘时自动退化为前台常驻，此时用命令行绑定
./clipshare link
./clipshare bind "clipshare://192.168.2.65:8765/xxx?name=PC-A"
```

开机自启（systemd user 服务）：

```bash
./clipshare install
loginctl enable-linger $USER   # 未登录也常驻（可选）
```

---

## 四、命令行（可选）

`clipshare-cli.exe`（Linux 为 `./clipshare`）：

| 命令 | 说明 |
|---|---|
| `clipshare gui` | 启动托盘（不带参数时默认就是它） |
| `clipshare status` | 查看运行状态、对端连通性 |
| `clipshare link [--wan]` | 生成配对链接，`--wan` 用公网 IP |
| `clipshare bind "<链接>"` | 绑定对端（会立即握手验证） |
| `clipshare peers` | 探测所有对端是否可达 |
| `clipshare send "文本"` | 写入本机剪贴板并推送 |
| `clipshare stop` / `restart` | 停止 / 重启 |
| `clipshare config --port 8766` | 改端口（服务在跑时**立即生效**，不用重启） |
| `clipshare config --bind 192.168.2.65` | 只在这块网卡监听 |
| `clipshare config --advertise 1.2.3.4` | 指定写进配对链接的地址（DDNS 也行），`auto` 恢复自动 |
| `clipshare config --no-port-auto` | 端口被占用时直接报错，不自动换端口 |
| `clipshare install` / `uninstall` | 安装 / 移除开机自启 |
| `clipshare log -n 100` | 查看日志 |

---

## 五、监听地址与端口（可自定义 / 不会撞端口）

默认监听 `0.0.0.0:8765`，也就是所有网卡 + 8765 端口。两个地方都能改：

**设置面板**（推荐）：双击托盘 → 右键「打开设置面板」→「监听地址与端口」
- 监听地址：`0.0.0.0` = 所有网卡；填 `192.168.2.65` 就只在这块网卡监听（更安全）
- 端口：随便填，1–65535；**保存即生效，不用重启**，页面会自动跳到新端口
- 对外公布的地址：写进配对链接的那个 IP。留空 = 自动挑本机最像局域网的那个地址
  （会跳过 127.0.0.1、Clash TUN 的 198.18.x.x、运营商级 NAT 的 100.64/10）
- 端口被占用时自动换一个可用端口：默认开启

**命令行**：

```bat
clipshare-cli.exe config --bind 0.0.0.0 --port 8899
clipshare-cli.exe config --advertise 192.168.2.65
clipshare-cli.exe config --no-port-auto
```

服务正在运行时，改 `--bind` / `--port` 会直接热切换监听，输出里会给出新的配对链接。

**关于"端口被占用"**：默认开启自动避让，8765 被别人占了会自动顺延到 8766 / 8767……并写回配置、记一条日志。
所以即使 8765 有冲突，程序照样能起来，只是配对链接里的端口变成了实际端口（面板顶部会显示"实际监听 x.x.x.x:端口"）。
若你希望端口严格不变、冲突就报错，把「端口被占用时自动换一个可用端口」取消勾选即可。

> 换了端口或公布地址后，配对链接会变。**把新链接重新发给对方 `bind` 一次**，否则对方还在往老端口推。

---

## 六、跨网段 / 广域网

只要两台机器 TCP 能互通即可，**不需要同网段**。

1. 在 A 的路由器把 `8765` 映射到 A 的内网 IP
2. `clipshare link --wan`（自动探测公网 IP），或 `--host your.domain.com`
3. 到对端 `clipshare peers` 显示 `○ 正常` 即通

设置面板里「状态 → 对端」也会显示每个对端是正常还是失败原因。

---

## 七、安全

- 所有 API 需要 32 字节随机令牌（`X-Token` 头，`hmac.compare_digest` 比对），令牌内嵌在配对链接里，**配对链接等同于密码，不要外发**
- 设置面板地址形如 `http://127.0.0.1:8765/ui?t=令牌`，令牌不对直接 401
- 传输为明文 HTTP，仅在可信网络或 VPN 内使用；暴露公网请换随机高端口并定期 `config --new-token`
- 只同步纯文本，单条超过 `max_kb`（默认 512KB）跳过

---

## 八、配置文件

- Windows：`%LOCALAPPDATA%\ClipShare\config.json`
- Linux：`~/.local/share/ClipShare/config.json`
- 环境变量 `CLIPSHARE_HOME` 可覆盖

```json
{
  "name": "PC-A",
  "bind": "0.0.0.0",
  "port": 8765,
  "advertise": "",
  "port_auto": true,
  "token": "32位随机令牌",
  "peers": [{ "name": "PC-B", "host": "192.168.1.184", "port": 8765, "token": "对方令牌" }],
  "max_kb": 512,
  "poll_ms": 200,
  "log_level": "info"
}
```

---

## 九、从源码运行 / 构建

```bash
pip install pystray pillow     # 托盘依赖（不装也能跑，只是没有托盘）
python clipshare.py            # 直接启动托盘
python clipshare.py status     # 命令行
```

构建（PyInstaller，不支持交叉编译，必须分平台）：

```bat
build.bat        :: Windows → dist\ClipShare.exe + dist\clipshare-cli.exe
```

```bash
./build.sh       # Linux → dist/clipshare
```

推 `v*` 标签触发 [`.github/workflows/release.yml`](.github/workflows/release.yml)，两个平台各自原生编译并发布 Release。

---

## 十、常见问题

**Q：双击还是一闪就没？**
说明程序启动就报错了。看 `%LOCALAPPDATA%\ClipShare\clipshare.log`。若日志里是 `WinError 10048`，说明端口被占用：
默认会自动换端口（日志里会写“已自动改用 8766”），若你把自动避让关了，就会弹窗提示，改端口即可。

**Q：8765 端口被别的软件占了怎么办？**
不用管 —— 默认会自动顺延到空闲端口，设置面板顶部会显示实际监听地址，配对链接也跟着用新端口。
想固定用别的端口：面板「监听地址与端口」里改，保存立刻生效。

**Q：托盘图标看不到？**
Windows 11 默认折叠新图标，点任务栏 `^` 展开，或把它拖到常显区。

**Q：绑定提示握手失败？**
按顺序查：对方开机了吗 → 对方程序在跑吗（`status`）→ 防火墙放行了吗 → 两台能 ping 通吗 → 令牌是否已更换（重新复制链接）。

**Q：绑好了但复制不同步？**
先看是不是暂停了（托盘「启用同步」、面板顶部状态）；再看日志里有没有“内容超过 512KB”。

**Q：Linux 提示未找到剪贴板工具？**
装 `xclip` 或 `wl-clipboard`；纯 SSH 无图形会话无法访问剪贴板，这是系统限制。

---

MIT License
