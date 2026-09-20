# kde-terminal-theme-sync

让 **Kitty** 和 **Starship** 的配色跟着 **KDE Plasma** 走。

换壁纸取色、手动切换配色方案、Plasma 6.5+ 的昼夜自动切换 —— 三种情况终端都会自动跟上。
两个纯标准库的 Python 脚本，一条 systemd Path 链路，零外部依赖。

---

## 它是怎么工作的

KDE 把当前配色写在 `~/.config/kdeglobals` 里。这套东西监听那个文件，一有变化就跑：

```
kdeglobals 变化
 └─ kde-theme-sync.path              （systemd --user，监听文件修改）
     └─ kde-theme-sync.service        oneshot
         └─ auto-sync.sh
             ├─ 生成到临时文件
             ├─ 与现有配置逐字节比对
             ├─ 有变化才落盘
             ├─ kitty：发 SIGUSR1 热重载
             └─ starship：只更新 [palettes.kde] 段，
                写完向 fish 发 SIGWINCH 强制重绘 prompt
```

**为什么用 `PathModified=` 而不是定时轮询**：KDE 改动是事件驱动的，轮询要么延迟高要么空转。

**为什么 starship 要发 `SIGWINCH`**：Starship 每次画 prompt 都会重读配置，所以新开的终端本来就会用新色 ——
但**已经开着的终端不会**，它会一直停在那儿。发 `SIGWINCH` 是骗 shell 重画一次。

---

## 文件

| 文件 | 作用 |
|---|---|
| `generate-kitty-theme.py` | 读 KDE 配色 → 生成 Kitty 主题（输出到 stdout，或 `--apply`） |
| `generate-starship-palette.py` | 读 KDE 配色 → 生成 Starship 调色板（`--palette-only` 只改 `[palettes.kde]` 段） |
| `auto-sync.sh` | 同步入口：生成 + 幂等比对 + 热重载信号。由 systemd Path 单元触发，也可手动跑 |
| `kde-theme-sync.path` | systemd 用户单元：监听 `kdeglobals` |
| `kde-theme-sync.service` | systemd 用户单元：oneshot 执行 `auto-sync.sh` |

---

## 环境

**验证过的组合**：

```
CachyOS (Arch) ｜ KDE Plasma 6 ｜ Kitty ｜ Starship 1.26.0 ｜ Wayland ｜ Python 3.14
```

只在**这一个组合**上实测过。下面这张表说明哪些地方会因环境而异：

| 项 | 是否通用 | 说明 |
|---|---|---|
| 配置文件路径（`~/.config/…`） | ✅ **通用** | XDG 规范，各发行版一致 |
| KDE 配色存储位置与优先级 | ✅ **通用** | 同上 |
| `kdeglobals` 的**内容** | ⚠️ **因 Plasma 版本而异** | 见下面「竞态防护」一节；给出的是自查方法 |
| Python | ✅ **3.8+** | 只用标准库 |
| `systemd --user` | ⚠️ **需发行版启用** | Arch / Fedora / Ubuntu 默认有；`systemctl --user status` 自查 |
| `PathModified=` 的触发时机 | ✅ **通用** | systemd 行为，与发行版无关 |
| kitty 热重载 `SIGUSR1` | ✅ **通用** | kitty 自身特性 |
| starship 的 `SIGWINCH` 重绘 | ❌ **只对 fish 有效** | bash / zsh 要换做法，见下 |
| 昼夜自动切换 | ⚠️ **需要 Plasma 6.5+** | 低版本没有这个功能 |

### 不用 fish 怎么办

`auto-sync.sh` 里有这一行：

```bash
pkill -WINCH -x fish 2>/dev/null || true
```

它存在的理由是「让已经开着的终端重画 prompt」。换 shell 就换这一行：

| shell | 做法 |
|---|---|
| **fish** | `pkill -WINCH -x fish`（本仓库默认） |
| **bash** | `pkill -WINCH -x bash` —— 但 bash 收到 `SIGWINCH` 不一定重绘 prompt，可能仍需新开终端 |
| **zsh** | 同理；zsh 可用 `zle reset-prompt` 配合 `TRAPWINCH`，但需要额外配置 |

> ⚠️ 注意 `-x`：它要求**进程名精确匹配**。不要用 `pkill -f`，那个模式串会匹配到命令行里
> 任何含该字符串的进程 —— 包括执行它的那个 shell 自己。

---

## 安装

```bash
# 1. 放脚本（路径随意，但 systemd 单元里的路径要跟着改）
mkdir -p ~/.local/share/kde-terminal-theme-sync
cp generate-kitty-theme.py generate-starship-palette.py auto-sync.sh \
   ~/.local/share/kde-terminal-theme-sync/
chmod +x ~/.local/share/kde-terminal-theme-sync/{auto-sync.sh,generate-*.py}

# 2. 先手动跑一次，确认能读出你的配色
~/.local/share/kde-terminal-theme-sync/generate-kitty-theme.py

# 3. 装 systemd 单元
cp kde-theme-sync.path kde-theme-sync.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kde-theme-sync.path

# 4. 看状态
systemctl --user status kde-theme-sync.path
journalctl --user -u kde-theme-sync.service -f
```

`.service` 里默认写的路径是 `%h/.local/share/kde-terminal-theme-sync/auto-sync.sh`
（`%h` 是 systemd 的家目录占位符）。**如果你放在别处，改那一行。**

Kitty 那边还需要一行 `include`，把它接上：

```conf
# ~/.config/kitty/kitty.conf
include colors-kde.conf
```

---

## 竞态防护（重要）

**KDE 写 `kdeglobals` 是分阶段的** —— 不是一次写完。而 `PathModified=` 在**第一次写入事件**就触发，
于是同步可能抢在颜色段落盘之前读到文件。

读到半成品会怎样：解析出「有 `[General]`、无 `[Colors:View]`」，取色函数拿到默认值
`(255,255,255)`，生成一份**看起来正确**的浅色结果。它和磁盘上已有的浅色文件**逐字节相同**，
于是幂等比对判定「无变化」→ 不写盘 → 之后颜色段真正落盘时**不再有事件** → **永远不重试**。

**结果是终端永久僵在浅色，而且日志里一切正常。**

三层防护：

1. **生成前等它写完** —— 连续两次采样摘要一致才继续
2. **只接受完整输入** —— 缺关键颜色段就**退出码 3**，调用方跳过本次，
   **绝不拿默认值凑**（逃生舱 `--allow-missing-colors` 可以退回旧行为，仅供排错）
3. **生成后复核** —— 期间文件又变了就整体重跑，上限 2 次

**设计原则一句话**：宁可这次不更新，也绝不写入错误的颜色。
不更新只是晚一点，写错了会一直错到下次切换。

### 退出码

| 退出码 | 含义 | 调用方行为 |
|---|---|---|
| 0 | 成功（含"无变化"） | 正常 |
| 2 | 参数错误 | 修正调用 |
| 3 | **KDE 配色数据不完整**（半成品，重试后仍缺关键段） | `auto-sync.sh` 跳过本次，等下次变化再触发 |

---

## 一个实现上的坑

判断「必需颜色是否齐全」**必须看原始 `kdeglobals`**，不能在合并后的字典上判断。

因为 `.colors` 基座文件会把缺失的 `[Colors:View]` 补齐 —— 在合并结果上判断，
防护形同虚设，真实缺色的 `kdeglobals` 照样生成浅色主题。

「半成品」的定义就是**原始文件里颜色段还没落盘**，跟合并后的结果没关系。

---

## 备选方案：kitty 自带的 `auto-color-scheme`

Kitty 0.38 起自带这个功能：跑一次 `kitten themes`，把主题存成 light / dark / no-preference
三份 `*.auto.conf`，之后 kitty 自己查询系统配色并切换，**不需要任何外部脚本**。

听起来应该直接换过去，但它有个性质：

> When these files exist, the colors in these files override all other colors,
> **and also all background image settings, even those specified using the
> `kitty --override` command line flag.**

它会**完全压制** `include colors-kde.conf`。也就是说，用了它就等于放弃跟随 KDE 的
accent 与壁纸取色 —— 而那正是本仓库存在的理由。

**什么时候该用它**：你只需要亮/暗跟随，不需要终端配色从壁纸长出来。

---

## 许可证

[MIT](LICENSE)
