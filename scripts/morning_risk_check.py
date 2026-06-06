#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
早盘风险检查: 每天9:25集合竞价后运行
  检查持仓股是否跳空低开过大
  飞书报警 + 给出操作建议
"""

import json, sys, time, urllib.request
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).parent.parent
PORTFOLIO_FILE = BASE_DIR / "data" / "live_portfolio.json"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a6ff4711-c5f6-4d9e-83dd-e43780bc625f"
QT_URL = "https://qt.gtimg.cn/q="


def send_feishu(title, content, color="yellow"):
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color
            },
            "elements": [
                {"tag": "markdown", "content": content}
            ]
        }
    }
    try:
        req = urllib.request.Request(FEISHU_WEBHOOK,
            data=json.dumps(card).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"飞书失败: {e}")
        return False


def get_realtime(symbols):
    """批量获取实时行情"""
    results = {}
    batch_size = 50
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        url = QT_URL + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://gu.qq.com/"})
            raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", errors="ignore")
            for line in raw.strip().split("\n"):
                if not line.strip(): continue
                parts = line.split("~")
                if len(parts) < 40: continue
                # 格式: v_sh600519="1~贵州茅台~..."
                symbol = parts[0].split('"')[1] if '"' in parts[0] else parts[0].replace("v_","")
                try:
                    results[symbol] = {
                        "name": parts[1],
                        "open": float(parts[5]) if parts[5] else 0,
                        "price": float(parts[3]) if parts[3] else 0,
                        "high": float(parts[33]) if parts[33] else 0,
                        "low": float(parts[34]) if parts[34] else 0,
                        "prev_close": float(parts[4]) if parts[4] else 0,
                        "change_pct": float(parts[32]) if parts[32] else 0,
                        "volume": float(parts[6]) if parts[6] else 0,
                    }
                except: pass
        except Exception as e:
            print(f"  行情获取失败: {e}")
    return results


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    wd = datetime.now().weekday()

    if wd >= 5:
        print(f"[跳过] {today} 周末")
        return

    # 读取持仓
    if not PORTFOLIO_FILE.exists():
        print("无持仓文件")
        return

    pf = json.load(open(PORTFOLIO_FILE, "r", encoding="utf-8"))
    holdings = pf.get("holdings", [])

    if not holdings:
        print("空仓, 无需检查")
        return

    # 获取实时行情
    symbols = [h["symbol"] for h in holdings]
    print(f"[{today}] 检查 {len(symbols)} 只持仓...")
    rt = get_realtime(symbols)

    # 分级检查
    alerts_red = []    # 危险: 跳空>5% 或 跌停
    alerts_yellow = [] # 警告: 跳空3-5%
    alerts_info = []   # 正常: 跳空<3%

    for h in holdings:
        sym = h["symbol"]
        rdata = rt.get(sym)
        if not rdata:
            alerts_info.append(f"⚠ {h['name']}({h['code']}) 无行情数据")
            continue

        gap = (rdata["open"] - rdata["prev_close"]) / rdata["prev_close"] * 100 if rdata["prev_close"] > 0 else 0
        chg = rdata.get("change_pct", 0)
        price = rdata.get("price", 0)
        buy_price = h.get("buy_price", 0)
        pnl = (price / buy_price - 1) * 100 if buy_price > 0 else 0
        stop = h.get("stop_loss", 0)

        # 更新持仓当前价
        h["current_price"] = price
        h["float_pnl_pct"] = round(pnl, 2)

        # 触发风险等级
        if gap <= -8 or chg <= -9:
            alerts_red.append((h, gap, chg, price, stop, pnl))
        elif gap <= -3 or chg <= -5:
            alerts_yellow.append((h, gap, chg, price, stop, pnl))
        else:
            alerts_info.append((h, gap, chg, price, stop, pnl))

    # 保存更新后的持仓
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)

    # ==== 构建飞书消息 ====
    equity = pf.get("cash", 0)
    for h in holdings:
        equity += h.get("shares", 0) * (h.get("current_price", h.get("buy_price", 0)))

    lines = []
    lines.append(f"**{today} 早盘风险检查**\n")
    lines.append(f"持仓 {len(holdings)} 只 | 权益 ¥{equity:,.0f}\n")
    lines.append("---\n")

    # 红色警报
    if alerts_red:
        lines.append(f"**🔴 红色警报 ({len(alerts_red)}只)** - 建议立即处理\n")
        for h, gap, chg, price, stop, pnl in alerts_red:
            lines.append(f"> **{h['name']}({h['code']})** 开盘跳空 **{gap:+.1f}%**\n")
            lines.append(f"> 买入 ¥{h['buy_price']:.2f} → 现价 ¥{price:.2f} ({pnl:+.1f}%)\n")
            lines.append(f"> 止损线 ¥{stop:.2f} — **止损已失效**, 建议: 挂跌停价排队卖出\n\n")
        color = "red"
    elif alerts_yellow:
        color = "yellow"
    else:
        color = "green"

    # 黄色警告
    if alerts_yellow:
        lines.append(f"**🟡 风险警告 ({len(alerts_yellow)}只)** - 密切关注\n")
        for h, gap, chg, price, stop, pnl in alerts_yellow:
            lines.append(f"> **{h['name']}({h['code']})** 开盘跳空 **{gap:+.1f}%** | 现跌 **{chg:+.1f}%**\n")
            lines.append(f"> 买入 ¥{h['buy_price']:.2f} → 现价 ¥{price:.2f} ({pnl:+.1f}%)\n")
            lines.append(f"> 止损线 ¥{stop:.2f} | 建议: 设止损条件单, 如继续下杀手动跑\n\n")

    # 正常
    if alerts_info and not alerts_red and not alerts_yellow:
        lines.append(f"**🟢 全部正常** - {len(alerts_info)}只持仓无异常\n")

    for h, gap, chg, price, stop, pnl in alerts_info:
        if isinstance(h, dict):
            lines.append(f"> {h['name']}({h['code']}) 开{gap:+.1f}% 现{price:.2f} 止损{stop:.2f} 浮{pnl:+.1f}%\n")

    # 发送
    total_alerts = len(alerts_red) + len(alerts_yellow)
    title = f"{'🔴' if alerts_red else '🟡' if alerts_yellow else '🟢'} 早盘检查 {'⚠'+str(total_alerts)+'只风险' if total_alerts else '全部正常'}"

    send_feishu(title, "".join(lines), color)
    print(f"[{today}] 早盘检查完成: 红{len(alerts_red)} 黄{len(alerts_yellow)} 绿{len(alerts_info)}")


if __name__ == "__main__":
    main()
