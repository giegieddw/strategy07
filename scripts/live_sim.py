#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Strategy 07 实盘模拟系统
  每天15:00后运行: 检查止损 → 扫描信号 → 买入 → 记录 → 出报告
  首次运行自动初始化10万本金
"""

import json, sys, time, urllib.request
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

# ============================================================
# 配置
# ============================================================
BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
PORTFOLIO_FILE = DATA_DIR / "live_portfolio.json"  # 持仓状态
LOG_DIR = OUTPUT_DIR / "live_logs"                   # 每日日志
LOG_DIR.mkdir(parents=True, exist_ok=True)

GAP_THRESHOLD = 0.002
STOP_LOSS_PCT = 0.01
SCORE_MIN = 8
MAX_HOLDINGS = 5
MAX_HOLD_DAYS = 20
CAP_PER_STOCK = 20000          # 每只2万
INIT_CASH = 100000             # 初始10万

# 交易成本 (模拟盘记录但不影响决策)
COMM_BUY = 0.00025
COMM_SELL = 0.00025
STAMP = 0.0005
SLIPPAGE = 0.002

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QT_URL = "https://qt.gtimg.cn/q="

WEEKDAY_CN = ["周一","周二","周三","周四","周五","周六","周日"]


def get_stock_pool():
    sys.path.insert(0, str(BASE_DIR / "scripts"))
    from virtual_portfolio import get_stock_pool as _gp
    return _gp()


def is_st(symbol):
    """动态ST检测"""
    try:
        url = f"{QT_URL}{symbol}"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=3).read().decode("gbk", errors="ignore")
        # 提取名称: v_sh600519="1~贵州茅台~..."
        parts = data.split("~")
        if len(parts) > 1:
            return "ST" in parts[1]
    except:
        pass
    return False


def fetch_klines(symbol, count=30):
    """获取近期K线"""
    url = f"{KLINE_URL}?_var=kline_day&param={symbol},day,,,{count},qfq"
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://gu.qq.com/"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
        start = data.find("{")
        if start == -1: return []
        jd = json.loads(data[start:])
        raw = jd["data"][symbol].get("qfqday", jd["data"][symbol].get("day", []))
        rows = []
        for r in raw:
            if len(r) < 6: continue
            rows.append({"date": r[0], "open": float(r[1]), "close": float(r[2]),
                "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])})
        return rows
    except:
        return []


def load_portfolio():
    """加载持仓状态"""
    if PORTFOLIO_FILE.exists():
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # 首次运行: 初始化
    return {
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "initial_cash": INIT_CASH,
        "cash": INIT_CASH,
        "holdings": [],
        "closed": [],
        "daily_equity": [],
        "trade_count": 0,
        "stats": {"total_trades": 0, "win_trades": 0, "total_pnl": 0}
    }


def save_portfolio(pf):
    """保存持仓状态"""
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(pf, f, ensure_ascii=False, indent=2)


def daily_run(date_str=None):
    """
    每日运行:
      1. 检查持仓止损/强平
      2. 扫描新信号
      3. 买入
      4. 生成报告
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    today = date_str
    wd = datetime.strptime(today, "%Y-%m-%d").weekday()
    if wd >= 5:
        print(f"[跳过] {today} 是周末, 不运行")
        return

    print(f"\n{'='*80}")
    print(f"  Strategy 07 实盘模拟 - {today} {WEEKDAY_CN[wd]}")
    print(f"  本金: ¥{INIT_CASH:,} | 每只: ¥{CAP_PER_STOCK:,} | 评分≥{SCORE_MIN} | 止损{STOP_LOSS_PCT*100}%")
    print(f"{'='*80}")

    # 加载状态
    pf = load_portfolio()
    cash = pf["cash"]
    holdings = pf["holdings"]
    pool = get_stock_pool()

    # ---- 阶段0: 获取所有K线 ----
    print(f"\n[0] 获取K线数据...")
    klines_dict = {}
    for i, s in enumerate(pool):
        kl = fetch_klines(s["symbol"], count=30)
        if kl:
            klines_dict[s["symbol"]] = kl
    print(f"    有效: {len(klines_dict)}只")

    # 构建今日数据索引
    today_klines = {}
    yesterday_klines = {}
    for sym, kl in klines_dict.items():
        for i, k in enumerate(kl):
            if k["date"] == today:
                today_klines[sym] = k
                if i > 0:
                    yesterday_klines[sym] = kl[i-1]
                break

    print(f"    今日有数据: {len(today_klines)}只")

    # ---- 阶段1: 检查止损 ----
    print(f"\n[1] 检查持仓 ({len(holdings)}只)...")
    closed_today = []

    for pos in list(holdings):
        symbol = pos["symbol"]
        k = today_klines.get(symbol)
        if k is None:
            print(f"    ⚠ {pos['name']}({pos['code']}) 无今日数据, 跳过")
            continue

        # 计算持有天数
        buy_date = pos["buy_date"]
        days_held = 0
        for d in get_trading_dates_since(buy_date, today, klines_dict):
            days_held += 1

        # 先检查止损
        if k["low"] <= pos["stop_loss"]:
            sell_price = pos["stop_loss"]
            # 模拟滑点
            actual_sell = sell_price * (1 - SLIPPAGE)
            proceeds = actual_sell * pos["shares"]
            fee = proceeds * (COMM_SELL + STAMP)
            net = proceeds - fee

            pnl_amt = net - (pos["buy_price"] * pos["shares"])
            pnl_pct = pnl_amt / (pos["buy_price"] * pos["shares"]) * 100

            pos["exit_date"] = today
            pos["exit_price"] = actual_sell
            pos["pnl_pct"] = round(pnl_pct, 2)
            pos["pnl_amt"] = round(pnl_amt, 2)
            pos["exit_reason"] = "止损"
            pos["days_held"] = days_held

            cash += net
            pf["stats"]["total_trades"] += 1
            if pnl_amt > 0:
                pf["stats"]["win_trades"] += 1
            pf["stats"]["total_pnl"] += pnl_amt

            closed_today.append(pos)
            pf["closed"].append(pos)

            icon = "✅" if pnl_amt > 0 else "❌"
            print(f"    {icon} {pos['name']}({pos['code']}) 止损触发!")
            print(f"       买{pos['buy_date']} ¥{pos['buy_price']:.2f} → 卖{today} ¥{actual_sell:.2f}")
            print(f"       盈亏: {pnl_pct:+.2f}% ({pnl_amt:+.0f}元) | 持有{days_held}天")
            continue

        # 更新移动止损
        if k["close"] > pos.get("highest_close", pos["buy_price"]):
            pos["highest_close"] = k["close"]
            pos["stop_loss"] = round(k["close"] * (1 - STOP_LOSS_PCT), 2)

        # 强平
        if days_held >= MAX_HOLD_DAYS:
            actual_sell = k["close"] * (1 - SLIPPAGE)
            proceeds = actual_sell * pos["shares"]
            fee = proceeds * (COMM_SELL + STAMP)
            net = proceeds - fee

            pnl_amt = net - (pos["buy_price"] * pos["shares"])
            pnl_pct = pnl_amt / (pos["buy_price"] * pos["shares"]) * 100

            pos["exit_date"] = today
            pos["exit_price"] = actual_sell
            pos["pnl_pct"] = round(pnl_pct, 2)
            pos["pnl_amt"] = round(pnl_amt, 2)
            pos["exit_reason"] = "强平"
            pos["days_held"] = days_held

            cash += net
            pf["stats"]["total_trades"] += 1
            if pnl_amt > 0:
                pf["stats"]["win_trades"] += 1
            pf["stats"]["total_pnl"] += pnl_amt

            closed_today.append(pos)
            pf["closed"].append(pos)

            icon = "✅" if pnl_amt > 0 else "❌"
            print(f"    {icon} {pos['name']}({pos['code']}) 持有{days_held}天强平")
            print(f"       买{pos['buy_date']} ¥{pos['buy_price']:.2f} → 卖{today} ¥{actual_sell:.2f}")
            print(f"       盈亏: {pnl_pct:+.2f}% ({pnl_amt:+.0f}元)")
            continue

    # 移除已平仓
    closed_codes = {p["code"] for p in closed_today}
    holdings = [h for h in holdings if h["code"] not in closed_codes]

    if not closed_today:
        print(f"    无触发")

    # ---- 阶段2: 扫描信号 ----
    print(f"\n[2] 扫描信号...")
    held_symbols = {h["symbol"] for h in holdings}
    candidates = []

    for s in pool:
        symbol = s["symbol"]
        if symbol in held_symbols:
            continue

        k = today_klines.get(symbol)
        if k is None:
            continue

        prev_k = yesterday_klines.get(symbol)
        if prev_k is None or prev_k["close"] <= 0 or k["open"] <= 0:
            continue

        gap_pct = (k["open"] - prev_k["close"]) / prev_k["close"]
        if gap_pct > -GAP_THRESHOLD:
            continue
        if k["close"] <= k["open"]:
            continue

        reversal_pct = (k["close"] - k["open"]) / k["open"] * 100
        score = abs(gap_pct * 100) + reversal_pct
        if score < SCORE_MIN:
            continue

        # ST检查
        if is_st(symbol):
            continue

        candidates.append({
            "symbol": symbol,
            "code": s["code"],
            "name": s["name"],
            "gap_pct": round(gap_pct * 100, 2),
            "reversal_pct": round(reversal_pct, 2),
            "score": round(score, 2),
            "close": k["close"],
            "open": k["open"],
            "prev_close": prev_k["close"],
            "high": k["high"],
            "low": k["low"],
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    print(f"    信号: {len(candidates)}个 | 持仓{len(holdings)}只 | 空位{MAX_HOLDINGS - len(holdings)}个")

    # ---- 阶段3: 买入 ----
    slots = MAX_HOLDINGS - len(holdings)
    bought_today = []

    if slots > 0 and candidates:
        print(f"\n[3] 买入 (最多{slots}只)...")
        for c in candidates[:slots]:
            base_price = c["close"]
            actual_price = base_price  # 模拟以收盘价买入（暂不加滑点）

            shares = int(CAP_PER_STOCK / actual_price / 100) * 100
            if shares < 100:
                print(f"    ⚠ {c['name']}({c['code']}) 股价{c['close']:.2f}太高, 2万买不起1手, 跳过")
                continue

            cost = shares * actual_price
            fee = cost * COMM_BUY
            total = cost + fee

            if cash < total:
                print(f"    ⚠ {c['name']}({c['code']}) 现金不足 (需¥{total:,.0f}, 仅¥{cash:,.0f}), 跳过")
                continue

            cash -= total
            pf["trade_count"] += 1

            pos = {
                "code": c["code"],
                "name": c["name"],
                "symbol": c["symbol"],
                "buy_date": today,
                "buy_price": actual_price,
                "shares": shares,
                "cost": round(cost, 2),
                "fee": round(fee, 2),
                "stop_loss": round(actual_price * (1 - STOP_LOSS_PCT), 2),
                "highest_close": actual_price,
                "gap_pct": c["gap_pct"],
                "reversal_pct": c["reversal_pct"],
                "score": c["score"],
                "status": "holding",
                "trade_no": pf["trade_count"],
            }

            holdings.append(pos)
            bought_today.append(pos)

            print(f"    #{pos['trade_no']} 买入 {c['name']}({c['code']})")
            print(f"       收盘价: ¥{actual_price:.2f} | {shares}股 | ¥{cost:,.0f}")
            print(f"       止损: ¥{pos['stop_loss']:.2f} | 跳空{c['gap_pct']:+.1f}% 反弹{c['reversal_pct']:+.1f}% 评分{c['score']:.1f}")

    # 信号展示
    if candidates:
        print(f"\n[信号TOP15]")
        print(f"  {'排名':<4} {'代码':<8} {'名称':<6} {'跳空%':>7} {'反弹%':>7} {'评分':>6} {'收盘':>8} {'状态'}")
        print(f"  {'-'*60}")
        for i, c in enumerate(candidates[:15], 1):
            bought = any(b["code"] == c["code"] for b in bought_today)
            status = "✅已买" if bought else ("排队" if i <= slots else "")
            print(f"  {i:<4} {c['code']:<8} {c['name']:<6} {c['gap_pct']:>+6.2f}% {c['reversal_pct']:>+6.2f}% {c['score']:>6.2f} {c['close']:>8.2f}  {status}")

    # ---- 阶段4: 计算权益 ----
    equity = cash
    for pos in holdings:
        k = today_klines.get(pos["symbol"], {})
        cur = k.get("close", pos["buy_price"]) if k else pos["buy_price"]
        pos["current_price"] = cur
        pos["float_pnl_pct"] = round((cur / pos["buy_price"] - 1) * 100, 2)
        equity += pos["shares"] * cur

    # 更新状态
    pf["cash"] = round(cash, 2)
    pf["holdings"] = holdings
    pf["daily_equity"].append({
        "date": today,
        "equity": round(equity, 2),
        "cash": round(cash, 2),
        "positions": len(holdings)
    })

    total_pnl = equity - INIT_CASH

    # ---- 阶段5: 持仓展示 ----
    if holdings:
        print(f"\n[当前持仓] {len(holdings)}只")
        print(f"  {'代码':<8} {'名称':<6} {'买入日':<12} {'买入价':>8} {'现价':>8} {'止损':>8} {'浮盈%':>7} {'持有天':>6}")
        print(f"  {'-'*70}")
        for pos in holdings:
            cur = pos.get("current_price", pos["buy_price"])
            fl = pos.get("float_pnl_pct", 0)
            days = 0
            for d in get_trading_dates_since(pos["buy_date"], today, klines_dict):
                days += 1
            icon = "📈" if fl > 0 else "📉"
            print(f"  {pos['code']:<8} {pos['name']:<6} {pos['buy_date']:<12} {pos['buy_price']:>8.2f} {cur:>8.2f} {pos['stop_loss']:>8.2f} {icon}{fl:>+6.2f}% {days:>4}天")

    # ---- 阶段6: 总结 ----
    print(f"\n{'='*80}")
    print(f"  📊 账户概览")
    print(f"{'='*80}")
    print(f"  初始资金:   ¥{INIT_CASH:>,.0f}")
    print(f"  当前权益:   ¥{equity:>,.0f}")
    print(f"  累计盈亏:   {total_pnl:+,.0f}元 ({total_pnl/INIT_CASH*100:+.2f}%)")
    print(f"  可用现金:   ¥{cash:>,.0f}")
    print(f"  持仓市值:   ¥{equity - cash:>,.0f}")
    print(f"  持仓数量:   {len(holdings)}/{MAX_HOLDINGS}")
    print(f"  累计交易:   {len(pf['closed'])}笔")
    if pf["closed"]:
        wins = sum(1 for t in pf["closed"] if t.get("pnl_amt", 0) > 0)
        print(f"  胜率:       {wins/len(pf['closed'])*100:.1f}%")
    print(f"{'='*80}")

    # 保存
    save_portfolio(pf)

    # 写日志
    log_file = LOG_DIR / f"report_{today}.txt"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"Strategy 07 实盘模拟 - {today}\n")
        f.write(f"权益: {equity:,.0f} | 盈亏: {total_pnl:+,.0f} | 持仓: {len(holdings)}只\n")
        if bought_today:
            f.write(f"\n买入:\n")
            for b in bought_today:
                f.write(f"  {b['code']} {b['name']} ¥{b['buy_price']:.2f}\n")
        if closed_today:
            f.write(f"\n卖出:\n")
            for c in closed_today:
                f.write(f"  {c['code']} {c['name']} 盈亏{c.get('pnl_pct',0):+.2f}%\n")

    print(f"\n日志: {log_file}")
    print(f"持仓: {PORTFOLIO_FILE}\n")

    return pf


def get_trading_dates_since(start_date, end_date, klines_dict):
    """获取两个日期之间的交易日"""
    dates = set()
    for kl in klines_dict.values():
        for k in kl:
            if start_date < k["date"] <= end_date:
                dates.add(k["date"])
    return sorted(dates)


def show_portfolio():
    """查看当前持仓"""
    pf = load_portfolio()
    total_pnl = sum(
        pos["shares"] * (pos.get("current_price", pos["buy_price"]) - pos["buy_price"])
        for pos in pf["holdings"]
    ) + pf["cash"] - INIT_CASH

    print(f"\n{'='*60}")
    print(f"  Strategy 07 实盘 - 当前状态")
    print(f"{'='*60}")
    print(f"  初始: ¥{INIT_CASH:,} | 权益: ¥{pf['cash']+sum(p['shares']*p.get('current_price',p['buy_price']) for p in pf['holdings']):,.0f}")
    print(f"  盈亏: {total_pnl:+,.0f}")
    print(f"  持仓: {len(pf['holdings'])}只 | 现金: ¥{pf['cash']:,.0f}")
    print(f"  累计交易: {len(pf['closed'])}笔")

    if pf["holdings"]:
        print(f"\n  {'代码':<8} {'名称':<6} {'买入价':>8} {'现价':>8} {'止损':>8} {'浮盈':>7}")
        for pos in pf["holdings"]:
            cur = pos.get("current_price", pos["buy_price"])
            fl = (cur / pos["buy_price"] - 1) * 100
            print(f"  {pos['code']:<8} {pos['name']:<6} {pos['buy_price']:>8.2f} {cur:>8.2f} {pos['stop_loss']:>8.2f} {fl:>+6.2f}%")
    print()


def show_signals():
    """仅扫描今日信号，不交易"""
    today = datetime.now().strftime("%Y-%m-%d")
    pool = get_stock_pool()
    klines_dict = {}
    for s in pool:
        kl = fetch_klines(s["symbol"], count=5)
        if kl: klines_dict[s["symbol"]] = kl

    today_klines = {}
    yesterday_klines = {}
    for sym, kl in klines_dict.items():
        for i, k in enumerate(kl):
            if k["date"] == today:
                today_klines[sym] = k
                if i > 0: yesterday_klines[sym] = kl[i-1]
                break

    candidates = []
    for s in pool:
        symbol = s["symbol"]
        k = today_klines.get(symbol)
        prev_k = yesterday_klines.get(symbol)
        if k is None or prev_k is None: continue
        gap = (k["open"]-prev_k["close"])/prev_k["close"]
        if gap > -GAP_THRESHOLD: continue
        if k["close"] <= k["open"]: continue
        rev = (k["close"]-k["open"])/k["open"]*100
        score = abs(gap*100)+rev
        if score < SCORE_MIN: continue
        if is_st(symbol): continue
        candidates.append({"code":s["code"],"name":s["name"],"gap":round(gap*100,2),"rev":round(rev,2),"score":round(score,2),"close":k["close"]})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n{today} 信号 (评分≥{SCORE_MIN}): {len(candidates)}个")
    for i, c in enumerate(candidates[:15], 1):
        print(f"  {i:2}. {c['code']} {c['name']:<6} 跳空{c['gap']:+.1f}% 反弹{c['rev']:+.1f}% 评分{c['score']:.1f} ¥{c['close']:.2f}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "run":
            date_arg = sys.argv[2] if len(sys.argv) > 2 else None
            daily_run(date_arg)
        elif cmd == "show":
            show_portfolio()
        elif cmd == "scan":
            show_signals()
        elif cmd == "backtest":
            # 回补历史日期
            date_arg = sys.argv[2] if len(sys.argv) > 2 else None
            if date_arg:
                daily_run(date_arg)
            else:
                print("用法: python live_sim.py backtest 2026-06-05")
        else:
            print("用法: python live_sim.py [run|show|scan|backtest <date>]")
    else:
        # 默认: 跑今天
        daily_run()
