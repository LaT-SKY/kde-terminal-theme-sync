#!/usr/bin/env bash
# KDE 配色变化 → 同步 Kitty/Starship（由 kde-theme-sync.path 触发，也可手动运行）
# 幂等：内容无变化则不写盘、不发信号
#
# 2026-09-19 竞态修复：
#   KDE 分阶段写 kdeglobals（先 General/ColorSchemeHash，颜色段随后落盘），
#   而 systemd PathModified 在写入瞬间就触发，旧实现会读到半成品 →
#   生成浅色主题 → 与已有文件逐字节相同 → 判定“无变化” → 永不重试，
#   结果切到暗色模式后 kitty 背景仍是浅色（starship 因稍后读取而正常）。
#   现在：生成前等内容稳定，生成后复核，若期间又变则整体重跑；且生成器
#   遇到必需颜色缺失会退出码 3、本脚本跳过本次同步而不是写入浅色垃圾。
set -euo pipefail

exec 9>/tmp/kde-theme-sync.lock
flock -n 9 || exit 0  # 已有实例在跑则直接退出，防并发

# 脚本自己所在目录 —— **不写死路径**，clone 到哪都能跑。
# （原版写死了作者的私人路径，发布前必须去掉这类东西。）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KDEGLOBALS="$HOME/.config/kdeglobals"
KITTY_CONF="$HOME/.config/kitty/colors-kde.conf"

REDO_COUNT=0    # 本次同步内允许的“整体重跑”次数
MAX_REDO=2
SETTLE_WAIT=0.4 # 稳定采样间隔
SETTLE_TRIES=12 # 最多等 12×0.4s ≈ 4.8s

# 内容摘要：作为“kdeglobals 是否仍在变”的判据
file_sig() { md5sum "$1" 2>/dev/null | cut -d' ' -f1; }

# 等 kdeglobals 写完：连续两次采样摘要一致才继续（KDE 分阶段写盘）
settle_kdeglobals() {
    local prev="" cur i
    for ((i = 0; i < SETTLE_TRIES; i++)); do
        cur="$(file_sig "$KDEGLOBALS")"
        [[ -n "$cur" && "$cur" == "$prev" ]] && return 0
        prev="$cur"
        sleep "$SETTLE_WAIT"
    done
    echo "[$(date '+%F %T')] 警告：kdeglobals 在 $SETTLE_TRIES 次采样内未稳定，继续尝试" >&2
    return 0
}

# 返回 0 表示“内容在同步期间又变了，需要重跑”，1 表示稳定
kdeglobals_changed_since() {
    [[ "$(file_sig "$KDEGLOBALS")" != "$1" ]]
}

# ── 生成前：等 KDE 把 kdeglobals 写完 ──
settle_kdeglobals

while :; do
    SIG_BEFORE="$(file_sig "$KDEGLOBALS")"

    TMP_K="$(mktemp)"
    if ! python3 "$DIR/generate-kitty-theme.py" -o "$TMP_K"; then
        # 退出码 3 = kdeglobals 半成品（生成器已自行重试过）；其余为真实错误
        echo "[$(date '+%F %T')] kitty 配色生成失败（kdeglobals 尚未写完或数据异常），本次跳过" >&2
        rm -f "$TMP_K"
        exit 0
    fi

    # 1) Kitty：生成到临时文件比对，仅内容变化时落盘并 SIGUSR1 热重载
    if ! cmp -s "$TMP_K" "$KITTY_CONF"; then
        cp "$TMP_K" "$KITTY_CONF"
        pkill -USR1 -x kitty 2>/dev/null || true
        echo "[$(date '+%F %T')] kitty colors-kde.conf 已更新并发送 SIGUSR1"
    else
        echo "[$(date '+%F %T')] kitty 配色无变化"
    fi
    rm -f "$TMP_K"

    # 2) Starship：只更新 [palettes.kde] 段（脚本内部自带幂等）
    OUT="$(python3 "$DIR/generate-starship-palette.py" --palette-only 2>&1 || true)"
    if echo "$OUT" | grep -q "palette 段已更新"; then
        # 真正写入了新 palette → 通知所有 fish 重绘 prompt（等效窗口 resize；
        # SIGWINCH 默认忽略、无杀伤。fish 重绘时 starship 重读配置即换色，
        # 解决 transience 下已开终端 prompt 不刷新问题）
        pkill -WINCH -x fish 2>/dev/null || true
        echo "[$(date '+%F %T')] starship palette 已更新，已通知 fish 重绘"
    elif echo "$OUT" | grep -q "palette 无变化"; then
        echo "[$(date '+%F %T')] starship palette 无变化"
    else
        echo "[$(date '+%F %T')] starship 更新异常：$OUT" >&2
    fi

    # ── 生成后复核：若 kdeglobals 在本次同步期间又被改写（说明读到的可能仍是半成品），整体重跑 ──
    if kdeglobals_changed_since "$SIG_BEFORE"; then
        if ((REDO_COUNT < MAX_REDO)); then
            REDO_COUNT=$((REDO_COUNT + 1))
            echo "[$(date '+%F %T')] 检测到 kdeglobals 在同步期间再次变化，重跑同步（第 $REDO_COUNT 次）"
            settle_kdeglobals
            continue
        fi
        echo "[$(date '+%F %T')] 警告：kdeglobals 在 $MAX_REDO 次重跑后仍在变化，放弃本次同步" >&2
    fi
    break
done
