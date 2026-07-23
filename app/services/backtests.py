from __future__ import annotations

from math import isfinite

import pandas as pd
from backtesting import Backtest, Strategy
from backtesting.lib import crossover


STRATEGY_NAMES = {
    "fixed_stop": "固定止损",
    "moving_average": "技术信号：双均线交叉",
    "batch_buy": "分批买入与止盈止损",
    "trend_filter": "趋势过滤",
}

MARKET_RULES = {
    "t_plus_one": {"simulated": False, "detail": "未严格模拟同日买卖限制。"},
    "price_limit": {"simulated": False, "detail": "未模拟涨跌停导致的无法成交。"},
    "suspension": {"simulated": False, "detail": "未补充停牌日历。"},
    "volume_limit": {"simulated": False, "detail": "未按成交量限制可成交数量。"},
}


def _sma(values, length: int):
    return pd.Series(values).rolling(length).mean().to_numpy()


class BuyAndHold(Strategy):
    position_fraction = 0.999

    def init(self):
        pass

    def next(self):
        if not self.position:
            self.buy(size=self.position_fraction, tag="基准首次买入并持有至区间结束")


class FixedStopStrategy(Strategy):
    stop_pct = 0.08
    max_position = 0.2

    def init(self):
        pass

    def next(self):
        if not self.position:
            price = float(self.data.Close[-1])
            self.buy(
                size=self.max_position,
                sl=price * (1 - self.stop_pct),
                tag=f"空仓买入；初始止损 {self.stop_pct:.2%}",
            )


class MovingAverageStrategy(Strategy):
    fast = 5
    slow = 20
    max_position = 0.2

    def init(self):
        self.fast_ma = self.I(_sma, self.data.Close, self.fast)
        self.slow_ma = self.I(_sma, self.data.Close, self.slow)

    def next(self):
        if crossover(self.fast_ma, self.slow_ma) and not self.position:
            self.buy(size=self.max_position, tag=f"MA{self.fast} 上穿 MA{self.slow}")
        elif crossover(self.slow_ma, self.fast_ma) and self.position:
            self.position.close()


class TrendFilterStrategy(Strategy):
    fast = 5
    slow = 20
    max_position = 0.2

    def init(self):
        self.fast_ma = self.I(_sma, self.data.Close, self.fast)
        self.slow_ma = self.I(_sma, self.data.Close, self.slow)

    def next(self):
        close = float(self.data.Close[-1])
        if not self.position and self.fast_ma[-1] > self.slow_ma[-1] and close > self.slow_ma[-1]:
            self.buy(size=self.max_position, tag=f"收盘站上 MA{self.slow} 且快线高于慢线")
        elif self.position and close < self.slow_ma[-1]:
            self.position.close()


class BatchBuyStrategy(Strategy):
    stop_pct = 0.08
    max_position = 0.2
    batch_drop = 0.03
    take_profit = 0.1

    def init(self):
        self.batch_count = 0
        self.last_entry_reference = None

    def next(self):
        close = float(self.data.Close[-1])
        if not self.position:
            self.batch_count = 1
            self.last_entry_reference = close
            self.buy(size=self.max_position / 3, tag="第一批建仓（计划仓位的 1/3）")
            return
        # backtesting.py 的 Position.pl_pct 单位是“百分点”，参数使用小数比例。
        if self.position.pl_pct <= -self.stop_pct * 100:
            self.position.close()
            self.batch_count = 0
            return
        if self.position.pl_pct >= self.take_profit * 100:
            self.position.close()
            self.batch_count = 0
            return
        if self.batch_count < 3 and close <= self.last_entry_reference * (1 - self.batch_drop):
            self.batch_count += 1
            self.last_entry_reference = close
            self.buy(
                size=self.max_position / 3,
                tag=f"价格较上一批下跌 {self.batch_drop:.2%}，第 {self.batch_count} 批加仓",
            )


STRATEGY_CLASSES = {
    "fixed_stop": FixedStopStrategy,
    "moving_average": MovingAverageStrategy,
    "batch_buy": BatchBuyStrategy,
    "trend_filter": TrendFilterStrategy,
}


def _finite(value, digits: int = 4):
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return round(number, digits) if isfinite(number) else None


def _exit_reason(
    row, strategy_name: str, final_bar: int, parameters: dict, return_pct: float
) -> str:
    if row["ExitBar"] == final_bar:
        return "回测区间结束，强制平仓"
    if pd.notna(row.get("SL")) and abs(float(row["ExitPrice"]) - float(row["SL"])) <= max(
        abs(float(row["SL"])) * 0.002, 0.01
    ):
        return f"触发 {parameters['stop_pct']:.2%} 止损"
    if strategy_name == "moving_average":
        return f"MA{parameters['fast']} 下穿 MA{parameters['slow']}"
    if strategy_name == "trend_filter":
        return f"收盘跌破 MA{parameters['slow']}"
    if strategy_name == "batch_buy":
        return (
            f"触发整体持仓 {parameters['take_profit']:.2%} 止盈信号"
            if return_pct >= 0
            else f"触发整体持仓 {parameters['stop_pct']:.2%} 止损信号"
        )
    return "策略退出条件触发"


def _trade_details(stats, strategy_name: str, parameters: dict, final_bar: int) -> list[dict]:
    details = []
    raw_trades = stats["_trades"]
    for number, (_, group) in enumerate(raw_trades.groupby("ExitTime", sort=True), 1):
        quantity = int(group["Size"].abs().sum())
        invested = float((group["EntryPrice"] * group["Size"].abs()).sum())
        entry_price = invested / quantity if quantity else 0
        exit_price = (
            float((group["ExitPrice"] * group["Size"].abs()).sum() / quantity) if quantity else 0
        )
        pnl = float(group["PnL"].sum())
        return_pct = pnl / invested * 100 if invested else 0
        entry_time = pd.Timestamp(group["EntryTime"].min())
        exit_time = pd.Timestamp(group["ExitTime"].max())
        ordered_group = group.sort_values("EntryTime")
        tags = list(dict.fromkeys(str(value) for value in ordered_group["Tag"].dropna()))
        representative = group.iloc[-1].copy()
        representative["ExitBar"] = int(group["ExitBar"].max())
        representative["ExitPrice"] = exit_price
        details.append(
            {
                "trade_no": number,
                "entry_date": entry_time.date().isoformat(),
                "entry_price": round(entry_price, 4),
                "exit_date": exit_time.date().isoformat(),
                "exit_price": round(exit_price, 4),
                "direction": "买入→卖出" if group["Size"].sum() > 0 else "卖出→买回",
                "quantity": quantity,
                "entry_reason": "；".join(tags) if tags else "策略买入条件触发",
                "exit_reason": _exit_reason(
                    representative, strategy_name, final_bar, parameters, return_pct
                ),
                "return_pct": round(return_pct, 4),
                "pnl": round(pnl, 2),
                "cost": round(float(group["Commission"].fillna(0).sum()), 2),
                "holding_days": int((exit_time - entry_time).days),
            }
        )
    return details


def _curve(stats) -> list[dict]:
    return [
        {
            "date": pd.Timestamp(index).date().isoformat(),
            "equity": round(float(row["Equity"]), 2),
            "drawdown_pct": round(abs(float(row["DrawdownPct"])) * 100, 4),
        }
        for index, row in stats["_equity_curve"].iterrows()
    ]


def _metrics(stats, trades: list[dict], cash: float) -> dict:
    curve = _curve(stats)
    winning = [item["pnl"] for item in trades if item["pnl"] > 0]
    losing = [abs(item["pnl"]) for item in trades if item["pnl"] < 0]
    profit_factor = sum(winning) / sum(losing) if losing else None
    profit_loss_ratio = (
        (sum(winning) / len(winning)) / (sum(losing) / len(losing)) if winning and losing else None
    )
    total_return = (curve[-1]["equity"] / cash - 1) * 100
    return {
        "total_return_pct": round(total_return, 4),
        "annual_return_pct": _finite(stats["Return (Ann.) [%]"]),
        "max_drawdown_pct": round(max(item["drawdown_pct"] for item in curve), 4),
        "win_rate_pct": round(len(winning) / len(trades) * 100, 4) if trades else 0,
        "profit_loss_ratio": round(profit_loss_ratio, 4) if profit_loss_ratio is not None else None,
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "trade_count": len(trades),
        "average_holding_days": round(sum(item["holding_days"] for item in trades) / len(trades), 2)
        if trades
        else 0,
        "total_holding_days": sum(item["holding_days"] for item in trades),
        "exposure_pct": _finite(stats["Exposure Time [%]"]),
        "final_equity": curve[-1]["equity"],
        "total_cost": round(sum(item["cost"] for item in trades), 2),
    }


def _rules(strategy_name: str, parameters: dict) -> dict:
    if strategy_name == "fixed_stop":
        return {
            "buy": f"空仓时按下一交易日开盘买入，目标仓位 {parameters['max_position']:.0%}。",
            "sell": f"价格触及初始买入参考价下方 {parameters['stop_pct']:.2%} 时止损；区间结束强制平仓。",
            "reentry": "止损后保持空仓至当日结束，下一交易日再次按相同规则入场。",
        }
    if strategy_name == "moving_average":
        return {
            "buy": f"MA{parameters['fast']} 上穿 MA{parameters['slow']} 后，下一交易日开盘买入 {parameters['max_position']:.0%} 仓位。",
            "sell": f"MA{parameters['fast']} 下穿 MA{parameters['slow']} 后，下一交易日开盘卖出。",
            "reentry": "卖出后等待下一次新的金叉，不在均线持续多头时重复买入。",
        }
    if strategy_name == "trend_filter":
        return {
            "buy": f"收盘高于 MA{parameters['slow']} 且 MA{parameters['fast']} 高于 MA{parameters['slow']}，下一交易日开盘买入。",
            "sell": f"收盘跌破 MA{parameters['slow']}，下一交易日开盘卖出。",
            "reentry": "卖出后，趋势条件再次全部成立才重新入场。",
        }
    return {
        "buy": f"先买计划仓位的 1/3；每较上一批参考价下跌 {parameters['batch_drop']:.2%} 再买 1/3，最多三批。",
        "sell": f"整体持仓收益达到 {parameters['take_profit']:.2%} 止盈，或亏损达到 {parameters['stop_pct']:.2%} 止损。",
        "reentry": "全部卖出后，下一交易日重新开始第一批建仓。",
    }


def run_backtest(
    frame: pd.DataFrame,
    strategy_name: str,
    cash: float,
    commission: float,
    slippage: float,
    parameters: dict,
    adjustment: str = "前复权(qfq)",
) -> dict:
    if strategy_name not in STRATEGY_CLASSES:
        raise ValueError("不支持的交易规则")
    if len(frame) < 30:
        raise ValueError("回测至少需要 30 个交易日的数据")
    data = frame.copy().sort_index()
    if data.index.has_duplicates:
        raise ValueError("历史数据存在重复交易日，请重新同步或指定单一数据源")
    parameters = {
        "stop_pct": float(parameters.get("stop_pct", 0.08)),
        "fast": int(parameters.get("fast", 5)),
        "slow": int(parameters.get("slow", 20)),
        "max_position": float(parameters.get("max_position", 0.2)),
        "batch_drop": float(parameters.get("batch_drop", 0.03)),
        "take_profit": float(parameters.get("take_profit", 0.1)),
    }
    if parameters["fast"] >= parameters["slow"]:
        raise ValueError("快均线周期必须小于慢均线周期")
    if not 0 < parameters["max_position"] < 1:
        raise ValueError("单次最大仓位必须大于 0 且小于 100%")

    # backtesting.py 没有独立滑点参数，使用双边等效费率保守近似，并在结果中明确披露。
    effective_commission = commission + slippage
    common = dict(
        cash=cash,
        commission=effective_commission,
        exclusive_orders=False,
        finalize_trades=True,
        trade_on_close=False,
    )
    strategy_parameters = {
        "fixed_stop": {
            "stop_pct": parameters["stop_pct"],
            "max_position": parameters["max_position"],
        },
        "moving_average": {
            "fast": parameters["fast"],
            "slow": parameters["slow"],
            "max_position": parameters["max_position"],
        },
        "trend_filter": {
            "fast": parameters["fast"],
            "slow": parameters["slow"],
            "max_position": parameters["max_position"],
        },
        "batch_buy": {
            "stop_pct": parameters["stop_pct"],
            "max_position": parameters["max_position"],
            "batch_drop": parameters["batch_drop"],
            "take_profit": parameters["take_profit"],
        },
    }[strategy_name]
    baseline_stats = Backtest(data, BuyAndHold, **common).run(position_fraction=0.999)
    strategy_stats = Backtest(data, STRATEGY_CLASSES[strategy_name], **common).run(
        **strategy_parameters
    )
    baseline_trades = _trade_details(
        baseline_stats, "baseline", parameters, final_bar=len(data) - 1
    )
    strategy_trades = _trade_details(
        strategy_stats, strategy_name, parameters, final_bar=len(data) - 1
    )
    baseline_curve = _curve(baseline_stats)
    strategy_curve = _curve(strategy_stats)
    baseline_metrics = _metrics(baseline_stats, baseline_trades, cash)
    strategy_metrics = _metrics(strategy_stats, strategy_trades, cash)

    calendar_days = (pd.Timestamp(data.index[-1]) - pd.Timestamp(data.index[0])).days
    warnings = [
        "未完整模拟 A 股 T+1、涨跌停、停牌和成交量限制，结果仅用于研究。",
        "滑点通过增加等效双边费率近似，不代表真实逐笔成交冲击。",
    ]
    if calendar_days < 1095:
        warnings.append("回测区间不足 3 年，可能未覆盖完整市场周期，结论稳定性较低。")
    sufficient = strategy_metrics["trade_count"] >= 30
    if not sufficient:
        warnings.append("策略完成交易少于 30 笔，样本不足，不能直接形成有效结论。")
    difference = strategy_metrics["total_return_pct"] - baseline_metrics["total_return_pct"]
    conclusion = (
        f"本次{STRATEGY_NAMES[strategy_name]}完成 {strategy_metrics['trade_count']} 笔交易，"
        f"总收益 {strategy_metrics['total_return_pct']:.2f}%，相对买入并持有"
        f"{'高' if difference >= 0 else '低'} {abs(difference):.2f} 个百分点，"
        f"最大回撤 {strategy_metrics['max_drawdown_pct']:.2f}%。"
    )
    conclusion += (
        "交易样本达到 30 笔，但仍需结合不同市场阶段继续验证。"
        if sufficient
        else "由于少于 30 笔，本结果只能视为样本不足的描述，不能证明规则有效。"
    )
    return {
        "engine": "backtesting.py",
        "strategy": {"code": strategy_name, "name": STRATEGY_NAMES[strategy_name]},
        "strategy_rules": _rules(strategy_name, parameters),
        "baseline": {
            "code": "buy_and_hold",
            "name": "买入并持有",
            "definition": "在首个可成交日以接近满仓买入，并持有到回测结束；使用与策略相同的手续费和滑点近似。",
            "metrics": baseline_metrics,
            **baseline_metrics,
        },
        "disciplined": {
            "name": STRATEGY_NAMES[strategy_name],
            "metrics": strategy_metrics,
            **strategy_metrics,
        },
        "commission": commission,
        "slippage": slippage,
        "configuration": {
            "initial_cash": round(cash, 2),
            "position_size_pct": round(parameters["max_position"] * 100, 2),
            "commission_pct": round(commission * 100, 4),
            "slippage_pct": round(slippage * 100, 4),
            "effective_round_trip_note": "手续费与滑点按每次成交收取；滑点采用等效费率近似。",
            "adjustment": adjustment,
            "data_frequency": "日线",
            "date_from": pd.Timestamp(data.index[0]).date().isoformat(),
            "date_to": pd.Timestamp(data.index[-1]).date().isoformat(),
            "calendar_days": calendar_days,
            "parameters": parameters,
        },
        "sample": {
            "trade_count": strategy_metrics["trade_count"],
            "minimum_required": 30,
            "sufficient": sufficient,
            "label": "样本充足" if sufficient else "样本不足",
        },
        "market_rules": MARKET_RULES,
        "curves": {"baseline": baseline_curve, "strategy": strategy_curve},
        "trades": strategy_trades,
        "warnings": warnings,
        "warning": " ".join(warnings),
        "conclusion": conclusion,
    }
