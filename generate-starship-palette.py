#!/usr/bin/env python3
"""
从 KDE Plasma 配色方案生成 Starship 调色板（palette）。

Starship 1.20+ 支持在 starship.toml 中定义 `[palettes.<name>]`，
然后模块通过 `fg:name` / `bg:name` 引用命名颜色。

此脚本输出完整的 starship.toml 配置，将用户当前的硬编码色号
替换为从 KDE 配色派生的语义化调色板。

用法：
    python3 generate-starship-palette.py                    # 预览生成到 stdout
    python3 generate-starship-palette.py --apply            # 写入 starship.toml
    python3 generate-starship-palette.py --apply --backup   # 备份后写入
    python3 generate-starship-palette.py --palette-only     # 只更新 [palettes.kde] 段（保留其他配置）

竞态防护（2026-09-19 加入，与 generate-kitty-theme.py 同一套逻辑）：
    KDE 分阶段写 kdeglobals，可能读到「只有 [General]、[Colors:*] 未落盘」的半成品，
    旧行为会静默回退成浅色调色板。现在缺失必需颜色会重试读取，仍缺失则退出码 3。
    退出码：0 成功 / 3 配色数据不完整
"""

import configparser
import os
import sys
import shutil
import time
from pathlib import Path


def rgb_to_hex(r, g, b):
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def adjust_lightness(r, g, b, factor):
    return tuple(min(255, max(0, int(c * factor))) for c in (r, g, b))


def blend(c1, c2, weight):
    """weight: c1 的权重"""
    return tuple(int(c1[i] * weight + c2[i] * (1 - weight)) for i in range(3))


def resolve_scheme_name(kdeglobals=None):
    """
    解析当前 KDE 配色方案名。

    查找顺序：
      1. ~/.config/kdeglobals [General] ColorScheme（传统位置）
      2. ~/.config/kdedefaults/kdeglobals [General] ColorScheme
         （Plasma 6.7 实测：全局主题切换只写这里，kdeglobals 里只剩 ColorSchemeHash）
      3. None —— 由调用方按背景亮度反推
    """
    candidates = [
        (Path.home() / ".config" / "kdeglobals", "~/.config/kdeglobals"),
        (Path.home() / ".config" / "kdedefaults" / "kdeglobals",
         "~/.config/kdedefaults/kdeglobals"),
    ]
    for path, desc in candidates:
        if path.exists():
            if kdeglobals is not None and path == Path.home() / ".config" / "kdeglobals":
                parser = kdeglobals
            else:
                parser = configparser.ConfigParser()
                parser.read(path)
            if parser.has_option("General", "ColorScheme"):
                return parser.get("General", "ColorScheme"), desc
    return None, "(未找到，按背景亮度反推)"


def get_rgb(colors, section, key, default=None):
    section_key = section.lower()
    if section_key in colors:
        raw = colors[section_key].get(key.lower())
        if raw:
            parts = raw.split(",")
            if len(parts) == 3:
                return tuple(int(p.strip()) for p in parts)
    return default


def load_kde_colors():
    """与 Kitty 脚本相同的 KDE 配色加载逻辑（基座文件按真实方案名查找）。"""
    colors = {}
    kdeglobals_path = Path.home() / ".config" / "kdeglobals"
    kdeglobals = configparser.ConfigParser()
    kdeglobals.read(kdeglobals_path)

    scheme_name, _src = resolve_scheme_name(kdeglobals)
    scheme_known = scheme_name is not None
    if not scheme_known:
        scheme_name = "BreezeLight"  # 仅用于基座查找；下面按亮度修正

    search_paths = [
        Path.home() / ".local" / "share" / "color-schemes" / f"{scheme_name}.colors",
        Path("/usr/share/color-schemes") / f"{scheme_name}.colors",
    ]

    base_colors = configparser.ConfigParser()
    for p in search_paths:
        if p.exists():
            base_colors.read(p)
            break

    for section in base_colors.sections():
        sec = section.lower()
        if sec not in colors:
            colors[sec] = {}
        for key, value in base_colors.items(section):
            colors[sec][key.lower()] = value

    for section in kdeglobals.sections():
        if section.lower().startswith("colors") or section.lower().startswith("coloreffects"):
            sec = section.lower()
            if sec not in colors:
                colors[sec] = {}
            for key, value in kdeglobals.items(section):
                colors[sec][key.lower()] = value

    if not scheme_known:
        view_bg = get_rgb(colors, "Colors:View", "BackgroundNormal")
        if view_bg is not None:
            lum = 0.299 * view_bg[0] + 0.587 * view_bg[1] + 0.114 * view_bg[2]
            scheme_name = "BreezeDark" if lum < 128 else "BreezeLight"

    return colors, scheme_name


def missing_required_colors(colors):
    """
    合并后仍缺必需颜色（例如连 .colors 基座也找不到）。
    注意：判断「是否读到 kdeglobals 半成品」必须看原始文件，见 missing_critical()。
    """
    required = [
        ("Colors:View", "BackgroundNormal"),
        ("Colors:View", "ForegroundNormal"),
        ("Colors:View", "DecorationFocus"),
    ]
    return [f"{sec}/{key}" for sec, key in required if get_rgb(colors, sec, key) is None]


def missing_critical():
    """
    判断 ~/.config/kdeglobals 是否处于「写入半成品」状态。

    只看 **原始 kdeglobals**：有 [Colors:View] BackgroundNormal 才算写完。
    若用合并后的字典判断，.colors 基座会把缺失掩盖掉，防护形同虚设。
    """
    kdeglobals = configparser.ConfigParser()
    kdeglobals.read(Path.home() / ".config" / "kdeglobals")
    if not kdeglobals.sections():
        return ["~/.config/kdeglobals 缺失或为空"]
    raw = {}
    for section in kdeglobals.sections():
        if section.lower().startswith("colors") or section.lower().startswith("coloreffects"):
            sec = section.lower()
            raw.setdefault(sec, {})
            for key, value in kdeglobals.items(section):
                raw[sec][key.lower()] = value
    if get_rgb(raw, "Colors:View", "BackgroundNormal") is None:
        return ["kdeglobals[Colors:View]/BackgroundNormal"]
    return []


def load_kde_colors_with_retry(wait=0.6, attempts=3, strict=True):
    """读取 KDE 配色；读到半成品则等待重读，仍不完整则退出码 3。"""
    missing = missing_critical()
    attempt = 1
    while missing and attempt < attempts:
        print(f"[!] kdeglobals 尚未写完（缺 {', '.join(missing)}），"
              f"{wait}s 后重试 ({attempt + 1}/{attempts})", file=sys.stderr)
        time.sleep(wait)
        missing = missing_critical()
        attempt += 1

    if missing:
        msg = (f"KDE 配色数据不完整，缺少: {', '.join(missing)}。"
               f"通常是 kdeglobals 正在被写入的半成品状态。")
        if strict:
            print(f"[✗] {msg} 本次跳过，不更新 Starship palette。", file=sys.stderr)
            sys.exit(3)
        print(f"[!] {msg} 继续使用回退默认值。", file=sys.stderr)

    colors, scheme_name = load_kde_colors()

    # 二次保险：合并后仍缺必需颜色（例如 .colors 也找不到）
    still = missing_required_colors(colors)
    if still:
        msg = f"合并后仍缺少配色键: {', '.join(still)}"
        if strict:
            print(f"[✗] {msg}，本次跳过。", file=sys.stderr)
            sys.exit(3)
        print(f"[!] {msg}，使用回退默认值。", file=sys.stderr)

    return colors, scheme_name


def generate_starship_palette(colors, scheme_name):
    """从 KDE 配色生成 Starship palette 定义。"""

    # ── 核心颜色提取 ──
    view_bg = get_rgb(colors, "Colors:View", "BackgroundNormal", (255, 255, 255))
    view_bg_alt = get_rgb(colors, "Colors:View", "BackgroundAlternate",
                          adjust_lightness(*view_bg, 0.95))
    view_fg = get_rgb(colors, "Colors:View", "ForegroundNormal", (35, 38, 41))
    window_bg = get_rgb(colors, "Colors:Window", "BackgroundNormal", (239, 240, 241))
    header_bg = get_rgb(colors, "Colors:Header", "BackgroundNormal", (222, 224, 226))
    btn_bg = get_rgb(colors, "Colors:Button", "BackgroundNormal", (252, 252, 252))

    # Accent
    accent = get_rgb(colors, "Colors:View", "DecorationFocus", (61, 174, 233))

    # 互补配色（暗色变体）
    comp_bg = get_rgb(colors, "Colors:Complementary", "BackgroundNormal", (42, 46, 50))
    comp_fg = get_rgb(colors, "Colors:Complementary", "ForegroundNormal", (252, 252, 252))

    # 语义色
    neg_fg = get_rgb(colors, "Colors:View", "ForegroundNegative", (218, 68, 83))
    pos_fg = get_rgb(colors, "Colors:View", "ForegroundPositive", (39, 174, 96))
    neutral_fg = get_rgb(colors, "Colors:View", "ForegroundNeutral", (246, 116, 0))
    link_fg = get_rgb(colors, "Colors:View", "ForegroundLink", (41, 128, 185))
    visited_fg = get_rgb(colors, "Colors:View", "ForegroundVisited", (155, 89, 182))
    inactive_fg = get_rgb(colors, "Colors:View", "ForegroundInactive", (112, 125, 138))

    # 选择区
    sel_bg = get_rgb(colors, "Colors:Selection", "BackgroundNormal", (61, 174, 233))
    sel_fg = get_rgb(colors, "Colors:Selection", "ForegroundNormal", (255, 255, 255))

    # ── 判断明暗 ──
    bg_luminance = 0.299 * view_bg[0] + 0.587 * view_bg[1] + 0.114 * view_bg[2]
    is_dark = bg_luminance < 128

    # ── Starship 配色层次 ──
    # Starship prompt 中的颜色分几个层次：
    #   bg_deep   — 最深色（Complementary 暗背景）→ 用于深色文字
    #   bg_surface — 主面层（Window 背景色）→ 用于面板/卡片底色
    #   bg_high   — 高亮层（Header/Button 背景色）→ 用于 git branch / hostname 条
    #   bg        — 最浅层（View 背景色）→ 终端背景色
    #
    #   fg        — 主文字色（View ForegroundNormal）
    #   fg_muted  — 次要文字色（View ForegroundInactive）
    #
    #   accent    — 强调色（DecorationFocus）
    #   link      — 链接色
    #   positive  — 成功色
    #   negative  — 失败色
    #   warning   — 警告色
    #   info      — 信息色

    # 淡化版 accent（与背景混合得到浅色变体）
    accent_soft = blend(accent, view_bg, 0.55)   # ~55% accent + 45% 背景
    accent_dim  = blend(accent, view_bg, 0.25)   # ~25% accent + 75% 背景

    if is_dark:
        palette = {
            "bg_deep":      rgb_to_hex(*adjust_lightness(*comp_bg, 0.8)),
            "bg_surface":   rgb_to_hex(*comp_bg),
            "bg_high":      rgb_to_hex(*window_bg),
            "bg":           rgb_to_hex(*view_bg),
            "fg":           rgb_to_hex(*comp_fg),
            "fg_muted":     rgb_to_hex(*inactive_fg),
            "accent":       rgb_to_hex(*accent),
            "accent_soft":  rgb_to_hex(*accent_soft),
            "accent_dim":   rgb_to_hex(*accent_dim),
            "link":         rgb_to_hex(*link_fg),
            "positive":     rgb_to_hex(*pos_fg),
            "negative":     rgb_to_hex(*neg_fg),
            "warning":      rgb_to_hex(*neutral_fg),
            "info":         rgb_to_hex(*visited_fg),
            "selection_bg": rgb_to_hex(*sel_bg),
            "selection_fg": rgb_to_hex(*sel_fg),
        }
    else:
        palette = {
            "bg_deep":      rgb_to_hex(*view_fg),
            "bg_surface":   rgb_to_hex(*window_bg),
            "bg_high":      rgb_to_hex(*header_bg),
            "bg":           rgb_to_hex(*view_bg_alt),
            "fg":           rgb_to_hex(*view_fg),
            "fg_muted":     rgb_to_hex(*inactive_fg),
            "accent":       rgb_to_hex(*accent),
            "accent_soft":  rgb_to_hex(*accent_soft),
            "accent_dim":   rgb_to_hex(*accent_dim),
            "link":         rgb_to_hex(*link_fg),
            "positive":     rgb_to_hex(*pos_fg),
            "negative":     rgb_to_hex(*neg_fg),
            "warning":      rgb_to_hex(*neutral_fg),
            "info":         rgb_to_hex(*visited_fg),
            "selection_bg": rgb_to_hex(*sel_bg),
            "selection_fg": rgb_to_hex(*sel_fg),
        }

    return palette, is_dark


def render_palette_section(palette, palette_name="kde"):
    """渲染 [palettes.<name>] 段的行列表（不含结尾空行）。"""
    lines = [f"[palettes.{palette_name}]"]
    for name, hex_val in palette.items():
        lines.append(f'{name} = "{hex_val}"')
    return lines


def report_file_error(action, path, error):
    """将文件操作错误转换为面向用户的可操作提示。"""
    if isinstance(error, PermissionError):
        print(f"[✗] {action}失败：没有权限访问 {path}。", file=sys.stderr)
        print("[*] 请确认目标文件及其父目录归当前用户所有且可写后重试。",
              file=sys.stderr)
    else:
        print(f"[✗] {action}失败：{path}: {error}", file=sys.stderr)
    return 1


def generate_starship_config(palette, is_dark):
    """生成完整的 starship.toml，保留用户当前的模块配置但使用调色板颜色。"""

    # ── 调色板名称 ──
    palette_name = "kde"

    lines = []
    lines.append("# Starship 配置 — 从 KDE Plasma 配色自动生成")
    lines.append(f"# 主题类型: {'dark' if is_dark else 'light'}")
    lines.append("# 运行 generate-starship-palette.py --apply 以更新")
    lines.append("")

    # ── 根级设置（必须在 [palettes.xxx] 之前） ──
    lines.append(f'palette = "{palette_name}"')
    lines.append("add_newline = false")
    lines.append("")
    lines.append("# ── 提示符布局 ──")
    lines.append('format = """')
    lines.append('$cmd_duration $directory$git_branch')
    lines.append('  $character"""')
    lines.append("")

    # ── Palette 定义 ──
    lines.extend(render_palette_section(palette, palette_name))
    lines.append("")

    # ── 胶囊配色层次 ──
    #   accent      — 最重要的元素 (git 分支)
    #   accent_soft — 次要元素 (主机名、用户名)
    #   accent_dim  — 辅助元素 (命令耗时)
    #   bg_high     — 基础元素 (目录, 最常出现, 保持低调)
    #   fg          — 胶囊上的深色文字
    #   fg_muted    — 非活跃元素

    lines.append("# ── Git 分支（accent — 主强调色胶囊）──")
    lines.append("[git_branch]")
    lines.append("style = \"bg:accent\"")
    lines.append('symbol = "󰘬"')
    lines.append("truncation_length = 12")
    lines.append('truncation_symbol = ""')
    lines.append('format = " 󰜥 [](bold fg:accent)[$symbol $branch(:$remote_branch)](fg:bg bg:accent)[ ](bold fg:accent)"')
    lines.append("")

    lines.append("# ── Git 状态 ──")
    lines.append("[git_status]")
    lines.append('conflicted = " 🏳 "')
    lines.append('ahead = " 🏎💨 "')
    lines.append('behind = " 😰 "')
    lines.append('diverged = " 😵 "')
    lines.append('untracked = " 🤷 ‍"')
    lines.append('stashed = " 📦 "')
    lines.append('modified = " 📝 "')
    lines.append('staged = \'[++\\($count\\)](green)\'')
    lines.append('renamed = " ✍️ "')
    lines.append('deleted = " 🗑 "')
    lines.append("")

    lines.append("# ── 主机名（accent_soft — 次级强调胶囊）──")
    lines.append("[hostname]")
    lines.append("ssh_only = false")
    lines.append('format = "[•$hostname](bg:accent_soft bold fg:fg)[](bold fg:accent_soft)"')
    lines.append('trim_at = ".companyname.com"')
    lines.append("disabled = false")
    lines.append("")

    lines.append("# ── 用户名（accent_soft — 次级强调胶囊）──")
    lines.append("[username]")
    lines.append("style_user = \"bold bg:accent_soft fg:fg\"")
    lines.append("style_root = \"red bold\"")
    lines.append('format = "[](bold fg:accent_soft)[$user]($style)"')
    lines.append("disabled = false")
    lines.append("show_always = true")
    lines.append("")

    lines.append("# ── 目录（accent_dim — 辅助色胶囊）──")
    lines.append("[directory]")
    lines.append('home_symbol = " "')
    lines.append('read_only = "  "')
    lines.append("style = \"bg:accent_dim fg:fg\"")
    lines.append("truncation_length = 2")
    lines.append('truncation_symbol = ".../"')
    lines.append('format = \'[](bold fg:accent_dim)[󰉋 → $path]($style)[](bold fg:accent_dim)\'')
    lines.append("")
    lines.append("[directory.substitutions]")
    lines.append('"Desktop" = "  "')
    lines.append('"Documents" = "  "')
    lines.append('"Downloads" = "  "')
    lines.append('"Music" = " 󰎈 "')
    lines.append('"Pictures" = "  "')
    lines.append('"Videos" = "  "')
    lines.append('"GitHub" = " 󰊤 "')
    lines.append("")

    lines.append("# ── 命令耗时（accent_dim — 辅助色胶囊）──")
    lines.append("[cmd_duration]")
    lines.append("min_time = 0")
    lines.append('format = \'[](bold fg:accent_dim)[󰪢 $duration](bold bg:accent_dim fg:fg)[](bold fg:accent_dim)\'')
    lines.append("")

    lines.append("# ── 填充符号 ──")
    lines.append("[fill]")
    lines.append('symbol = "-"')
    lines.append("style = \"fg:fg_muted\"")
    lines.append("")

    lines.append("# ── 光标字符 ──")
    lines.append("[character]")
    lines.append('success_symbol = "[ ](bold fg:accent)"')
    lines.append('error_symbol = "[ ](bold fg:negative)"')
    lines.append("")

    lines.append("# ── 禁用模块 ──")
    lines.append("[package]")
    lines.append("disabled = true")
    lines.append("")
    lines.append("[line_break]")
    lines.append("disabled = false")
    lines.append("")
    lines.append("[memory_usage]")
    lines.append("disabled = true")
    lines.append("threshold = -1")
    lines.append('symbol = " "')
    lines.append("style = \"bold dimmed green\"")
    lines.append("")
    lines.append("[time]")
    lines.append("disabled = true")
    lines.append('format = \'🕙[\\[ $time \\]]($style) \'')
    lines.append('time_format = "%T"')
    lines.append("")
    lines.append("[git_commit]")
    lines.append("commit_hash_length = 4")
    lines.append('tag_symbol = " "')
    lines.append("")
    lines.append("[git_state]")
    lines.append('format = \'[\\($state( $progress_current of $progress_total)\\)]($style) \'')
    lines.append('cherry_pick = "[🍒 PICKING](bold red)"')

    return "\n".join(lines) + "\n"


def update_palette_only(palette):
    """
    只更新 ~/.config/starship.toml 的 [palettes.kde] 段，其余内容原样保留。
    段不存在则追加；顶层 palette 键缺失时自动补 'palette = "kde"'。
    内容无变化则不写盘（幂等，供自动同步循环调用）。
    """
    path = Path.home() / ".config" / "starship.toml"
    if not path.exists():
        print(f"[!] 不存在 {path}，先运行 --apply 生成完整配置", file=sys.stderr)
        return 1

    palette_name = "kde"
    new_block = render_palette_section(palette, palette_name) + [""]

    try:
        with open(path) as f:
            original = f.read()
    except OSError as error:
        return report_file_error("读取 Starship 配置", path, error)
    lines = original.splitlines()

    # 顶层 palette 键检查
    palette_ok = False
    insert_at = len(lines)
    for i, ln in enumerate(lines):
        if ln.startswith("["):
            if not palette_ok:
                insert_at = i  # 第一个 [section] 之前补顶层键
            break
        if not palette_ok and ln.startswith("palette") and "=" in ln:
            val = ln.split("=", 1)[1].strip().strip('"').strip("'")
            if val == palette_name:
                palette_ok = True
            else:
                print(f"[!] 顶层 palette={val!r} 不是 {palette_name!r}，跳过顶层键处理",
                      file=sys.stderr)
                palette_ok = True
    if not palette_ok:
        lines.insert(insert_at, f'palette = "{palette_name}"')

    # 定位现有 [palettes.kde] 段
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(f"[palettes.{palette_name}]"):
            start = i
            break

    if start is None:
        # 追加到文件末尾（保留一个空行分隔）
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(new_block)
    else:
        # 找到段结束：段内数据行之后的第一个 [section] 或注释行
        # （注释被视为下一个 section 的说明，不能吞掉）
        end = len(lines)
        saw_content = False
        for i in range(start + 1, len(lines)):
            s = lines[i].lstrip()
            if not s:
                continue
            if s.startswith("["):
                end = i
                break
            if s.startswith("#"):
                if saw_content:
                    end = i
                    break
                continue
            saw_content = True
        lines[start:end] = new_block

    new_text = "\n".join(lines).rstrip() + "\n"
    if new_text == original:
        print("[*] palette 无变化，跳过写入", file=sys.stderr)
        return 0

    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        try:
            shutil.copy2(path, backup_path)
        except OSError as error:
            return report_file_error("备份 Starship 配置", backup_path, error)
        print(f"[*] 首次备份到: {backup_path}", file=sys.stderr)
    try:
        with open(path, "w") as f:
            f.write(new_text)
    except OSError as error:
        return report_file_error("写入 Starship 配置", path, error)
    print(f"[✓] palette 段已更新: {path}", file=sys.stderr)
    return 0


def main():
    output_path = None
    apply_flag = False
    backup_flag = False
    palette_only_flag = False
    strict = True
    wait = 0.6
    attempts = 3

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--apply":
            apply_flag = True
        elif args[i] == "--backup":
            backup_flag = True
        elif args[i] == "--palette-only":
            palette_only_flag = True
        elif args[i] == "--allow-missing-colors":
            strict = False
        elif args[i] == "--wait":
            i += 1
            wait = float(args[i])
        elif args[i] == "--attempts":
            i += 1
            attempts = max(1, int(args[i]))
        elif args[i] == "--help" or args[i] == "-h":
            print(__doc__)
            sys.exit(0)
        i += 1

    colors, scheme_name = load_kde_colors_with_retry(wait=wait, attempts=attempts, strict=strict)
    palette, is_dark = generate_starship_palette(colors, scheme_name)

    if palette_only_flag:
        if apply_flag or backup_flag:
            print("[!] --palette-only 已隐含写入 ~/.config/starship.toml，忽略 --apply/--backup",
                  file=sys.stderr)
        sys.exit(update_palette_only(palette))

    if apply_flag:
        output_path = os.path.expanduser("~/.config/starship.toml")

    config = generate_starship_config(palette, is_dark)

    if output_path:
        try:
            if backup_flag and os.path.exists(output_path):
                backup_path = output_path + ".bak"
                shutil.copy2(output_path, backup_path)
                print(f"[*] 已备份到: {backup_path}", file=sys.stderr)

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w") as f:
                f.write(config)
        except OSError as error:
            sys.exit(report_file_error("写入或备份 Starship 配置", output_path, error))
        print(f"[✓] 已写入: {output_path}", file=sys.stderr)
        print(f"[*] 重新打开终端或执行 'exec fish' 查看效果", file=sys.stderr)
    else:
        print(config)


if __name__ == "__main__":
    main()
