#!/usr/bin/env python3
"""
从 KDE Plasma 配色方案生成 Kitty 终端主题文件。

数据来源优先级：
1. ~/.config/kdeglobals（用户自定义覆盖，最准确）
2. ~/.local/share/color-schemes/<ColorScheme>.colors
3. /usr/share/color-schemes/<ColorScheme>.colors（系统默认）

用法：
    python3 generate-kitty-theme.py                    # 生成到 stdout
    python3 generate-kitty-theme.py --output ~/.config/kitty/colors-kde.conf
    python3 generate-kitty-theme.py --apply            # 生成并直接输出到 Kitty 配色目录

竞态防护（2026-09-19 加入）：
    KDE 分阶段写 kdeglobals，可能在某个瞬间读到「只有 [General]、[Colors:*] 还没落盘」
    的半成品。旧行为会静默回退到浅色默认值，产出错误结果。现在：
      · 必需颜色缺失时按 --wait/--attempts 重试读取；
      · 仍然缺失则非 0 退出（退出码 3），由调用方跳过本次同步。
    退出码：0 成功 / 2 参数错误 / 3 配色数据不完整
"""

import configparser
import os
import sys
import time
from pathlib import Path


def rgb_to_hex(r, g, b):
    """RGB(0-255) => #rrggbb"""
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def hex_to_rgb(hex_str):
    """#rrggbb => (r, g, b)"""
    h = hex_str.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def adjust_lightness(r, g, b, factor):
    """
    调整颜色亮度。
    factor > 1.0 = 变亮
    factor < 1.0 = 变暗
    factor = 1.0 = 不变
    """
    return tuple(min(255, max(0, int(c * factor))) for c in (r, g, b))


def blend(c1, c2, weight):
    """
    混合两个 RGB 颜色。
    weight 表示 c1 的权重（0.0 = 完全是 c2，1.0 = 完全是 c1）。
    """
    return tuple(int(c1[i] * weight + c2[i] * (1 - weight)) for i in range(3))


def resolve_scheme_name(kdeglobals=None):
    """
    解析当前 KDE 配色方案名。

    查找顺序：
      1. ~/.config/kdeglobals [General] ColorScheme（传统位置）
      2. ~/.config/kdedefaults/kdeglobals [General] ColorScheme
         （Plasma 6.7 实测：全局主题切换只写这里，kdeglobals 里只剩 ColorSchemeHash）
      3. None —— 交给调用方按背景亮度反推 BreezeDark / BreezeLight

    返回 (scheme_name, source_desc)。scheme_name 可能为 None。
    """
    candidates = [
        (Path.home() / ".config" / "kdeglobals", "~/.config/kdeglobals"),
        (Path.home() / ".config" / "kdedefaults" / "kdeglobals",
         "~/.config/kdedefaults/kdeglobals"),
    ]
    for path, desc in candidates:
        if path.exists():
            parser = kdeglobals if (kdeglobals is not None
                                    and path == Path.home() / ".config" / "kdeglobals") \
                else configparser.ConfigParser()
            if parser is not kdeglobals:
                parser.read(path)
            if parser.has_option("General", "ColorScheme"):
                return parser.get("General", "ColorScheme"), desc
    return None, "(未找到，按背景亮度反推)"


def guess_scheme_name(view_bg):
    """配色方案名缺失时，按 View 背景亮度反推，避免注释与实际不符。"""
    if view_bg is None:
        return None
    luminance = 0.299 * view_bg[0] + 0.587 * view_bg[1] + 0.114 * view_bg[2]
    return "BreezeDark" if luminance < 128 else "BreezeLight"


def read_raw_kdeglobals():
    """读取 ~/.config/kdeglobals 原始内容（未与 .colors 基座合并）。"""
    kdeglobals = configparser.ConfigParser()
    kdeglobals.read(Path.home() / ".config" / "kdeglobals")
    return kdeglobals


def _load_colors_from_parsers(kdeglobals, base_parser, base_label, scheme_name):
    """把 .colors 基座与 kdeglobals 合并成统一小写字典（kdeglobals 优先）。"""
    colors = {}
    if base_parser is not None:
        for section in base_parser.sections():
            sec = section.lower()
            colors.setdefault(sec, {})
            for key, value in base_parser.items(section):
                colors[sec][key.lower()] = value
    for section in kdeglobals.sections():
        if section.lower().startswith("colors") or section.lower().startswith("coloreffects"):
            sec = section.lower()
            colors.setdefault(sec, {})
            for key, value in kdeglobals.items(section):
                colors[sec][key.lower()] = value  # 逐 key 覆盖
    return colors


def load_kde_colors():
    """
    加载 KDE 配色：先从 .colors 文件读取基础，再用 kdeglobals 覆盖。

    配色基座文件用「解析出来的真实方案名」查找，而不是硬编码 BreezeLight，
    否则暗色方案下会拿浅色基座去补齐 kdeglobals 未覆盖的段落。
    """
    kdeglobals = read_raw_kdeglobals()
    if not (Path.home() / ".config" / "kdeglobals").exists():
        print("警告: ~/.config/kdeglobals 不存在", file=sys.stderr)

    scheme_name, source_desc = resolve_scheme_name(kdeglobals)
    scheme_name_known = scheme_name is not None
    if not scheme_name_known:
        scheme_name = "BreezeLight"  # 仅用于基座查找；下面按亮度修正
    else:
        print(f"[*] 当前 KDE 配色方案: {scheme_name}  (来源: {source_desc})", file=sys.stderr)

    base_parser = None
    base_label = None
    for p in (Path.home() / ".local" / "share" / "color-schemes" / f"{scheme_name}.colors",
              Path("/usr/share/color-schemes") / f"{scheme_name}.colors"):
        if p.exists():
            base_parser = configparser.ConfigParser()
            base_parser.read(p)
            base_label = str(p)
            print(f"[*] 读取基础配色文件: {p}", file=sys.stderr)
            break
    else:
        print(f"[!] 未找到 {scheme_name}.colors，仅使用 kdeglobals", file=sys.stderr)

    colors = _load_colors_from_parsers(kdeglobals, base_parser, base_label, scheme_name)

    # 方案名未知时按实际背景亮度修正（修掉注释里方案名与色彩不符的问题）
    if not scheme_name_known:
        view_bg = get_rgb(colors, "Colors:View", "BackgroundNormal")
        guessed = guess_scheme_name(view_bg)
        if guessed:
            scheme_name = guessed
            print(f"[*] 配色方案名缺失，按背景亮度反推为: {scheme_name}", file=sys.stderr)

    return colors, scheme_name


def kdeglobals_view_complete(kdeglobals):
    """
    判断 kdeglobals 里的颜色段是否已完整落盘。

    依据：KDE 写完时会在 kdeglobals 写出完整 [Colors:View]（含 BackgroundNormal）。
    写入中途只会看到 [General]（或颜色段残缺），此时必须等待重读 ——
    否则会拿 .colors 基座或默认值凑出错误主题（2026-09-19 bug 根因）。
    """
    return get_rgb(_raw_colors(kdeglobals), "Colors:View", "BackgroundNormal") is not None


def _raw_colors(kdeglobals):
    """仅取 kdeglobals 自身的 [Colors:*] 段，不含 .colors 基座。"""
    colors = {}
    for section in kdeglobals.sections():
        if section.lower().startswith("colors") or section.lower().startswith("coloreffects"):
            sec = section.lower()
            colors.setdefault(sec, {})
            for key, value in kdeglobals.items(section):
                colors[sec][key.lower()] = value
    return colors


def get_rgb(colors, section, key, default=None):
    """从 colors 字典获取 RGB 元组"""
    section_key = section.lower()
    if section_key in colors:
        raw = colors[section_key].get(key.lower())
        if raw:
            parts = raw.split(",")
            if len(parts) == 3:
                return tuple(int(p.strip()) for p in parts)
    return default


def missing_required_colors(colors):
    """
    检查生成主题所必需的 KDE 颜色是否齐全。

    注意：这里只看 **合并后的** 字典，用来兜住「连 .colors 基座都补不齐」的情况。
    判断「是否读到了 kdeglobals 半成品」必须看原始文件，见 missing_critical()。
    """
    required = [
        ("Colors:View", "BackgroundNormal"),
        ("Colors:View", "ForegroundNormal"),
        ("Colors:View", "DecorationFocus"),
    ]
    missing = [f"{sec}/{key}" for sec, key in required if get_rgb(colors, sec, key) is None]
    return missing


def missing_critical():
    """
    判断当前 ~/.config/kdeglobals 是否处于「写入半成品」状态。

    判定依据是 **原始 kdeglobals**（未与 .colors 基座合并）里有没有
    [Colors:View] BackgroundNormal：
      · 有  → KDE 已写完，可以安全生成；
      · 无  → 只有 [General] 之类先落盘，颜色段还没写，必须等待重读。
    这里若误用合并后的字典，基座文件会把缺失掩盖掉，等于没防护。
    """
    kdeglobals = read_raw_kdeglobals()
    if not kdeglobals.sections():
        return ["~/.config/kdeglobals 缺失或为空"]
    if not kdeglobals_view_complete(kdeglobals):
        return ["kdeglobals[Colors:View]/BackgroundNormal"]
    return []


def load_kde_colors_with_retry(wait=0.6, attempts=3, strict=True):
    """
    读取 KDE 配色；若读到 kdeglobals 半成品则等待后重读。

    strict=True 且重试用尽仍不完整：退出码 3，由调用方跳过本次同步
    （宁可不更新，也不能把深色主题静默写回浅色）。
    """
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
            print(f"[✗] {msg} 本次跳过，不更新 Kitty 配色。", file=sys.stderr)
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


def generate_kitty_theme(colors, scheme_name):
    """根据 KDE 配色生成 Kitty 主题"""

    # ── 核心颜色提取 ──
    # 视图区（主要内容区域）
    view_bg = get_rgb(colors, "Colors:View", "BackgroundNormal", (255, 255, 255))
    view_fg = get_rgb(colors, "Colors:View", "ForegroundNormal", (35, 38, 41))
    view_bg_alt = get_rgb(colors, "Colors:View", "BackgroundAlternate",
                          adjust_lightness(*view_bg, 0.95))

    # 窗口背景
    window_bg = get_rgb(colors, "Colors:Window", "BackgroundNormal", (239, 240, 241))

    # 选择区
    sel_bg = get_rgb(colors, "Colors:Selection", "BackgroundNormal", (61, 174, 233))
    sel_fg = get_rgb(colors, "Colors:Selection", "ForegroundNormal", (255, 255, 255))

    # Accent / 装饰色
    accent = get_rgb(colors, "Colors:View", "DecorationFocus", (61, 174, 233))

    # ── 判断主题明暗 ──
    bg_luminance = 0.299 * view_bg[0] + 0.587 * view_bg[1] + 0.114 * view_bg[2]
    is_dark = bg_luminance < 128
    theme_type = "dark" if is_dark else "light"

    # ── 背景掺入主色调 ──
    # 纯白/纯黑背景缺乏温度。按低比例混入 accent 色，
    # 亮色主题 5%，暗色主题 3%。
    tint_factor = 0.05 if not is_dark else 0.03
    tinted_bg = blend(accent, view_bg, tint_factor)

    # 互补配色（通常是暗色变体——对亮色主题而言）
    comp_bg = get_rgb(colors, "Colors:Complementary", "BackgroundNormal", (42, 46, 50))
    comp_fg = get_rgb(colors, "Colors:Complementary", "ForegroundNormal", (252, 252, 252))

    # 标题栏
    header_bg = get_rgb(colors, "Colors:Header", "BackgroundNormal", (222, 224, 226))
    header_fg = get_rgb(colors, "Colors:Header", "ForegroundNormal", (35, 38, 41))

    # 按钮
    btn_bg = get_rgb(colors, "Colors:Button", "BackgroundNormal", (252, 252, 252))

    # 语义色
    neg_fg = get_rgb(colors, "Colors:View", "ForegroundNegative", (218, 68, 83))
    pos_fg = get_rgb(colors, "Colors:View", "ForegroundPositive", (39, 174, 96))
    neutral_fg = get_rgb(colors, "Colors:View", "ForegroundNeutral", (246, 116, 0))
    link_fg = get_rgb(colors, "Colors:View", "ForegroundLink", (41, 128, 185))
    visited_fg = get_rgb(colors, "Colors:View", "ForegroundVisited", (155, 89, 182))
    inactive_fg = get_rgb(colors, "Colors:View", "ForegroundInactive", (112, 125, 138))

    print(f"[*] 检测到 {'暗色' if is_dark else '亮色'} 主题 (背景亮度={bg_luminance:.0f})", file=sys.stderr)

    # ── ANSI 蓝色：优先从基础主题中找真正的蓝色 ──
    # KDE 的 ForegroundLink 可能已被用户覆盖成非蓝色的 accent 色，
    # 但 ANSI 蓝色槽位需要保持蓝色，否则终端程序会困惑。
    # 策略：检查 link_fg 是否接近蓝色（色相 180°-260°），
    # 如果不是，从 Complementary/Header 区域寻找蓝色，或使用 Breeze 默认蓝。
    def is_bluish(r, g, b):
        """简单判断 RGB 是否偏蓝"""
        return b > r and b > g * 0.85

    ansi_blue = None
    if is_bluish(*link_fg):
        ansi_blue = link_fg
    else:
        # 尝试从 Complementary 的 link 取蓝色
        comp_link = get_rgb(colors, "Colors:Complementary", "ForegroundLink")
        if comp_link and is_bluish(*comp_link):
            ansi_blue = comp_link
        else:
            # 回退到标准 Breeze 蓝
            ansi_blue = (41, 128, 185)  # BreezeLight 默认 blue link

    # ── ANSI 16 色构建 ──
    if is_dark:
        color0 = adjust_lightness(*comp_bg, 0.85)
        color7 = comp_fg
        color8 = adjust_lightness(*comp_fg, 0.5)
        color15 = adjust_lightness(*comp_fg, 1.15)
    else:
        color0 = comp_bg
        color7 = view_bg_alt
        color8 = inactive_fg
        color15 = view_bg

    color1 = neg_fg                                       # 红色
    color2 = pos_fg                                       # 绿色
    color3 = neutral_fg                                  # 黄色/橙色
    color4 = ansi_blue                                    # 蓝色（真正的蓝色）
    color5 = visited_fg                                   # 洋红/紫

    # 青色：从蓝色推导（调整绿分量使其偏青）
    color6 = (min(255, int(ansi_blue[0] * 0.7)),
              min(255, int(ansi_blue[1] * 1.15)),
              min(255, int(ansi_blue[2] * 1.0)))         # 青

    # 亮色变体
    color9 = adjust_lightness(*color1, 1.25)
    color10 = adjust_lightness(*color2, 1.25)
    color11 = adjust_lightness(*color3, 1.25)
    color12 = adjust_lightness(*color4, 1.25)
    color13 = adjust_lightness(*color5, 1.25)
    color14 = adjust_lightness(*color6, 1.25)

    # ── 生成配置文件 ──
    lines = []
    lines.append(f"# Kitty 主题 - 从 KDE 配色方案 '{scheme_name}' 生成")
    lines.append(f"# 主题类型: {theme_type}")
    lines.append(f"# 自动生成 — 不要手动编辑")
    lines.append("")
    lines.append("# ── 前景 / 背景 ──")
    lines.append(f"foreground  {rgb_to_hex(*view_fg)}")
    lines.append(f"background  {rgb_to_hex(*tinted_bg)}")
    lines.append("")
    lines.append("# ── 光标 ──")
    lines.append(f"cursor       {rgb_to_hex(*accent)}")
    lines.append(f"cursor_text_color {rgb_to_hex(*view_bg)}")
    lines.append("")
    lines.append("# ── 选择区 ──")
    lines.append(f"selection_foreground {rgb_to_hex(*sel_fg)}")
    lines.append(f"selection_background {rgb_to_hex(*sel_bg)}")
    lines.append("")
    lines.append("# ── URL / 链接 ──")
    lines.append(f"url_color    {rgb_to_hex(*link_fg)}")
    lines.append("")
    lines.append("# ── ANSI 16 色 ──")
    lines.append(f"# 普通色")
    lines.append(f"color0  {rgb_to_hex(*color0)}   # 黑")
    lines.append(f"color1  {rgb_to_hex(*color1)}   # 红")
    lines.append(f"color2  {rgb_to_hex(*color2)}   # 绿")
    lines.append(f"color3  {rgb_to_hex(*color3)}   # 黄/橙")
    lines.append(f"color4  {rgb_to_hex(*color4)}   # 蓝")
    lines.append(f"color5  {rgb_to_hex(*color5)}   # 紫")
    lines.append(f"color6  {rgb_to_hex(*color6)}   # 青")
    lines.append(f"color7  {rgb_to_hex(*color7)}   # 浅灰")
    lines.append(f"")
    lines.append(f"# 亮色")
    lines.append(f"color8  {rgb_to_hex(*color8)}   # 亮黑(灰)")
    lines.append(f"color9  {rgb_to_hex(*color9)}   # 亮红")
    lines.append(f"color10 {rgb_to_hex(*color10)}  # 亮绿")
    lines.append(f"color11 {rgb_to_hex(*color11)}  # 亮黄")
    lines.append(f"color12 {rgb_to_hex(*color12)}  # 亮蓝")
    lines.append(f"color13 {rgb_to_hex(*color13)}  # 亮紫")
    lines.append(f"color14 {rgb_to_hex(*color14)}  # 亮青")
    lines.append(f"color15 {rgb_to_hex(*color15)}  # 亮白")
    lines.append("")
    lines.append("# ── Tab 栏 ──")
    lines.append(f"active_tab_foreground   {rgb_to_hex(*view_bg)}")
    lines.append(f"active_tab_background   {rgb_to_hex(*accent)}")
    lines.append(f"inactive_tab_foreground {rgb_to_hex(*inactive_fg)}")
    lines.append(f"inactive_tab_background {rgb_to_hex(*window_bg)}")
    lines.append(f"tab_bar_background      {rgb_to_hex(*window_bg)}")
    lines.append("")
    lines.append("# ── 窗口边框 ──")
    lines.append(f"active_border_color     {rgb_to_hex(*accent)}")
    lines.append(f"inactive_border_color   {rgb_to_hex(*window_bg)}")
    lines.append(f"bell_border_color       {rgb_to_hex(*neg_fg)}")

    return "\n".join(lines) + "\n"


def main():
    output_path = None
    apply_flag = False
    strict = True
    wait = 0.6
    attempts = 3

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--output" or args[i] == "-o":
            i += 1
            if i >= len(args):
                print("[✗] --output 缺少参数", file=sys.stderr)
                sys.exit(2)
            output_path = args[i]
        elif args[i] == "--apply":
            apply_flag = True
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

    if apply_flag and not output_path:
        output_path = os.path.expanduser("~/.config/kitty/colors-kde.conf")

    colors, scheme_name = load_kde_colors_with_retry(wait=wait, attempts=attempts, strict=strict)
    theme = generate_kitty_theme(colors, scheme_name)

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(theme)
        print(f"[✓] 已写入: {output_path}", file=sys.stderr)
        print(f"[*] 在 kitty.conf 中确保有: include colors-kde.conf", file=sys.stderr)

        # 如果使用了 quickshell 的自动主题，提醒用户可能需要注释掉
        kitty_conf = os.path.expanduser("~/.config/kitty/kitty.conf")
        if os.path.exists(kitty_conf):
            with open(kitty_conf) as f:
                # 只看生效的行，忽略注释（否则已注释掉的 quickshell include 会一直误报）
                active_lines = [ln for ln in f
                                if ln.strip() and not ln.lstrip().startswith("#")]
            if any("quickshell" in ln for ln in active_lines):
                print("[!] 检测到 kitty.conf 中有生效的 quickshell 主题引用，"
                      "可能与本配色冲突，建议注释掉", file=sys.stderr)
    else:
        print(theme)


if __name__ == "__main__":
    main()
