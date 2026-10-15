"""报告用的内联 SVG 图表：纯字符串拼接，零依赖。

为什么不让模型画图：模型输出的图表数值是"编"的，和库里的数据对不上；
而 ECharts 那套只能在浏览器里跑，导出成 PDF/邮件附件就没了。
所以图表一律由平台侧用真实数据生成 SVG，直接内嵌进 HTML —— 打开、打印、转 PDF 都不掉。
"""
from __future__ import annotations

import html

BRAND = "#3d5fd9"
BRAND2 = "#6b86e8"
NEG = "#cf4a44"
MID = "#8b93a3"
POS = "#178a5f"
PALETTE = [BRAND, "#cf4a44", "#e08a1e", "#178a5f", "#0aa2c0", "#7c5cff", "#59637a", "#b07d1a"]


def _esc(t) -> str:
    return html.escape(str(t))


def kpi_row(items: list[dict]) -> str:
    """KPI 卡片行：[{label, value, sub}]，用 flex 排，打印也不会散。

    value 可能是数字也可能是平台名/标题这类文字，所以字号按长度降一档，
    否则「微信公众号」这种五个字会把卡片撑破行。
    """
    cards = []
    for i, it in enumerate(items):
        val = str(it["value"])
        size = 21 if len(val) <= 8 else (17 if len(val) <= 12 else 15)
        cards.append(
            f'<div style="flex:1 1 158px;min-width:158px;background:#fff;border:1px solid #e8eaef;'
            f'border-radius:12px;padding:12px 14px;position:relative;overflow:hidden">'
            f'<div style="position:absolute;left:0;top:0;bottom:0;width:3px;'
            f'background:{PALETTE[i % len(PALETTE)]}"></div>'
            f'<div style="font-size:{size}px;font-weight:650;letter-spacing:-.3px;'
            f'white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{_esc(val)}</div>'
            f'<div style="font-size:12px;color:#566072">{_esc(it["label"])}</div>'
            f'<div style="font-size:11px;color:#8b93a3;margin-top:2px;white-space:nowrap;'
            f'overflow:hidden;text-overflow:ellipsis">{_esc(it.get("sub", ""))}</div>'
            f'</div>')
    return ('<div class="kpi-row" style="display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 6px">'
            + "".join(cards) + "</div>")


def line(points: list[tuple[str, float]], series2: list[float] | None = None,
         w: int = 780, h: int = 210, label1: str = "声量", label2: str = "负面声量") -> str:
    """按日趋势：面积折线 + 可选第二条线。x 轴只标首/中/尾，避免挤成一团。"""
    if not points:
        return '<div style="color:#8b93a3;font-size:12px">没有可画的趋势数据</div>'
    pad_l, pad_b, pad_t = 38, 22, 10
    vals = [v for _, v in points] + (series2 or [])
    top = max(vals + [1])
    plot_w, plot_h = w - pad_l - 10, h - pad_b - pad_t
    n = max(1, len(points) - 1)

    def xy(i, v):
        return (pad_l + plot_w * i / n, pad_t + plot_h * (1 - v / top))

    def path(seq):
        return " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}"
                        for i, (x, y) in enumerate(xy(i, v) for i, v in enumerate(seq)))

    main = [v for _, v in points]
    area = (path(main) + f" L{pad_l + plot_w:.1f},{pad_t + plot_h:.1f} L{pad_l:.1f},{pad_t + plot_h:.1f} Z")
    grid = "".join(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h * k / 3:.1f}" x2="{w - 10}" '
        f'y2="{pad_t + plot_h * k / 3:.1f}" stroke="#eef0f5"/>'
        f'<text x="{pad_l - 6}" y="{pad_t + plot_h * k / 3 + 4:.1f}" font-size="10" fill="#8b93a3" '
        f'text-anchor="end">{int(top * (3 - k) / 3)}</text>' for k in range(4))
    ticks = ""
    for i in (0, len(points) // 2, len(points) - 1):
        x, _ = xy(i, 0)
        ticks += (f'<text x="{x:.1f}" y="{h - 6}" font-size="10" fill="#8b93a3" '
                  f'text-anchor="middle">{_esc(points[i][0])}</text>')
    second = ""
    if series2:
        second = (f'<path d="{path(series2)}" fill="none" stroke="{NEG}" stroke-width="2"/>')
    legend = (f'<circle cx="{pad_l + 4}" cy="{pad_t - 2}" r="3" fill="{BRAND}"/>'
              f'<text x="{pad_l + 12}" y="{pad_t + 2}" font-size="10" fill="#566072">{_esc(label1)}</text>')
    if series2:
        legend += (f'<circle cx="{pad_l + 70}" cy="{pad_t - 2}" r="3" fill="{NEG}"/>'
                   f'<text x="{pad_l + 78}" y="{pad_t + 2}" font-size="10" fill="#566072">{_esc(label2)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">{grid}'
            f'<path d="{area}" fill="{BRAND}" fill-opacity=".10"/>'
            f'<path d="{path(main)}" fill="none" stroke="{BRAND}" stroke-width="2"/>'
            f'{second}{ticks}{legend}</svg>')


def donut(parts: list[tuple[str, float]], center: str = "", sub: str = "",
          size: int = 200) -> str:
    """情感/结构环形图。用 stroke-dasharray 画，兼容性最好。"""
    total = sum(v for _, v in parts) or 1
    r, cx, cy, sw = 70, size / 2, size / 2, 20
    circ = 2 * 3.14159265 * r
    off = 0.0
    segs = []
    for i, (name, v) in enumerate(parts):
        frac = v / total
        segs.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{PALETTE[i % len(PALETTE)]}" '
            f'stroke-width="{sw}" stroke-dasharray="{circ * frac:.2f} {circ:.2f}" '
            f'stroke-dashoffset="{-circ * off:.2f}" transform="rotate(-90 {cx} {cy})"/>')
        off += frac
    txt = ""
    if center:
        txt = (f'<text x="{cx}" y="{cy - 2}" font-size="19" font-weight="650" fill="#1c212b" '
               f'text-anchor="middle">{_esc(center)}</text>'
               f'<text x="{cx}" y="{cy + 16}" font-size="11" fill="#8b93a3" '
               f'text-anchor="middle">{_esc(sub)}</text>')
    rows = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;font-size:12px;margin:3px 0">'
        f'<i style="width:8px;height:8px;border-radius:2px;background:{PALETTE[i % len(PALETTE)]};'
        f'display:inline-block"></i><span style="color:#566072">{_esc(n)}</span>'
        f'<b style="margin-left:auto">{_esc(f"{v:,.0f}")}</b>'
        f'<span style="color:#8b93a3;width:44px;text-align:right">{v / total * 100:.1f}%</span></div>'
        for i, (n, v) in enumerate(parts))
    return (f'<div style="display:flex;gap:14px;align-items:center">'
            f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(segs)}{txt}</svg>'
            f'<div style="flex:1;min-width:150px">{rows}</div></div>')


def bar(rows: list[tuple[str, float, float]], w: int = 380, label_w: int = 76) -> str:
    """横向条形（名称, 主值, 其中负面值）——用于平台声量对比，负面部分深色叠加。"""
    if not rows:
        return '<div style="color:#8b93a3;font-size:12px">没有可画的数据</div>'
    top = max([v for _, v, _ in rows] + [1])
    bh, gap = 18, 10
    h = len(rows) * (bh + gap) + 6
    body = []
    for i, (name, v, neg) in enumerate(rows):
        y = i * (bh + gap)
        full = (w - label_w - 46) * v / top
        part = (w - label_w - 46) * (neg or 0) / top
        body.append(
            f'<text x="0" y="{y + 13}" font-size="11.5" fill="#566072">{_esc(name[:6])}</text>'
            f'<rect x="{label_w}" y="{y}" width="{full:.1f}" height="{bh}" rx="4" fill="{BRAND}" '
            f'fill-opacity=".22"/>'
            f'<rect x="{label_w}" y="{y}" width="{part:.1f}" height="{bh}" rx="4" fill="{NEG}"/>'
            f'<text x="{label_w + full + 6:.1f}" y="{y + 13}" font-size="11" fill="#1c212b">'
            f'{_esc(f"{v:,.0f}")}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(body)}</svg>'
            f'<div style="font-size:11px;color:#8b93a3;margin-top:4px">'
            f'深色为负面部分，浅色为该平台总声量</div>')


def table(headers: list[str], rows: list[list], align_right: set[int] | None = None,
          raw_cols: set[int] | None = None) -> str:
    """raw_cols 里的列不做转义，用来放已经拼好的小徽章（如"重点"标记）。"""
    align_right = align_right or set()
    raw_cols = raw_cols or set()
    th = "".join(f'<th>{_esc(h)}</th>' for h in headers)
    tr = "".join("<tr>" + "".join(
        f'<td style="text-align:{"right" if i in align_right else "left"}">'
        f'{c if i in raw_cols else _esc(c)}</td>'
        for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"
