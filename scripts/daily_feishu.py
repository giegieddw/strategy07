#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动: 跑实盘扫描 + 推送飞书
  用法: python scripts/daily_feishu.py
  可配合 WorkBuddy 定时任务每天15:10自动执行
"""

import json, sys, time, urllib.request
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).parent.parent

# 飞书 Webhook (从你的 bridge 脚本获取)
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/a6ff4711-c5f6-4d9e-83dd-e43780bc625f"

# 策略脚本
LIVE_SIM = BASE_DIR / "scripts" / "live_sim.py"
PYTHON = sys.executable  # 自动检测 Python 路径
PORTFOLIO_FILE = BASE_DIR / "data" / "live_portfolio.json"
LOG_DIR = BASE_DIR / "output" / "live_logs"


def send_feishu(title, content):
    """发送飞书消息"""
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green"
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
        print(f"飞书发送失败: {e}")
        return False


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    wd = datetime.now().weekday()
    wd_cn = ["周一","周二","周三","周四","周五","周六","周日"][wd]

    if wd >= 5:
        print(f"[跳过] {today} 是周末")
        return

    print(f"[{today}] 开始每日扫描...")

    # 1. 运行实盘扫描
    import subprocess
    r = subprocess.run([str(PYTHON), str(LIVE_SIM), "run"],
        capture_output=True, text=True, timeout=120, cwd=str(BASE_DIR))
    output = r.stdout

    if r.returncode != 0:
        send_feishu(f"❌ Strategy07 扫描失败 {today}", f"错误: {r.stderr[-500:]}")
        return

    # 2. 读取最新状态
    pf = json.load(open(PORTFOLIO_FILE, "r", encoding="utf-8"))
    holdings = pf.get("holdings", [])
    closed = pf.get("closed", [])
    cash = pf.get("cash", 0)

    # 计算权益
    equity = cash
    for h in holdings:
        equity += h.get("shares", 0) * h.get("current_price", h.get("buy_price", 0))

    init = pf.get("initial_cash", 100000)
    total_pnl = equity - init

    # ---- 构建飞书消息 ----
    lines = []
    lines.append(f"**{today} {wd_cn}** 盘后扫描完成\n")
    lines.append(f"权益: **¥{equity:,.0f}** | 累计: **{total_pnl:+,.0f}** ({total_pnl/init*100:+.1f}%)\n")
    lines.append(f"现金: ¥{cash:,.0f} | 持仓: {len(holdings)}/5\n")
    lines.append("---\n")

    # 今日操作
    today_sells = [t for t in closed if t.get("exit_date") == today]
    today_buys = [h for h in holdings if h.get("buy_date") == today]

    if today_sells:
        lines.append("**🔴 今日卖出:**\n")
        for t in today_sells:
            icon = "✅" if t.get("pnl_amt", 0) > 0 else "❌"
            lines.append(f"{icon} {t['name']}({t['code']}) {t.get('pnl_pct',0):+.1f}%\n")
        lines.append("\n")

    if today_buys:
        lines.append("**🟢 今日买入:**\n")
        for h in today_buys:
            lines.append(f"📈 {h['name']}({h['code']}) ¥{h['buy_price']:.2f} "
                        f"跳空{h.get('gap_pct',0):+.1f}% 反弹{h.get('reversal_pct',0):+.1f}% 评分{h.get('score',0):.0f}\n")
        lines.append("\n")

    # 当前持仓
    if holdings:
        lines.append("**📊 当前持仓:**\n")
        for h in holdings:
            cur = h.get("current_price", h.get("buy_price", 0))
            fl = (cur / h["buy_price"] - 1) * 100
            icon = "📈" if fl > 0 else "📉"
            lines.append(f"{icon} {h['name']}({h['code']}) 买¥{h['buy_price']:.2f} 现¥{cur:.2f} "
                        f"止损¥{h['stop_loss']:.2f} 浮{fl:+.1f}%\n")
    else:
        lines.append("**空仓中**\n")

    # 统计
    total_trades = len(closed)
    wins = sum(1 for t in closed if t.get("pnl_amt", 0) > 0)
    wr = wins / total_trades * 100 if total_trades else 0
    lines.append(f"\n累计: {total_trades}笔 | 盈{wins}笔 | 胜率{wr:.0f}%\n")

    # 发送
    content = "".join(lines)
    title = f"📊 S07 {today} 权益¥{equity:,.0f} ({total_pnl:+,.0f})"
    send_feishu(title, content)
    print(f"[{today}] 飞书推送完成")


if __name__ == "__main__":
    main()
