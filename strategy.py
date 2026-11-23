"""PAPAS 7.0 풍차돌리기 계산 모듈.

백테스트와 실전 앱이 이 파일 하나를 함께 쓴다. 화면(Streamlit)·DB(Supabase) 코드는 넣지 않는다.

규칙(백테스트기 기준)
1. 최초 매도: 출발일부터 5거래일, 매일 종가 MOC로 1/5씩
2. 기준가: 총 매도대금 ÷ 매도수량
3. 등비 재매수: n차 지정가 = 기준가 × (1 − 간격)^n, 매도 기간이 끝난 다음 거래일부터
4. 1차 메이크업: 확정 주봉 몸통 하단 > 5주선 → 다음 거래일부터 5일 종가 LOC,
   예산 = 매도대금 × 50% − 이미 쓴 매수금액
5. 2차 메이크업: 1차 이후, 30주선이 전주보다 낮지 않고 몸통 하단 > 300주선 →
   다음 거래일부터 5일 종가 LOC, 예산 = 매도대금 − 쓴 매수금액 − 진행 중인 1차 잔여예산
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- 거래 유형
SELL = "매도"
BUYBACK = "재매수"
MK1 = "1차 메이크업"
MK2 = "2차 메이크업"
TRADE_TYPES = [SELL, BUYBACK, MK1, MK2]
BUY_TYPES = [BUYBACK, MK1, MK2]

DEFAULT_INTERVALS = [0.05, 0.045, 0.04, 0.035, 0.03]
EPS = 1e-9


def default_interval(stage_no: int, schedule: list[float] | None = None) -> float:
    """Stage 순서별 기본 재매수 간격. 목록을 넘어가면 마지막 값."""
    schedule = schedule or DEFAULT_INTERVALS
    i = max(0, int(stage_no) - 1)
    return float(schedule[i] if i < len(schedule) else schedule[-1])


# ---------------------------------------------------------------- 규칙 설정
@dataclass
class Rules:
    mk1_rule: str = "above"      # above: 5주선 위에 있음(백테스트) / cross: 아래에 있다가 회복
    mk2_rule: str = "weekly300"  # weekly300: 30주선 횡보·상승+300주선 위(백테스트) / daily30: 일봉 30일선 회복(엑셀)
    basis: str = "amount"        # amount: 매도대금 기준(백테스트) / quantity: 매도수량 기준(엑셀)
    mk1_ratio: float = 0.5
    mk_days: int = 5

    @classmethod
    def from_portfolio(cls, p: dict) -> "Rules":
        return cls(
            mk1_rule=p.get("makeup1_rule") or "above",
            mk2_rule=p.get("makeup2_rule") or "weekly300",
            basis=p.get("makeup_basis") or "amount",
        )


RULE_LABELS = {
    "above": "5주선 위에 있음 (백테스트 방식)",
    "cross": "5주선 아래에 있다가 회복",
    "weekly300": "30주선 횡보·상승 + 몸통 > 300주선 (백테스트 방식)",
    "daily30": "일봉 몸통 > 30일선 (엑셀 방식)",
    "amount": "금액 기준: 매도대금의 50%·100% (백테스트 방식)",
    "quantity": "수량 기준: 매도수량의 50%·100% (엑셀 방식)",
}


# ---------------------------------------------------------------- 미국 거래일 달력
def _easter(y: int) -> date:
    a = y % 19
    b, c = divmod(y, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(y, month, day + 1)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> date:
    d = date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> date:
    d = date(y, month + 1, 1) - timedelta(days=1) if month < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


# 정기 휴장일 외 임시 휴장(허리케인·국장)
_SPECIAL_CLOSURES = {date(2012, 10, 29), date(2012, 10, 30), date(2018, 12, 5), date(2025, 1, 9)}


@lru_cache(maxsize=None)
def nyse_holidays(y: int) -> frozenset:
    h = set()
    ny = date(y, 1, 1)
    if ny.weekday() == 6:
        h.add(ny + timedelta(days=1))
    elif ny.weekday() != 5:  # 토요일이면 전날(12/31)을 쉬지 않는다
        h.add(ny)
    h.add(_nth_weekday(y, 1, 0, 3))   # 마틴 루터 킹
    h.add(_nth_weekday(y, 2, 0, 3))   # 대통령의 날
    h.add(_easter(y) - timedelta(days=2))  # 성금요일
    h.add(_last_weekday(y, 5, 0))     # 메모리얼 데이
    if y >= 2022:
        h.add(_observed(date(y, 6, 19)))  # 준틴스
    h.add(_observed(date(y, 7, 4)))
    h.add(_nth_weekday(y, 9, 0, 1))   # 노동절
    h.add(_nth_weekday(y, 11, 3, 4))  # 추수감사절
    h.add(_observed(date(y, 12, 25)))
    h |= {d for d in _SPECIAL_CLOSURES if d.year == y}
    return frozenset(h)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def next_trading_day(d: date, include: bool = False) -> date:
    d = d if include else d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date, include: bool = False) -> date:
    d = d if include else d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_from(start: date, n: int) -> list[date]:
    """start(거래일이면 포함)부터 n거래일."""
    out, d = [], next_trading_day(start, include=True)
    while len(out) < n:
        out.append(d)
        d = next_trading_day(d)
    return out


def ny_now() -> datetime:
    return datetime.now(NY)


def current_session(now: datetime | None = None) -> date:
    """지금 주문을 넣으면 체결될 장의 날짜(뉴욕 기준)."""
    now = (now or ny_now()).astimezone(NY)
    d = now.date()
    if is_trading_day(d) and now.time() < time(16, 0):
        return d
    return next_trading_day(d)


def last_closed_session(now: datetime | None = None) -> date:
    """종가가 확정된 가장 최근 거래일(뉴욕 기준)."""
    now = (now or ny_now()).astimezone(NY)
    d = now.date()
    if is_trading_day(d) and now.time() >= time(16, 15):
        return d
    return prev_trading_day(d)


# ---------------------------------------------------------------- 지표·신호
def _clean_daily(daily: pd.DataFrame) -> pd.DataFrame:
    df = daily[["Open", "High", "Low", "Close"]].dropna().copy()
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    return df[~df.index.duplicated(keep="last")].sort_index()


def weekly_bars(daily: pd.DataFrame, last_closed: date | None = None) -> pd.DataFrame:
    """금요일 기준 주봉 + 5·30·300주선 + 신호. last_closed를 주면 그날까지 확정된 주봉만 남긴다."""
    d = _clean_daily(daily)
    w = d.resample("W-FRI").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    last_day = d.index.to_series().resample("W-FRI").max().reindex(w.index)
    w["MA5"] = w["Close"].rolling(5).mean()
    w["MA30"] = w["Close"].rolling(30).mean()
    w["MA300"] = w["Close"].rolling(300).mean()
    w["MA30_prev"] = w["MA30"].shift(1)
    w["BODY_LOW"] = w[["Open", "Close"]].min(axis=1)
    w["BODY_HIGH"] = w[["Open", "Close"]].max(axis=1)
    w["signal_date"] = [x.date() for x in last_day]
    w["week_end"] = [x.date() for x in w.index]
    w["cond1"] = (w["BODY_LOW"] > w["MA5"]).fillna(False)
    w["cond2"] = ((w["MA30"] >= w["MA30_prev"]) & (w["BODY_LOW"] > w["MA300"])).fillna(False)
    if last_closed is not None:
        done = [
            (sd <= last_closed) and (we <= last_closed or next_trading_day(sd) > we)
            for sd, we in zip(w["signal_date"], w["week_end"])
        ]
        w = w[np.array(done, dtype=bool)]
    return w


def daily_bars(daily: pd.DataFrame, last_closed: date | None = None) -> pd.DataFrame:
    """일봉 + 30일선. 엑셀 방식 2차 신호(cond_d30)."""
    d = _clean_daily(daily)
    d["MA30D"] = d["Close"].rolling(30).mean()
    d["BODY_LOW"] = d[["Open", "Close"]].min(axis=1)
    d["cond_d30"] = (d["BODY_LOW"] > d["MA30D"]).fillna(False)
    d["day"] = [x.date() for x in d.index]
    if last_closed is not None:
        d = d[d["day"] <= last_closed]
    return d


@dataclass
class Signals:
    """확정된 신호 모음. 날짜는 '신호를 확인한 거래일'(주봉이면 그 주 마지막 거래일)."""
    mk1: dict          # date -> bool (주봉 몸통 하단 > 5주선)
    mk2_weekly: dict   # date -> bool
    mk2_daily: dict    # date -> bool

    @classmethod
    def build(cls, weekly: pd.DataFrame, daily: pd.DataFrame | None = None) -> "Signals":
        mk1 = dict(zip(weekly["signal_date"], weekly["cond1"].astype(bool)))
        mk2w = dict(zip(weekly["signal_date"], weekly["cond2"].astype(bool)))
        mk2d = dict(zip(daily["day"], daily["cond_d30"].astype(bool))) if daily is not None else {}
        return cls(mk1, mk2w, mk2d)

    def mk1_trigger(self, after: date, first_sell: date, rule: str, before: date | None = None) -> date | None:
        """after(마지막 매도일) 이후 처음 나오는 1차 신호일. before(주문할 장) 전에 확정된 신호만 본다."""
        seen_below = False
        for d in sorted(self.mk1):
            if before is not None and d >= before:
                break
            ok = self.mk1[d]
            if d >= after and ok and (rule == "above" or seen_below):
                return d
            if d >= first_sell and not ok:
                seen_below = True
        return None

    def mk2_trigger(self, after: date, rule: str, before: date | None = None) -> date | None:
        src = self.mk2_daily if rule == "daily30" else self.mk2_weekly
        for d in sorted(src):
            if before is not None and d >= before:
                break
            if d >= after and src[d]:
                return d
        return None


# ---------------------------------------------------------------- 실전: Stage 상태 계산
@dataclass
class Order:
    stage: str
    kind: str        # 매도 MOC / 재매수 지정가 / 메이크업 LOC
    trade_type: str
    qty: float
    price: float | None  # 지정가(LOC는 참고 종가)
    amount: float | None
    round_no: int | None = None
    note: str = ""


@dataclass
class MakeupView:
    phase: int
    trigger: date | None = None
    days: list = field(default_factory=list)
    budget: float = 0.0          # 금액 기준이면 $, 수량 기준이면 주
    spent: float = 0.0
    status: str = "신호 대기"

    @property
    def remaining(self) -> float:
        return max(0.0, self.budget - self.spent)


@dataclass
class StageView:
    stage: dict
    phase: str
    sold_qty: float
    proceeds: float
    base: float | None
    planned_qty: float
    sell_days: list
    levels: list            # [(round, price, qty)]
    filled_rounds: dict     # round -> tx row(dict)
    buy_spend: float
    buy_qty: float
    profit: float           # (기준가 − 매수가) × 수량 합계
    mk1: MakeupView
    mk2: MakeupView
    orders: list
    sells: pd.DataFrame
    buys: pd.DataFrame
    warnings: list


def _prep_tx(txs: pd.DataFrame) -> pd.DataFrame:
    cols = ["id", "trade_date", "stage_id", "trade_type", "round_no", "quantity", "price", "fee", "memo"]
    if txs is None or len(txs) == 0:
        return pd.DataFrame({c: pd.Series(dtype="float64" if c in ("quantity", "price", "fee", "round_no")
                                          else "object") for c in cols + ["amount"]})
    t = txs.copy()
    for c in cols:
        if c not in t.columns:
            t[c] = None
    t["trade_date"] = pd.to_datetime(t["trade_date"]).dt.date
    for c in ["quantity", "price", "fee"]:
        t[c] = pd.to_numeric(t[c], errors="coerce").fillna(0.0)
    t["round_no"] = pd.to_numeric(t["round_no"], errors="coerce")
    t["amount"] = t["quantity"] * t["price"]
    return t.sort_values(["trade_date"], kind="stable")


def _as_dates(v) -> list[date]:
    if not v:
        return []
    return [pd.to_datetime(x).date() for x in v]


def stage_sell_days(stage: dict) -> list[date]:
    days = _as_dates(stage.get("sell_days"))
    if days:
        return days
    start = stage.get("start_date") or stage.get("sell_start")
    if not start:
        return []
    return trading_days_from(pd.to_datetime(start).date(), int(stage.get("sell_count") or 5))


def stage_view(stage: dict, txs: pd.DataFrame, signals: Signals | None, rules: Rules,
               session: date, confirmed: set | None = None, ref_price: float | None = None) -> StageView:
    """한 Stage의 진행 단계, 오늘(session) 넣을 주문, 엑셀식 표를 계산한다.

    confirmed: 사용자가 확인한 메이크업 단계 {1, 2}. 확인 전에는 메이크업 주문을 내지 않는다.
    ref_price: 메이크업 LOC 수량 환산용 최근 종가.
    """
    confirmed = confirmed or set()
    t = _prep_tx(txs)
    name = stage.get("name", "")
    per = float(stage.get("per_trade_qty") or 0)
    count = int(stage.get("sell_count") or 5)
    gap = float(stage.get("buy_interval") or 0.03)
    planned = per * count
    sdays = stage_sell_days(stage)
    first_sell = sdays[0] if sdays else None
    last_sell = sdays[-1] if sdays else None
    warnings: list[str] = []

    sells = t[t.trade_type == SELL]
    buys = t[t.trade_type.isin(BUY_TYPES)]
    sold = float(sells.quantity.sum())
    proceeds = float(sells.amount.sum())
    base = proceeds / sold if sold > EPS else None
    sell_done = sold >= planned - EPS or (last_sell is not None and session > last_sell)
    if sold > planned + EPS:
        warnings.append(f"매도수량 {sold:,.0f}주가 계획 {planned:,.0f}주보다 많습니다.")
    past_days = [d for d in sdays if d < session]
    if not stage.get("closed_at") and len(sells) < len(past_days) and sold < planned - EPS:
        warnings.append(f"지난 매도일 {len(past_days)}일 중 {len(sells)}건만 입력되었습니다. 체결을 입력하세요.")
    # 매도를 마친 날(계획 수량을 다 팔았으면 실제 마지막 매도일, 아니면 계획상 마지막 매도일)
    if sold >= planned - EPS and len(sells):
        after = max(sells.trade_date)
    else:
        after = last_sell or (max(sells.trade_date) if len(sells) else None)

    # 재매수 회차
    n_rounds = count
    qty_each = (sold / count) if sell_done and sold > EPS else per
    levels = [(n, base * (1 - gap) ** n, qty_each) for n in range(1, n_rounds + 1)] if base else []
    filled: dict[int, dict] = {}
    bb = t[t.trade_type == BUYBACK]
    unnumbered = []
    for _, r in bb.iterrows():
        rn = r["round_no"]
        if pd.notna(rn) and 1 <= int(rn) <= n_rounds and int(rn) not in filled:
            filled[int(rn)] = r.to_dict()
        else:
            unnumbered.append(r.to_dict())
    for r in unnumbered:  # 회차 없는 기존 기록은 빈 회차를 앞에서부터 채운다
        free = [n for n in range(1, n_rounds + 1) if n not in filled]
        if free:
            filled[free[0]] = r

    buy_spend = float(buys.amount.sum())
    buy_qty = float(buys.quantity.sum())
    profit = float(((base - buys.price) * buys.quantity).sum()) if base and len(buys) else 0.0

    def spent_until(d: date, types=BUY_TYPES, qty=False) -> float:
        x = t[t.trade_type.isin(types) & (t.trade_date <= d)]
        return float(x.quantity.sum() if qty else x.amount.sum())

    use_qty = rules.basis == "quantity"
    mk1 = MakeupView(1)
    mk2 = MakeupView(2)

    if base and sell_done and signals is not None and after is not None:
        trig1 = signals.mk1_trigger(after, first_sell or after, rules.mk1_rule, before=session)
        if trig1:
            mk1.trigger = trig1
            mk1.days = trading_days_from(next_trading_day(trig1), rules.mk_days)
            if use_qty:
                mk1.budget = max(0.0, sold * rules.mk1_ratio - spent_until(trig1, qty=True))
                mk1.spent = float(t[t.trade_type == MK1].quantity.sum())
            else:
                mk1.budget = max(0.0, proceeds * rules.mk1_ratio - spent_until(trig1))
                mk1.spent = float(t[t.trade_type == MK1].amount.sum())
            trig2 = signals.mk2_trigger(trig1, rules.mk2_rule, before=session)
            if trig2:
                mk2.trigger = trig2
                mk2.days = trading_days_from(next_trading_day(trig2), rules.mk_days)
                # 2차 시작 시점에 아직 진행 중인 1차 잔여예산은 빼고 계산(백테스트 버그 수정)
                mk1_running = mk1.days and trig2 < mk1.days[-1]
                if use_qty:
                    mk1_left = max(0.0, mk1.budget - float(
                        t[(t.trade_type == MK1) & (t.trade_date <= trig2)].quantity.sum())) if mk1_running else 0.0
                    mk2.budget = max(0.0, sold - spent_until(trig2, qty=True) - mk1_left)
                    mk2.spent = float(t[t.trade_type == MK2].quantity.sum())
                else:
                    mk1_left = max(0.0, mk1.budget - float(
                        t[(t.trade_type == MK1) & (t.trade_date <= trig2)].amount.sum())) if mk1_running else 0.0
                    mk2.budget = max(0.0, proceeds - spent_until(trig2) - mk1_left)
                    mk2.spent = float(t[t.trade_type == MK2].amount.sum())

    for mk in (mk1, mk2):
        if mk.trigger is None:
            mk.status = "신호 대기"
        elif mk.budget <= 0.01:
            mk.status = "예산 없음(이미 확보)"
        elif mk.remaining <= 0.01:
            mk.status = "완료"
        elif session > mk.days[-1]:
            mk.status = "기간 종료"
        elif mk.phase not in confirmed:
            mk.status = "신호 발생 · 확인 필요"
        elif session < mk.days[0]:
            mk.status = "다음 거래일 시작"
        else:
            mk.status = f"진행 중 ({mk.days.index(session) + 1}/{len(mk.days)}일)" if session in mk.days else "진행 중"

    # 단계 판정
    closed = bool(stage.get("closed_at"))
    full = (buy_qty >= sold - EPS) if use_qty else (buy_spend >= proceeds * 0.995)
    if closed:
        phase = "종료(수동)"
    elif not sell_done:
        phase = "출발 전" if (sold <= EPS and first_sell and session < first_sell) else "매도 중"
    elif sold <= EPS:
        phase = "매도 미입력"
    elif full:
        phase = "완료"
    elif mk2.trigger and (mk2.status not in ("기간 종료", "완료", "예산 없음(이미 확보)")):
        phase = "2차 메이크업"
    elif mk2.trigger:
        phase = "완료"
    elif mk1.trigger and mk1.status not in ("기간 종료", "완료", "예산 없음(이미 확보)"):
        phase = "1차 메이크업"
    elif mk1.trigger:
        phase = "2차 대기"
    elif len(filled) >= n_rounds:
        phase = "재매수 완료 · 메이크업 대기"
    else:
        phase = "재매수 대기"

    # 오늘 주문
    orders: list[Order] = []
    active = phase not in ("종료(수동)", "완료")
    if active and session in sdays and sold < planned - EPS:
        already = float(sells[sells.trade_date == session].quantity.sum())
        if already <= EPS:
            q = min(per, planned - sold)
            orders.append(Order(name, "매도 MOC", SELL, q, None, None,
                                note=f"{sdays.index(session) + 1}/{len(sdays)}일째"))
    if active and base and sell_done and (after is None or session > after):
        for n, px, q in levels:
            if n not in filled:
                orders.append(Order(name, "재매수 지정가", BUYBACK, q, px, q * px, round_no=n,
                                    note=f"기준가 ${base:,.2f} × (1−{gap * 100:g}%)^{n}"))
    for mk, ttype in ((mk1, MK1), (mk2, MK2)):
        if active and mk.trigger and mk.phase in confirmed and mk.days and session in mk.days and mk.remaining > 0.01:
            left_days = len(mk.days) - mk.days.index(session)
            per_day = mk.remaining / left_days
            if use_qty:
                q = per_day
                amt = q * ref_price if ref_price else None
            else:
                amt = per_day
                q = amt / ref_price if ref_price else 0.0
            orders.append(Order(name, "메이크업 LOC", ttype, q, ref_price, amt,
                                note=f"{mk.days.index(session) + 1}/{len(mk.days)}일째 · 남은 "
                                     + (f"{mk.remaining:,.1f}주" if use_qty else f"${mk.remaining:,.2f}")))

    return StageView(stage, phase, sold, proceeds, base, planned, sdays, levels, filled,
                     buy_spend, buy_qty, profit, mk1, mk2, orders, sells, buys, warnings)


# ---------------------------------------------------------------- 실전: 계좌 합계
def ledger_cash_shares(initial_shares: float, initial_cash: float, txs: pd.DataFrame) -> tuple[float, float]:
    t = _prep_tx(txs)
    s = t[t.trade_type == SELL]
    b = t[t.trade_type.isin(BUY_TYPES)]
    shares = initial_shares - float(s.quantity.sum()) + float(b.quantity.sum())
    cash = initial_cash + float((s.amount - s.fee).sum()) - float((b.amount + b.fee).sum())
    return shares, cash


def equity_curve(initial_shares: float, initial_cash: float, txs: pd.DataFrame,
                 closes: pd.Series) -> pd.DataFrame:
    """거래일별 보유수량·현금·평가액. closes: 날짜 인덱스의 종가."""
    t = _prep_tx(txs)
    if t.empty or closes is None or closes.empty:
        return pd.DataFrame(columns=["Close", "Shares", "Cash", "Equity", "Drawdown"])
    c = closes.copy()
    c.index = pd.to_datetime(c.index).normalize()
    if getattr(c.index, "tz", None) is not None:
        c.index = c.index.tz_localize(None)
    start = pd.Timestamp(min(t.trade_date))
    c = c[c.index >= start]
    sign = np.where(t.trade_type == SELL, -1.0, 1.0)
    t = t.assign(dq=sign * t.quantity,
                 dc=np.where(t.trade_type == SELL, t.amount - t.fee, -(t.amount + t.fee)))
    g = t.groupby(pd.to_datetime(t.trade_date))[["dq", "dc"]].sum()
    g = g.reindex(c.index.union(g.index), fill_value=0).cumsum()
    out = pd.DataFrame({"Close": c})
    out["Shares"] = initial_shares + g["dq"].reindex(out.index).ffill().fillna(0)
    out["Cash"] = initial_cash + g["dc"].reindex(out.index).ffill().fillna(0)
    out["Equity"] = out["Shares"] * out["Close"] + out["Cash"]
    out["Drawdown"] = out["Equity"] / out["Equity"].cummax() - 1
    return out


# ---------------------------------------------------------------- 백테스트
def run_backtest(daily_all: pd.DataFrame, signals: Signals, start: date, end: date,
                 shares0: float, avg0: float, gap: float, rules: Rules,
                 sell_ratio: float | None = None, sell_qty: float | None = None) -> dict:
    """백테스트기 run_one을 옮긴 것. 차이: 1·2차 규칙 선택, 수량 기준 선택, 2차 예산 버그 수정, 확정 주봉만 사용."""
    d_all = _clean_daily(daily_all)
    daily = d_all[(d_all.index.date >= start) & (d_all.index.date <= end)]
    if daily.empty:
        raise ValueError("선택한 기간에 거래 데이터가 없습니다.")
    shares = float(shares0)
    cash = 0.0
    initial_equity = shares0 * avg0
    sold_qty = float(sell_qty) if sell_qty else shares0 * float(sell_ratio or 0)
    sold_qty = min(sold_qty, shares0)
    tranche = sold_qty / 5.0
    proceeds = 0.0
    sell_days = list(daily.index[:5])
    log, rows = [], []
    levels: list[float] = []
    base = None
    each = sold_qty / 5.0
    spend = 0.0
    bought_qty = 0.0
    done: set[int] = set()
    s1 = s2 = False
    queue: list[dict] = []
    first_sell = sell_days[0].date() if sell_days else start
    seen_below = False
    use_qty = rules.basis == "quantity"

    for dt, bar in daily.iterrows():
        dd = dt.date()
        if dt in sell_days:
            q = min(tranche, shares)
            if q > 0:
                px = float(bar["Close"])
                shares -= q
                cash += q * px
                proceeds += q * px
                log.append([dt, "최초 매도(MOC)", px, q, q * px, f"5일 분할 {sell_days.index(dt) + 1}/5"])
            if dt == sell_days[-1]:
                base = proceeds / sold_qty if sold_qty else None
                if base and base > 0:
                    levels = [base * ((1 - gap) ** n) for n in range(1, 6)]

        if base is not None and dt not in sell_days:
            for n, lv in enumerate(levels, start=1):
                if n in done:
                    continue
                if float(bar["Low"]) <= lv and cash > 0:
                    px = min(float(bar["Open"]), lv) if float(bar["Open"]) < lv else lv
                    q = min(each, cash / px)
                    if q > 0:
                        shares += q
                        cash -= q * px
                        spend += q * px
                        bought_qty += q
                        done.add(n)
                        log.append([dt, f"등비 재매수 {n}차", px, q, q * px, f"기준가 {base:.4f}, 간격 {gap * 100:.1f}%"])

        c1 = signals.mk1.get(dd)
        if c1 is not None and dd >= first_sell and not c1:
            seen_below = True
        if base is not None:
            if not s1 and c1 and (rules.mk1_rule == "above" or seen_below):
                s1 = True
                if use_qty:
                    need = max(0.0, sold_qty * rules.mk1_ratio - bought_qty)
                    if need > 0:
                        queue.append({"name": "1차 메이크업", "qty": need, "days": rules.mk_days})
                else:
                    budget = max(0.0, proceeds * rules.mk1_ratio - spend)
                    if budget > 0:
                        queue.append({"name": "1차 메이크업", "budget": min(budget, cash), "days": rules.mk_days})
            src = signals.mk2_daily if rules.mk2_rule == "daily30" else signals.mk2_weekly
            if not s2 and s1 and src.get(dd):
                s2 = True
                if use_qty:
                    need = max(0.0, sold_qty - bought_qty - sum(x.get("qty", 0) for x in queue))
                    if need > 0:
                        queue.append({"name": "2차 메이크업", "qty": need, "days": rules.mk_days})
                else:
                    budget = max(0.0, proceeds - spend - sum(x.get("budget", 0) for x in queue))
                    if budget > 0:
                        queue.append({"name": "2차 메이크업", "budget": min(budget, cash), "days": rules.mk_days})

        for item in list(queue):
            if item.get("start_after") is None:
                item["start_after"] = dt
                continue
            if dt <= item["start_after"]:
                continue
            if item["days"] > 0 and cash > 0:
                px = float(bar["Close"])
                if use_qty:
                    q = min(item["qty"] / item["days"], cash / px)
                else:
                    q = min(item["budget"] / item["days"], cash) / px
                if q > 0:
                    shares += q
                    cash -= q * px
                    spend += q * px
                    bought_qty += q
                    if use_qty:
                        item["qty"] -= q
                    else:
                        item["budget"] -= q * px
                    item["days"] -= 1
                    log.append([dt, item["name"], px, q, q * px, "다음 거래일부터 5일 종가 LOC 근사"])
            else:
                item["days"] = 0
            left = item["qty"] if use_qty else item["budget"]
            if item["days"] <= 0 or left <= (1e-6 if use_qty else 0.01):
                queue.remove(item)

        rows.append([dt, float(bar["Close"]), shares, cash, shares * float(bar["Close"]) + cash])

    res = pd.DataFrame(rows, columns=["Date", "Close", "Shares", "Cash", "Equity"]).set_index("Date")
    trades = pd.DataFrame(log, columns=["Date", "Action", "Price", "Quantity", "Amount", "Note"])
    last = float(daily["Close"].iloc[-1])
    final = shares * last + cash
    hold = shares0 * last
    mdd = ((res["Equity"] / res["Equity"].cummax()) - 1).min() * 100
    hold_eq = shares0 * res["Close"]
    hold_mdd = ((hold_eq / hold_eq.cummax()) - 1).min() * 100
    return {
        "매도비율": f"{sell_ratio * 100:.0f}%" if sell_ratio else f"{sold_qty:,.0f}주",
        "재매수간격": f"{gap * 100:.1f}%",
        "최종자산($)": final, "최종주식수": shares, "최종현금($)": cash,
        "실현 매도대금($)": proceeds, "누적 매수금액($)": spend,
        "평가손익+실현손익($)": final - initial_equity,
        "수익률(%)": (final / initial_equity - 1) * 100 if initial_equity else 0.0,
        "보유만 했을 때 수익률(%)": (hold / initial_equity - 1) * 100 if initial_equity else 0.0,
        "초과수익($)": final - hold,
        "MDD(%)": mdd, "보유만 했을 때 MDD(%)": hold_mdd,
        "등비재매수체결": len(done), "1차메이크업": "실행" if s1 else "미실행",
        "2차메이크업": "실행" if s2 else "미실행",
        "기준매도가($)": base if base else np.nan, "마지막종가($)": last,
        "거래내역": trades, "일별자산": res,
    }
