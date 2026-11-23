"""PAPAS 7.0 풍차돌리기 실전 관리 앱 (Streamlit + Supabase).

매매 규칙은 strategy.py에 있고, 백테스트 메뉴도 같은 규칙을 쓴다.
이 앱은 주문을 실행하지 않는다. 오늘 넣을 주문을 계산해 보여주고, 사용자가 입력한 체결을 기록한다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from io import BytesIO

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from supabase import Client, create_client

import strategy as S

st.set_page_config(page_title="PAPAS 7.0 | Windmill", page_icon="📈", layout="wide")
KST = S.ZoneInfo("Asia/Seoul")


# ================================================================ 공통 도구
def money(x) -> str:
    try:
        v = float(x)
        return f"${v:,.2f}" if np.isfinite(v) else "—"
    except Exception:
        return "—"


def fnum(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def get_supabase() -> Client:
    try:
        url = st.secrets.get("SUPABASE_URL", "")
        key = st.secrets.get("SUPABASE_ANON_KEY", "")
    except Exception:
        url = key = ""
    if not url or not key:
        st.error("Supabase 연결 정보가 없습니다. Streamlit Secrets에 SUPABASE_URL과 SUPABASE_ANON_KEY를 등록하세요.")
        st.stop()
    return create_client(url, key)


def db(fn, what: str):
    """DB 호출. 로그인 만료면 한 번 갱신을 시도하고, 그래도 안 되면 로그인 화면으로 보낸다."""
    try:
        return fn()
    except Exception as e:
        msg = str(e)
        if "JWT" in msg or "expired" in msg.lower() or "401" in msg:
            try:
                st.session_state.sb.auth.refresh_session()
                return fn()
            except Exception:
                st.session_state.auth_user = None
                st.warning("로그인이 만료되었습니다. 다시 로그인하세요.")
                st.rerun()
        st.error(f"{what} 중 오류가 발생했습니다: {msg}")
        st.stop()


def flash(msg: str):
    st.session_state["flash"] = msg


# ================================================================ 로그인
if "sb" not in st.session_state:
    st.session_state.sb = get_supabase()
sb: Client = st.session_state.sb
st.session_state.setdefault("auth_user", None)

if st.session_state.auth_user is None:
    st.title("📈 PAPAS 7.0")
    st.caption("WINDMILL TRADING MANAGEMENT")
    tab1, tab2 = st.tabs(["로그인", "회원가입"])
    with tab1:
        with st.form("login"):
            email = st.text_input("이메일")
            password = st.text_input("비밀번호", type="password")
            ok = st.form_submit_button("로그인", use_container_width=True)
        if ok:
            try:
                res = sb.auth.sign_in_with_password({"email": email.strip(), "password": password})
                st.session_state.auth_user = res.user
                st.rerun()
            except Exception as e:
                st.error(f"로그인 실패: {e}")
    with tab2:
        with st.form("signup"):
            email2 = st.text_input("가입 이메일")
            pw2 = st.text_input("비밀번호 (6자 이상)", type="password")
            ok2 = st.form_submit_button("회원가입", use_container_width=True)
        if ok2:
            try:
                sb.auth.sign_up({"email": email2.strip(), "password": pw2})
                st.success("가입 요청을 완료했습니다. 이메일 인증이 켜져 있으면 메일의 링크를 누른 뒤 로그인하세요.")
            except Exception as e:
                st.error(f"회원가입 실패: {e}")
    st.stop()

uid = st.session_state.auth_user.id


# ================================================================ 데이터 읽기
def load_portfolio() -> dict:
    rows = db(lambda: sb.table("portfolios").select("*").eq("user_id", uid).order("created_at").execute().data,
              "계좌 정보 조회") or []
    if rows:
        return rows[0]
    created = db(lambda: sb.table("portfolios").insert({"user_id": uid}).execute().data, "기본 계좌 생성")
    return created[0]


portfolio = load_portfolio()
pid = portfolio["id"]
symbol = (portfolio.get("symbol") or "SOXL").upper()
rules = S.Rules.from_portfolio(portfolio)
schedule = [fnum(x) for x in (portfolio.get("interval_schedule") or S.DEFAULT_INTERVALS)] or S.DEFAULT_INTERVALS
per_default = fnum(portfolio.get("per_trade_qty"), 80)
count_default = int(fnum(portfolio.get("sell_count"), 5))

stages = db(lambda: sb.table("stages").select("*").eq("portfolio_id", pid).order("stage_no").execute().data,
            "Stage 조회") or []
txs = db(lambda: sb.table("transactions").select("*").eq("portfolio_id", pid)
         .order("trade_date").order("created_at").execute().data, "거래 조회") or []
mks = db(lambda: sb.table("makeups").select("*").eq("portfolio_id", pid).execute().data, "메이크업 조회") or []

txdf = S._prep_tx(pd.DataFrame(txs))
stage_by_id = {s["id"]: s for s in stages}
stage_name = {s["id"]: s["name"] for s in stages}
confirmed: dict[str, set] = {}
for m in mks:
    if m.get("confirmed") and m.get("phase"):
        confirmed.setdefault(m["stage_id"], set()).add(int(m["phase"]))


# ================================================================ 가격·신호
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_prices(ticker: str) -> pd.DataFrame:
    data = yf.download(ticker, period="max", auto_adjust=True, progress=False, group_by="column")
    if data is None or data.empty:
        raise ValueError("가격 데이터를 받지 못했습니다.")
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data[["Open", "High", "Low", "Close"]].dropna().copy()
    data.index = pd.to_datetime(data.index).tz_localize(None)
    return data


def load_prices(ticker: str) -> tuple[pd.DataFrame | None, str]:
    """yfinance로 받고 DB에 보관. 실패하면 DB에 보관된 가격을 쓴다."""
    try:
        df = fetch_prices(ticker)
        flag = f"cache_synced_{ticker}"
        if not st.session_state.get(flag):
            try:
                last = sb.table("price_cache").select("day").eq("user_id", uid).eq("symbol", ticker) \
                    .order("day", desc=True).limit(1).execute().data
                since = pd.Timestamp(last[0]["day"]) - pd.Timedelta(days=10) if last else None
                new = df if since is None else df[df.index >= since]
                rows = [{"user_id": uid, "symbol": ticker, "day": d.date().isoformat(), "open": float(r.Open),
                         "high": float(r.High), "low": float(r.Low), "close": float(r.Close)}
                        for d, r in new.iterrows()]
                for i in range(0, len(rows), 500):
                    sb.table("price_cache").upsert(rows[i:i + 500]).execute()
                st.session_state[flag] = True
            except Exception:
                pass  # 보관 실패는 화면 동작에 영향 없음
        return df, f"Yahoo Finance · {df.index[-1].date()} 종가까지"
    except Exception as e:
        try:
            rows, start = [], 0
            while True:
                part = sb.table("price_cache").select("day,open,high,low,close").eq("user_id", uid) \
                    .eq("symbol", ticker).order("day").range(start, start + 999).execute().data or []
                rows += part
                if len(part) < 1000:
                    break
                start += 1000
            if rows:
                df = pd.DataFrame(rows).rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
                df.index = pd.to_datetime(df.pop("day"))
                df = df.astype(float)
                return df, f"⚠️ 자동 조회 실패({e}) — 보관된 가격 사용 · {df.index[-1].date()} 종가까지"
        except Exception:
            pass
        return None, f"⚠️ 가격을 불러오지 못했습니다: {e}"


session = S.current_session()
last_closed = S.last_closed_session()
with st.spinner("가격과 신호를 계산하고 있습니다..."):
    prices, price_src = load_prices(symbol)
if prices is not None:
    weekly = S.weekly_bars(prices, last_closed)
    dailyb = S.daily_bars(prices, last_closed)
    signals = S.Signals.build(weekly, dailyb)
    ref_price = float(dailyb["Close"].iloc[-1]) if len(dailyb) else None
else:
    weekly = dailyb = None
    signals = None
    ref_price = None


def views_for(sess: date) -> dict:
    out = {}
    for s in stages:
        out[s["id"]] = S.stage_view(s, txdf[txdf.stage_id == s["id"]], signals, rules, sess,
                                    confirmed.get(s["id"]), ref_price)
    return out


views = views_for(session)
shares_now, cash_now = S.ledger_cash_shares(fnum(portfolio.get("initial_shares")),
                                            fnum(portfolio.get("initial_cash")), txdf)


# ================================================================ 사이드바
st.sidebar.title("📈 PAPAS 7.0")
st.sidebar.caption(f"{symbol} · 풍차돌리기 관리")
PAGES = ["오늘 할 일", "대시보드", "Stage 관리", "거래 원장", "신호·차트", "백테스트", "설정"]
page = st.sidebar.radio("메뉴", PAGES)
st.sidebar.caption(price_src)
if st.sidebar.button("가격 새로고침", use_container_width=True):
    fetch_prices.clear()
    st.rerun()
if st.sidebar.button("로그아웃", use_container_width=True):
    try:
        sb.auth.sign_out()
    except Exception:
        pass
    st.session_state.auth_user = None
    st.rerun()
if st.session_state.get("flash"):
    st.success(st.session_state.pop("flash"))


def insert_tx(d: date, stage_id, ttype: str, qty: float, price: float, fee: float, memo: str, round_no=None):
    if price <= 0 or qty <= 0:
        st.error("수량과 체결가는 0보다 커야 합니다.")
        return
    payload = {"user_id": uid, "portfolio_id": pid, "trade_date": d.isoformat(), "stage_id": stage_id,
               "trade_type": ttype, "round_no": int(round_no) if round_no else None,
               "quantity": float(qty), "price": float(price), "fee": float(fee), "memo": memo or None}
    db(lambda: sb.table("transactions").insert(payload).execute(), "거래 저장")
    flash(f"{stage_name.get(stage_id, '')} {ttype} {qty:,.4g}주 @ {money(price)} 저장했습니다.")
    st.rerun()


def session_kst_open(d: date) -> str:
    t = datetime.combine(d, time(9, 30), tzinfo=S.NY).astimezone(KST)
    return t.strftime("%m/%d %H:%M")


def orders_table(orders: list) -> pd.DataFrame:
    return pd.DataFrame([{
        "Stage": o.stage, "주문": o.kind, "유형": o.trade_type,
        "회차": f"{o.round_no}차" if o.round_no else "",
        "수량(주)": round(o.qty, 4), "가격": money(o.price) if o.price else ("종가" if o.kind != "매도 MOC" else "종가 MOC"),
        "금액": money(o.amount) if o.amount else "", "비고": o.note} for o in orders])


ACTIVE_DONE = ("완료", "종료(수동)")
MK2_RULES = ["weekly30", "weekly300", "daily30"]

# ================================================================ 오늘 할 일
if page == "오늘 할 일":
    st.title("오늘 할 일")
    st.caption(f"주문 대상 장: **{session} (뉴욕)** · 한국 시간 {session_kst_open(session)} 개장 · "
               f"신호 기준: {last_closed} 종가까지 확정")

    if weekly is not None and len(weekly):
        w = weekly.iloc[-1]
        c1, c2, c3 = st.columns(3)
        c1.metric("최근 확정 주봉", f"{w['week_end']}",
                  f"몸통 하단 {money(w['BODY_LOW'])}", delta_color="off")
        c2.metric("1차 조건: 몸통 하단 > 5주선", "충족" if w["cond1"] else "미충족",
                  f"5주선 {money(w['MA5'])}", delta_color="off")
        if rules.mk2_rule == "daily30":
            d = dailyb.iloc[-1]
            c3.metric("2차 조건: 일봉 몸통 > 30일선", "충족" if d["cond_d30"] else "미충족",
                      f"30일선 {money(d['MA30D'])}", delta_color="off")
        elif rules.mk2_rule == "weekly300":
            trend = "상승·횡보" if w["MA30"] >= w["MA30_prev"] else "하락"
            c3.metric("2차 조건: 30주선↑ + 몸통 > 300주선", "충족" if w["cond2"] else "미충족",
                      f"30주선 {trend} · 300주선 {money(w['MA300'])}", delta_color="off")
        else:
            trend = "상승·횡보" if w["MA30"] >= w["MA30_prev"] else "하락"
            c3.metric("2차 조건: 30주선↑ + 몸통 > 30주선", "충족" if w["cond2_30"] else "미충족",
                      f"30주선 {money(w['MA30'])} · {trend}", delta_color="off")
    else:
        st.warning("가격 데이터가 없어 메이크업 신호를 판정하지 못했습니다. 매도·재매수 지시는 정상입니다.")

    all_orders = [o for v in views.values() for o in v.orders]
    need_cash = sum(o.amount or 0 for o in all_orders if o.trade_type in S.BUY_TYPES)
    st.subheader(f"주문 목록 ({len(all_orders)}건)")
    if all_orders:
        st.dataframe(orders_table(all_orders), use_container_width=True, hide_index=True)
        st.caption(f"매수 주문이 모두 체결되면 필요한 현금 {money(need_cash)} · 현재 현금 {money(cash_now)}"
                   + (" — ⚠️ 현금이 부족합니다. 오래된 Stage부터 체결하세요." if need_cash > cash_now + 0.01 else ""))
    else:
        st.info("오늘 넣을 주문이 없습니다.")

    st.subheader("Stage별 상세 · 체결 입력")
    shown = [s for s in stages if views[s["id"]].phase not in ACTIVE_DONE]
    if not shown:
        st.info("진행 중인 Stage가 없습니다. Stage 관리에서 새 Stage를 만드세요.")
    for s in shown:
        v = views[s["id"]]
        with st.container(border=True):
            st.markdown(f"**{s['name']}** · {v.phase} · 매도 {v.sold_qty:,.0f}/{v.planned_qty:,.0f}주"
                        + (f" · 기준가 {money(v.base)}" if v.base else ""))
            for wmsg in v.warnings:
                st.warning(wmsg)
            for mk in (v.mk1, v.mk2):
                if mk.status == "신호 발생 · 확인 필요":
                    basis = (f"{mk.budget:,.1f}주" if rules.basis == "quantity" else money(mk.budget))
                    cols = st.columns([3, 1])
                    cols[0].info(f"{mk.phase}차 메이크업 신호: {mk.trigger} 확정. "
                                 f"{mk.days[0]}~{mk.days[-1]} 5거래일 종가 LOC, 예산 {basis}")
                    if cols[1].button(f"{mk.phase}차 신호 확인", key=f"cf{s['id']}{mk.phase}"):
                        db(lambda: sb.table("makeups").upsert(
                            {"user_id": uid, "portfolio_id": pid, "stage_id": s["id"], "phase": mk.phase,
                             "signal_date": mk.trigger.isoformat(), "confirmed": True},
                            on_conflict="stage_id,phase").execute(), "신호 확인 저장")
                        flash(f"{s['name']} {mk.phase}차 메이크업 신호를 확인했습니다.")
                        st.rerun()
                elif mk.trigger:
                    st.caption(f"{mk.phase}차 메이크업: {mk.status} · 신호 {mk.trigger}")
            if v.orders:
                labels = [f"{i + 1}. {o.kind} {o.round_no or ''}{'차' if o.round_no else ''} · {o.qty:,.4g}주"
                          + (f" @ {money(o.price)}" if o.price and o.kind == '재매수 지정가' else "") for i, o in enumerate(v.orders)]
                labels.append("직접 입력")
            else:
                labels = ["직접 입력"]
            pick = st.selectbox("체결한 주문", labels, key=f"pk{s['id']}")
            o = v.orders[labels.index(pick)] if pick != "직접 입력" else None
            with st.form(f"fill{s['id']}"):
                c = st.columns(5)
                k = s["id"]
                fd = c[0].date_input("체결일(뉴욕)", value=session, key=f"fd{k}")
                tt = c[1].selectbox("유형", S.TRADE_TYPES, index=S.TRADE_TYPES.index(o.trade_type) if o else 0,
                                 key=f"tt{k}{pick}")
                rn = c[2].number_input("회차(재매수)", min_value=0, max_value=30, value=int(o.round_no or 0) if o else 0,
                                       key=f"rn{k}{pick}")
                fq = c[3].number_input("수량", min_value=0.0, value=float(round(o.qty, 4)) if o else float(per_default),
                                       step=1.0, key=f"fq{k}{pick}")
                fp = c[4].number_input("체결가", min_value=0.0, value=float(round(o.price, 2)) if (o and o.price) else 0.0,
                                       step=0.01, format="%.4f", key=f"fp{k}{pick}")
                c2 = st.columns([1, 3])
                ff = c2[0].number_input("수수료", min_value=0.0, value=0.0, step=0.01, format="%.2f", key=f"ff{k}")
                fm = c2[1].text_input("메모", value="", key=f"fm{k}")
                if st.form_submit_button("체결 저장", use_container_width=True):
                    insert_tx(fd, s["id"], tt, fq, fp, ff, fm, rn if tt == S.BUYBACK and rn > 0 else None)

# ================================================================ 대시보드
elif page == "대시보드":
    st.title("종합현황")
    st.caption(f"{portfolio['name']} · {symbol}")
    sold_all = float(txdf[txdf.trade_type == S.SELL].quantity.sum())
    buy_all = float(txdf[txdf.trade_type.isin(S.BUY_TYPES)].quantity.sum())
    profit_all = sum(v.profit for v in views.values())
    out_qty = sum(max(0.0, v.sold_qty - v.buy_qty) for s_id, v in views.items() if v.phase not in ACTIVE_DONE)
    strat_qty = fnum(portfolio.get("initial_shares")) * fnum(portfolio.get("strategy_ratio"), 0.5)
    c = st.columns(4)
    c[0].metric("현재 보유수량", f"{shares_now:,.2f}주")
    c[1].metric("현금 잔액", money(cash_now))
    c[2].metric("평가액(주식+현금)", money(shares_now * ref_price + cash_now) if ref_price else "—",
                f"최근 종가 {money(ref_price)}" if ref_price else None, delta_color="off")
    c[3].metric("가격차이 이익 합계*", money(profit_all))
    c = st.columns(4)
    c[0].metric("누적 매도", f"{sold_all:,.0f}주")
    c[1].metric("누적 매수", f"{buy_all:,.2f}주")
    c[2].metric("아직 되사지 않은 수량", f"{out_qty:,.0f}주", f"전략 적용 한도 {strat_qty:,.0f}주", delta_color="off")
    c[3].metric("진행 중 Stage", f"{sum(1 for v in views.values() if v.phase not in ACTIVE_DONE)} / {len(stages)}")
    if out_qty > strat_qty + S.EPS:
        st.warning(f"진행 중 Stage에서 되사지 않은 수량({out_qty:,.0f}주)이 전략 적용 한도({strat_qty:,.0f}주)를 넘었습니다.")
    if shares_now < -S.EPS:
        st.error("보유수량이 0보다 작습니다. 초기 보유수량이나 거래 기록을 확인하세요.")

    if stages:
        rows = []
        for s in stages:
            v = views[s["id"]]
            rows.append({"Stage": s["name"], "단계": v.phase, "출발일": s.get("start_date"),
                         "간격": f"{fnum(s.get('buy_interval')) * 100:g}%", "매도수량": v.sold_qty,
                         "기준가": round(v.base, 2) if v.base else None,
                         "재매수 체결": f"{len(v.filled_rounds)}/{len(v.levels) or int(s.get('sell_count') or 5)}",
                         "매수수량": round(v.buy_qty, 2), "가격차이 이익*": round(v.profit, 2),
                         "1차 메이크업": v.mk1.status, "2차 메이크업": v.mk2.status})
        st.subheader("Stage별 진행 현황")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption("* 가격차이 이익 = (Stage 기준가 − 실제 매수가) × 매수수량. 수수료·세금·환율·기존 보유분 평가손익은 제외한 참고값입니다.")

    if prices is not None and len(txdf):
        eq = S.equity_curve(fnum(portfolio.get("initial_shares")), fnum(portfolio.get("initial_cash")),
                            txdf, prices["Close"])
        if len(eq):
            st.subheader("계좌 평가액 추이")
            fig = go.Figure()
            fig.add_scatter(x=eq.index, y=eq["Equity"], name="평가액", mode="lines")
            fig.add_scatter(x=eq.index, y=eq["Cash"], name="현금", mode="lines")
            fig.update_layout(height=360, margin=dict(l=10, r=10, t=10, b=10), yaxis_tickprefix="$")
            st.plotly_chart(fig, use_container_width=True)
            st.caption(f"첫 거래일 이후 최대 낙폭(MDD) {eq['Drawdown'].min() * 100:.2f}% · 수정주가 종가 기준")
    elif not len(txdf):
        st.info("아직 거래 기록이 없습니다. Stage 관리에서 Stage를 만들고 오늘 할 일에서 체결을 입력하세요.")

# ================================================================ Stage 관리
elif page == "Stage 관리":
    st.title("Stage 관리")
    nxt = (max([int(s["stage_no"]) for s in stages]) + 1) if stages else 1
    with st.expander("새 Stage 추가", expanded=not stages):
        with st.form("new_stage"):
            c = st.columns(3)
            name = c[0].text_input("Stage 이름", value=ordinal(nxt))
            stage_no = c[1].number_input("순서 번호", min_value=1, value=nxt)
            start = c[2].date_input("출발일(첫 매도일, 뉴욕)", value=session)
            c = st.columns(3)
            gap = c[0].number_input("재매수 간격 (%)", min_value=0.1, max_value=50.0,
                                    value=round(S.default_interval(nxt, schedule) * 100, 2), step=0.5)
            per = c[1].number_input("1회 매도·매수 수량", min_value=1.0, value=per_default, step=1.0)
            cnt = c[2].number_input("매도 횟수(거래일)", min_value=1, max_value=30, value=count_default)
            st.caption(f"간격 기본값은 설정의 순서별 간격표를 따릅니다: "
                       + " → ".join(f"{x * 100:g}%" for x in schedule) + ", 이후 마지막 값")
            if st.form_submit_button("Stage 생성"):
                days = S.trading_days_from(start, int(cnt))
                db(lambda: sb.table("stages").insert({
                    "user_id": uid, "portfolio_id": pid, "name": name.strip(), "stage_no": int(stage_no),
                    "start_date": days[0].isoformat(), "sell_start": days[0].isoformat(), "sell_end": days[-1].isoformat(),
                    "sell_days": [d.isoformat() for d in days], "buy_interval": gap / 100, "per_trade_qty": per,
                    "sell_count": int(cnt), "sell_target": per * int(cnt)}).execute(), "Stage 생성")
                flash(f"{name} 생성 · 매도일 " + ", ".join(d.strftime('%m/%d') for d in days))
                st.rerun()

    for s in stages:
        v = views[s["id"]]
        with st.expander(f"{s['name']} · {v.phase} · 매도 {v.sold_qty:,.0f}/{v.planned_qty:,.0f}주", expanded=False):
            for wmsg in v.warnings:
                st.warning(wmsg)
            c = st.columns(4)
            c[0].metric("기준가(평균 매도가)", money(v.base) if v.base else "—")
            c[1].metric("재매수 체결", f"{len(v.filled_rounds)}/{len(v.levels) or int(s.get('sell_count') or 5)}회")
            c[2].metric("가격차이 이익*", money(v.profit))
            spent_ratio = v.buy_spend / v.proceeds * 100 if v.proceeds else 0
            c[3].metric("매도대금 대비 재투입", f"{spent_ratio:.1f}%")
            st.caption("매도일: " + ", ".join(d.strftime("%m/%d") for d in v.sell_days)
                       + f" · 간격 {fnum(s.get('buy_interval')) * 100:g}% · 1차 메이크업 {v.mk1.status}"
                       + f" · 2차 메이크업 {v.mk2.status}")

            left, right = st.columns(2)
            with left:
                st.write("**매도 기록**")
                if len(v.sells):
                    sv = v.sells[["trade_date", "quantity", "price", "amount"]].copy()
                    sv["누적 평균"] = sv["amount"].cumsum() / sv["quantity"].cumsum()
                    sv.columns = ["매도일", "수량", "매도가", "매도금액", "누적 평균"]
                    st.dataframe(sv.round(2), use_container_width=True, hide_index=True)
                else:
                    st.caption("아직 매도 기록이 없습니다.")
            with right:
                st.write("**매수 기록 (기준가 대비)**")
                rows = []
                for n, lv, q in v.levels:
                    f = v.filled_rounds.get(n)
                    px = fnum(f["price"]) if f else None
                    rows.append({"구분": f"재매수 {n}차", "지정가": round(lv, 2), "실제 매수가": px,
                                 "수량": fnum(f["quantity"]) if f else None,
                                 "매수일": f["trade_date"] if f else None,
                                 "이익*": round((v.base - px) * fnum(f["quantity"]), 2) if f else None,
                                 "이익률*": f"{(v.base - px) / px * 100:.2f}%" if f else None})
                for _, r in v.buys[v.buys.trade_type.isin([S.MK1, S.MK2])].iterrows():
                    rows.append({"구분": r["trade_type"], "지정가": None, "실제 매수가": r["price"],
                                 "수량": r["quantity"], "매수일": r["trade_date"],
                                 "이익*": round((v.base - r["price"]) * r["quantity"], 2) if v.base else None,
                                 "이익률*": f"{(v.base - r['price']) / r['price'] * 100:.2f}%" if v.base else None})
                if rows:
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.caption("매도가 끝나면 지정가가 계산됩니다.")

            with st.form(f"edit{s['id']}"):
                st.write("Stage 설정 수정")
                c = st.columns(5)
                k = s["id"]
                en = c[0].text_input("이름", value=s["name"], key=f"en{k}")
                eno = c[1].number_input("순서", min_value=1, value=int(s["stage_no"]), key=f"eno{k}")
                est = c[2].date_input("출발일", value=pd.to_datetime(s.get("start_date") or session).date(), key=f"est{k}")
                eg = c[3].number_input("간격 (%)", min_value=0.1, max_value=50.0,
                                       value=float(fnum(s.get("buy_interval"), 0.03) * 100), step=0.5, key=f"eg{k}")
                eq_ = c[4].number_input("1회 수량", min_value=1.0, value=float(fnum(s.get("per_trade_qty"), 80)),
                                        step=1.0, key=f"eq{k}")
                if st.form_submit_button("저장"):
                    cnt = int(s.get("sell_count") or 5)
                    days = S.trading_days_from(est, cnt)
                    db(lambda: sb.table("stages").update({
                        "name": en.strip(), "stage_no": int(eno), "start_date": days[0].isoformat(),
                        "sell_start": days[0].isoformat(), "sell_end": days[-1].isoformat(),
                        "sell_days": [d.isoformat() for d in days], "buy_interval": eg / 100,
                        "per_trade_qty": eq_, "sell_target": eq_ * cnt}).eq("id", s["id"]).execute(), "Stage 수정")
                    flash(f"{en} 설정을 저장했습니다.")
                    st.rerun()
            c = st.columns([1, 1, 2])
            if s.get("closed_at"):
                if c[0].button("다시 진행", key=f"re{s['id']}"):
                    db(lambda: sb.table("stages").update({"closed_at": None}).eq("id", s["id"]).execute(), "Stage 수정")
                    st.rerun()
            elif c[0].button("수동 종료", key=f"cl{s['id']}"):
                db(lambda: sb.table("stages").update({"closed_at": date.today().isoformat()}).eq("id", s["id"]).execute(),
                   "Stage 종료")
                st.rerun()
            sure = c[2].checkbox("이 Stage를 삭제합니다(거래 기록은 남고 Stage 연결만 해제)", key=f"sure{s['id']}")
            if c[1].button("삭제", key=f"del{s['id']}", disabled=not sure):
                db(lambda: sb.table("stages").delete().eq("id", s["id"]).execute(), "Stage 삭제")
                flash(f"{s['name']}을(를) 삭제했습니다.")
                st.rerun()

# ================================================================ 거래 원장
elif page == "거래 원장":
    st.title("거래 원장")
    opts = {"(연결 안 함)": None} | {s["name"]: s["id"] for s in stages}
    with st.form("add_tx"):
        c = st.columns(4)
        td = c[0].date_input("거래일(뉴욕)", value=min(session, date.today()))
        typ = c[1].selectbox("거래 유형", S.TRADE_TYPES)
        sn = c[2].selectbox("Stage", list(opts.keys()), index=1 if len(opts) > 1 else 0)
        rn = c[3].number_input("재매수 회차 (없으면 0)", min_value=0, max_value=30, value=0)
        c = st.columns(4)
        qty = c[0].number_input("수량", min_value=0.0, value=float(per_default), step=1.0)
        price = c[1].number_input("체결가 (USD)", min_value=0.0, value=0.0, step=0.01, format="%.4f")
        fee = c[2].number_input("수수료 (USD)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        memo = c[3].text_input("메모")
        if st.form_submit_button("거래 저장", use_container_width=True):
            insert_tx(td, opts[sn], typ, qty, price, fee, memo, rn if typ == S.BUYBACK and rn > 0 else None)

    st.subheader("거래 내역")
    if len(txdf):
        view = txdf.copy()
        view["Stage"] = view["stage_id"].map(stage_name).fillna("")
        view["회차"] = view["round_no"].map(lambda x: f"{int(x)}차" if pd.notna(x) else "")
        show = view[["trade_date", "Stage", "trade_type", "회차", "quantity", "price", "amount", "fee", "memo"]]
        show.columns = ["거래일", "Stage", "유형", "회차", "수량", "체결가", "금액", "수수료", "메모"]
        st.dataframe(show.iloc[::-1].round(4), use_container_width=True, hide_index=True)
        labels = {r["id"]: f"{r['trade_date']} · {stage_name.get(r['stage_id'], '-')} · {r['trade_type']} "
                           f"{r['quantity']:,.4g}주 @ {money(r['price'])}" for _, r in view.iloc[::-1].iterrows()}
        with st.form("delete_tx"):
            did = st.selectbox("삭제할 거래", list(labels.keys()), format_func=lambda k: labels[k])
            ok = st.checkbox("이 거래를 삭제합니다.")
            if st.form_submit_button("선택 거래 삭제"):
                if ok:
                    db(lambda: sb.table("transactions").delete().eq("id", did).execute(), "거래 삭제")
                    flash("거래를 삭제했습니다.")
                    st.rerun()
                else:
                    st.warning("삭제하려면 확인란을 체크하세요.")
        exp = show.copy()
        st.download_button("거래 내역 CSV 다운로드", exp.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{symbol}_transactions.csv", mime="text/csv")
    else:
        st.info("거래 내역이 없습니다.")

    with st.expander("CSV 가져오기 (엑셀 이관)"):
        st.caption("Stage CSV 열: name, stage_no, start_date, buy_interval, per_trade_qty, sell_count · "
                   "거래 CSV 열: stage, trade_date, trade_type, round_no, quantity, price, fee, memo. "
                   "이름이 같은 Stage는 새로 만들지 않고 연결합니다.")
        fs = st.file_uploader("Stage CSV (선택)", type="csv", key="up_st")
        ft = st.file_uploader("거래 CSV", type="csv", key="up_tx")
        dup = st.checkbox("같은 거래를 두 번 가져오지 않는지 확인했습니다.")
        if st.button("가져오기", disabled=not (ft and dup)):
            names = {s["name"]: s["id"] for s in stages}
            made = 0
            if fs:
                sdf = pd.read_csv(fs, encoding="utf-8-sig")
                for _, r in sdf.iterrows():
                    if str(r["name"]) in names:
                        continue
                    cnt = int(r.get("sell_count", 5) or 5)
                    days = S.trading_days_from(pd.to_datetime(r["start_date"]).date(), cnt)
                    per = float(r.get("per_trade_qty", per_default) or per_default)
                    res = db(lambda: sb.table("stages").insert({
                        "user_id": uid, "portfolio_id": pid, "name": str(r["name"]), "stage_no": int(r["stage_no"]),
                        "start_date": days[0].isoformat(), "sell_start": days[0].isoformat(),
                        "sell_end": days[-1].isoformat(), "sell_days": [d.isoformat() for d in days],
                        "buy_interval": float(r["buy_interval"]), "per_trade_qty": per, "sell_count": cnt,
                        "sell_target": per * cnt}).execute().data, "Stage 가져오기")
                    names[str(r["name"])] = res[0]["id"]
                    made += 1
            tdf = pd.read_csv(ft, encoding="utf-8-sig")
            bad = [x for x in tdf["trade_type"].unique() if x not in S.TRADE_TYPES]
            if bad:
                st.error(f"알 수 없는 거래 유형: {bad}. 매도/재매수/1차 메이크업/2차 메이크업만 가능합니다.")
                st.stop()
            rows = [{"user_id": uid, "portfolio_id": pid, "stage_id": names.get(str(r["stage"])),
                     "trade_date": pd.to_datetime(r["trade_date"]).date().isoformat(), "trade_type": r["trade_type"],
                     "round_no": int(r["round_no"]) if pd.notna(r.get("round_no")) and str(r.get("round_no")) != "" else None,
                     "quantity": float(r["quantity"]), "price": float(r["price"]),
                     "fee": float(r.get("fee", 0) or 0), "memo": None if pd.isna(r.get("memo")) else str(r.get("memo"))}
                    for _, r in tdf.iterrows()]
            db(lambda: sb.table("transactions").insert(rows).execute(), "거래 가져오기")
            flash(f"Stage {made}개, 거래 {len(rows)}건을 가져왔습니다.")
            st.rerun()

# ================================================================ 신호·차트
elif page == "신호·차트":
    st.title("신호·차트")
    st.caption(price_src)
    if weekly is None or not len(weekly):
        st.warning("가격 데이터가 없습니다.")
        st.stop()
    yrs = st.select_slider("주봉 기간", options=[1, 2, 3, 5, 10], value=3, format_func=lambda x: f"{x}년")
    wv = weekly[weekly.index >= weekly.index[-1] - pd.DateOffset(years=yrs)]
    fig = go.Figure()
    fig.add_candlestick(x=wv.index, open=wv.Open, high=wv.High, low=wv.Low, close=wv.Close, name="주봉")
    for col, nm in (("MA5", "5주선"), ("MA30", "30주선"), ("MA300", "300주선")):
        fig.add_scatter(x=wv.index, y=wv[col], name=nm, mode="lines")
    s1 = wv[wv.cond1 & ~wv.cond1.shift(1, fill_value=False)]
    fig.add_scatter(x=s1.index, y=s1.Low * 0.95, mode="markers", name="1차 조건 시작",
                    marker=dict(symbol="triangle-up", size=10))
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10),
                      yaxis_tickprefix="$")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("확정된 주봉만 표시합니다. 진행 중인 주는 금요일 장 마감 후 반영됩니다.")

    dv = dailyb.iloc[-130:]
    fig2 = go.Figure()
    fig2.add_candlestick(x=dv.index, open=dv.Open, high=dv.High, low=dv.Low, close=dv.Close, name="일봉")
    fig2.add_scatter(x=dv.index, y=dv.MA30D, name="30일선", mode="lines")
    fig2.update_layout(height=360, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=10, b=10),
                       yaxis_tickprefix="$")
    st.subheader("일봉 (최근 6개월)")
    st.plotly_chart(fig2, use_container_width=True)

    st.subheader("최근 주봉 신호")
    h = weekly.iloc[-20:].iloc[::-1]
    tbl = pd.DataFrame({
        "주(금)": h.week_end, "확인일": h.signal_date, "몸통 하단": h.BODY_LOW.round(2), "5주선": h.MA5.round(2),
        "1차 조건": np.where(h.cond1, "충족", "-"), "30주선": h.MA30.round(2), "전주 30주선": h.MA30_prev.round(2),
        "2차 조건(30주선)": np.where(h.cond2_30, "충족", "-"),
        "300주선": h.MA300.round(2), "2차 조건(300주선)": np.where(h.cond2, "충족", "-")})
    st.dataframe(tbl, use_container_width=True, hide_index=True)

# ================================================================ 백테스트
elif page == "백테스트":
    st.title("백테스트")
    st.caption("실전 앱과 같은 계산 모듈(strategy.py)을 씁니다. 메이크업 규칙은 설정 값이 기본입니다.")
    with st.form("bt"):
        c = st.columns(4)
        bt_ticker = c[0].text_input("티커", value=symbol).strip().upper()
        bt_start = c[1].date_input("시작일", value=date(2022, 1, 3))
        bt_end = c[2].date_input("종료일", value=last_closed)
        mode = c[3].radio("매도 규모", ["보유 비율", "고정 수량"], horizontal=True)
        c = st.columns(4)
        sh0 = c[0].number_input("초기 보유 수량", min_value=0.0001, value=120.0, step=1.0)
        avg0 = c[1].number_input("초기 평단가", min_value=0.01, value=30.88, step=0.01)
        fixed = c[2].number_input("고정 매도 수량(5일 합계)", min_value=1.0, value=float(per_default * 5), step=1.0)
        c = st.columns(3)
        r1 = c[0].selectbox("1차 규칙", ["above", "cross"], index=["above", "cross"].index(rules.mk1_rule),
                            format_func=lambda k: S.RULE_LABELS[k])
        r2 = c[1].selectbox("2차 규칙", MK2_RULES, index=MK2_RULES.index(rules.mk2_rule) if rules.mk2_rule in MK2_RULES else 0,
                            format_func=lambda k: S.RULE_LABELS[k])
        r3 = c[2].selectbox("메이크업 크기", ["amount", "quantity"], index=["amount", "quantity"].index(rules.basis),
                            format_func=lambda k: S.RULE_LABELS[k])
        run = st.form_submit_button("백테스트 실행", type="primary", use_container_width=True)
    ratios = [0.10, 0.25, 0.50, 1.00]
    gaps = [x / 200 for x in range(2, 21)]
    if run:
        if bt_start > bt_end:
            st.error("시작일은 종료일보다 빠르거나 같아야 합니다.")
            st.stop()
        try:
            with st.spinner("가격 수집 및 전체 조합 계산 중..."):
                px = fetch_prices(bt_ticker) if bt_ticker != symbol or prices is None else prices
                px = px[px.index.date <= bt_end]
                sig = S.Signals.build(S.weekly_bars(px, bt_end), S.daily_bars(px, bt_end))
                rr = S.Rules(mk1_rule=r1, mk2_rule=r2, basis=r3)
                res, det = [], {}
                keys = [(r, g) for r in ratios for g in gaps] if mode == "보유 비율" else [(None, g) for g in gaps]
                for r, g in keys:
                    out = S.run_backtest(px, sig, bt_start, bt_end, sh0, avg0, g, rr,
                                         sell_ratio=r, sell_qty=None if r else fixed)
                    det[(r, g)] = out
                    res.append({k: v for k, v in out.items() if k not in ("거래내역", "일별자산")})
                summ = pd.DataFrame(res).sort_values("수익률(%)", ascending=False).reset_index(drop=True)
            st.session_state.bt = dict(summary=summ, details=det, ticker=bt_ticker, start=bt_start, end=bt_end,
                                       prices=px)
        except Exception as e:
            st.session_state.pop("bt", None)
            st.error(f"실행 오류: {e}")
    if "bt" in st.session_state:
        b = st.session_state.bt
        summ, det = b["summary"], b["details"]
        st.success(f"{b['ticker']} · {b['start']} ~ {b['end']} · {len(summ)}개 시나리오")
        best = summ.iloc[0]
        c = st.columns(4)
        c[0].metric("수익률 1위", f"{best['수익률(%)']:.2f}%", f"매도 {best['매도비율']} / 간격 {best['재매수간격']}")
        c[1].metric("보유만 했을 때", f"{best['보유만 했을 때 수익률(%)']:.2f}%")
        c[2].metric("최종자산", money(best["최종자산($)"]))
        c[3].metric("MDD", f"{best['MDD(%)']:.2f}%", f"보유만 {best['보유만 했을 때 MDD(%)']:.2f}%", delta_color="off")
        st.dataframe(summ.drop(columns=["기준매도가($)", "마지막종가($)"]).round(2), use_container_width=True,
                     hide_index=True)
        st.subheader("수익률 비교표(%)")
        st.dataframe(summ.pivot(index="매도비율", columns="재매수간격", values="수익률(%)").round(2),
                     use_container_width=True)
        st.subheader("개별 전략 상세")
        keys = list(det.keys())
        k = st.selectbox("전략", keys, format_func=lambda k: (f"매도 {k[0] * 100:.0f}%" if k[0] else "고정 수량")
                         + f" · 간격 {k[1] * 100:.1f}%")
        d = det[k]
        c = st.columns(4)
        c[0].metric("최종 자산", money(d["최종자산($)"]))
        c[1].metric("최종 보유 수량", f"{d['최종주식수']:,.4f}")
        c[2].metric("현금", money(d["최종현금($)"]))
        c[3].metric("MDD", f"{d['MDD(%)']:.2f}%")
        st.dataframe(d["거래내역"], use_container_width=True, hide_index=True)
        st.line_chart(d["일별자산"]["Equity"])
        out = BytesIO()
        with pd.ExcelWriter(out, engine="openpyxl") as w:
            summ.to_excel(w, sheet_name="전략비교", index=False)
            d["거래내역"].to_excel(w, sheet_name="선택전략_매매내역", index=False)
            d["일별자산"].to_excel(w, sheet_name="선택전략_일별자산")
        st.download_button("결과 엑셀 다운로드", out.getvalue(),
                           file_name=f"{b['ticker']}_papas7_backtest_{b['start']}_{b['end']}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        with st.expander("계산 가정"):
            st.markdown("""
- 첫 5거래일 종가로 매도 물량을 5등분해 매도하고, 평균 매도가를 기준가로 씁니다.
- 1~5차 재매수 지정가 = 기준가 × (1 − 간격)^n. 저가가 지정가 이하이면 체결, 시가가 더 낮으면 시가 체결로 봅니다.
- 메이크업은 신호 다음 거래일부터 5거래일 종가 LOC로 나눠 삽니다. 신호는 확정된 주봉(또는 일봉)만 씁니다.
- 2차 예산은 매도대금 − 쓴 매수금액 − 진행 중인 1차 잔여예산입니다(원본 백테스트기의 예산 중복 오류 수정).
- 수익률 기준은 '초기 보유수량 × 평단가'입니다. '보유만 했을 때'와 비교해 전략 효과를 보세요.
- 배당·세금·환전·수수료·슬리피지는 반영하지 않습니다. 300주선은 약 6년 치 주봉이 있어야 계산됩니다.
""")

# ================================================================ 설정
elif page == "설정":
    st.title("계좌 및 전략 설정")
    with st.form("settings"):
        c = st.columns(3)
        pname = c[0].text_input("계좌 이름", value=portfolio.get("name") or "")
        psym = c[1].text_input("종목 티커", value=symbol)
        pcash = c[2].number_input("초기 현금 (USD)", min_value=0.0, value=fnum(portfolio.get("initial_cash")), step=100.0)
        c = st.columns(3)
        pshares = c[0].number_input("초기 보유수량", min_value=0.0, value=fnum(portfolio.get("initial_shares")), step=1.0)
        pratio = c[1].number_input("전략 적용 비율 (%)", min_value=0.0, max_value=100.0,
                                   value=fnum(portfolio.get("strategy_ratio"), 0.5) * 100, step=1.0)
        pper = c[2].number_input("기본 1회 매도·매수 수량", min_value=1.0, value=per_default, step=1.0)
        sched = st.text_input("Stage 순서별 재매수 간격 (%, 쉼표로 구분)",
                              value=", ".join(f"{x * 100:g}" for x in schedule))
        st.write("**메이크업 규칙**")
        m1 = st.radio("1차 메이크업 신호", ["above", "cross"], index=["above", "cross"].index(rules.mk1_rule),
                      format_func=lambda k: S.RULE_LABELS[k])
        m2 = st.radio("2차 메이크업 신호", MK2_RULES,
                      index=MK2_RULES.index(rules.mk2_rule) if rules.mk2_rule in MK2_RULES else 0,
                      format_func=lambda k: S.RULE_LABELS[k])
        m3 = st.radio("메이크업 크기", ["amount", "quantity"], index=["amount", "quantity"].index(rules.basis),
                      format_func=lambda k: S.RULE_LABELS[k])
        if st.form_submit_button("설정 저장"):
            try:
                sc = [float(x) / 100 for x in sched.replace("%", "").split(",") if x.strip()]
                assert sc and all(0 < x < 0.5 for x in sc)
            except Exception:
                st.error("간격표는 '5, 4.5, 4, 3.5, 3'처럼 0~50 사이 숫자를 쉼표로 구분해 입력하세요.")
                st.stop()
            db(lambda: sb.table("portfolios").update({
                "name": pname, "symbol": psym.strip().upper(), "initial_cash": pcash, "initial_shares": pshares,
                "strategy_ratio": pratio / 100, "per_trade_qty": pper, "interval_schedule": sc,
                "makeup1_rule": m1, "makeup2_rule": m2, "makeup_basis": m3}).eq("id", pid).execute(), "설정 저장")
            flash("설정을 저장했습니다.")
            st.rerun()
    st.divider()
    st.warning("이 앱은 주문 계산과 거래 기록용입니다. 증권사 주문을 실행하지 않습니다. "
               "가격은 Yahoo Finance 수정주가를 쓰며 지연·오류가 있을 수 있으니 신호는 차트로 한 번 더 확인하세요.")
