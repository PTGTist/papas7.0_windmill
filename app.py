import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import yfinance as yf
from supabase import create_client, Client

st.set_page_config(page_title="PAPAS 7.0 | Windmill", page_icon="📈", layout="wide")

# ---------- Configuration / Supabase ----------
def get_supabase():
    url = st.secrets.get("SUPABASE_URL", "")
    key = st.secrets.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        st.error("Supabase 연결 정보가 없습니다. Streamlit Secrets에 SUPABASE_URL과 SUPABASE_ANON_KEY를 등록하세요.")
        st.stop()
    return create_client(url, key)

def money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$0.00"

def num(x, digits=2):
    try:
        return f"{float(x):,.{digits}f}"
    except Exception:
        return "—"

def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def load_portfolio(sb, uid):
    r = sb.table("portfolios").select("*").eq("user_id", uid).order("created_at").execute()
    rows = r.data or []
    if not rows:
        payload = {"user_id": uid, "name": "SOXL 기본 계좌", "symbol": "SOXL", "initial_shares": 1600,
                   "strategy_ratio": 0.5, "initial_cash": 0, "currency": "USD"}
        created = sb.table("portfolios").insert(payload).execute()
        return created.data[0]
    return rows[0]

def query_rows(sb, table, portfolio_id):
    return sb.table(table).select("*").eq("portfolio_id", portfolio_id).order("created_at").execute().data or []

def get_stage_rows(sb, portfolio_id):
    return sb.table("stages").select("*").eq("portfolio_id", portfolio_id).order("stage_no").execute().data or []

def get_transactions(sb, portfolio_id):
    return sb.table("transactions").select("*").eq("portfolio_id", portfolio_id).order("trade_date").order("created_at").execute().data or []

def refresh():
    st.cache_data.clear()
    st.rerun()

# ---------- Login ----------
if "sb" not in st.session_state:
    st.session_state.sb = get_supabase()
sb: Client = st.session_state.sb

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None

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
                res = sb.auth.sign_in_with_password({"email": email, "password": password})
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
                res = sb.auth.sign_up({"email": email2, "password": pw2})
                st.success("가입 요청을 완료했습니다. Supabase 이메일 인증 설정에 따라 메일 인증 후 로그인하세요.")
            except Exception as e:
                st.error(f"회원가입 실패: {e}")
    st.stop()

user = st.session_state.auth_user
uid = user.id
try:
    portfolio = load_portfolio(sb, uid)
except Exception as e:
    st.error(f"계좌 정보를 불러오지 못했습니다. Supabase SQL을 먼저 실행했는지 확인하세요. ({e})")
    st.stop()

pid = portfolio["id"]
symbol = portfolio["symbol"]
st.sidebar.title("📈 PAPAS 7.0")
st.sidebar.caption(f"{symbol} · 풍차돌리기 관리")
page = st.sidebar.radio("메뉴", ["대시보드", "풍차돌리기", "거래 내역", "Make up", "전략 분석", "주가 분석", "설정"])
if st.sidebar.button("로그아웃", use_container_width=True):
    try: sb.auth.sign_out()
    except Exception: pass
    st.session_state.auth_user = None
    st.rerun()

stages = get_stage_rows(sb, pid)
txs = get_transactions(sb, pid)
txdf = pd.DataFrame(txs)
if txdf.empty:
    txdf = pd.DataFrame(columns=["id","trade_date","stage_id","trade_type","quantity","price","fee","memo"])
else:
    txdf["quantity"] = pd.to_numeric(txdf["quantity"], errors="coerce").fillna(0)
    txdf["price"] = pd.to_numeric(txdf["price"], errors="coerce").fillna(0)
    txdf["fee"] = pd.to_numeric(txdf["fee"], errors="coerce").fillna(0)

# Initial inventory is a seed balance, not a transaction.
initial_shares = safe_float(portfolio.get("initial_shares"))
initial_cash = safe_float(portfolio.get("initial_cash"))
strategy_ratio = safe_float(portfolio.get("strategy_ratio"), .5)
sell = txdf[txdf.trade_type == "매도"] if not txdf.empty else txdf
buys = txdf[txdf.trade_type.isin(["일반 매수","Make up 매수"])] if not txdf.empty else txdf
sell_qty = float(sell.quantity.sum()) if len(sell) else 0
buy_qty = float(buys.quantity.sum()) if len(buys) else 0
current_shares = initial_shares - sell_qty + buy_qty
cash_balance = initial_cash + float((sell.quantity*sell.price-sell.fee).sum()) - float((buys.quantity*buys.price+buys.fee).sum()) if len(txdf) else initial_cash
net_strategy_qty = initial_shares*strategy_ratio
current_price = None

def stage_metrics(stage):
    rows = [x for x in txs if x.get("stage_id") == stage["id"]]
    sdf = pd.DataFrame(rows)
    if sdf.empty:
        return {"sold":0,"sell_value":0,"avg_sell":0,"general_buy":0,"makeup_buy":0,"buy_value":0}
    sdf["quantity"] = pd.to_numeric(sdf["quantity"], errors="coerce").fillna(0)
    sdf["price"] = pd.to_numeric(sdf["price"], errors="coerce").fillna(0)
    sold = sdf[sdf.trade_type=="매도"]
    gb = sdf[sdf.trade_type=="일반 매수"]
    mb = sdf[sdf.trade_type=="Make up 매수"]
    sq = sold.quantity.sum()
    sv = (sold.quantity*sold.price).sum()
    return {"sold":float(sq),"sell_value":float(sv),"avg_sell":float(sv/sq) if sq else 0,
            "general_buy":float(gb.quantity.sum()),"makeup_buy":float(mb.quantity.sum()),
            "buy_value":float((gb.quantity*gb.price).sum()+(mb.quantity*mb.price).sum())}

def add_transaction(trade_date, stage_id, trade_type, quantity, price, fee, memo):
    payload = {"user_id":uid,"portfolio_id":pid,"trade_date":str(trade_date),"stage_id":stage_id or None,
               "trade_type":trade_type,"quantity":float(quantity),"price":float(price),"fee":float(fee),"memo":memo}
    sb.table("transactions").insert(payload).execute()
    st.success("거래 내역을 저장했습니다.")
    st.rerun()

# ---------- Dashboard ----------
if page == "대시보드":
    st.title("종합현황")
    st.caption(f"{portfolio['name']} · {symbol}")
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("현재 보유수량", f"{current_shares:,.0f}주")
    c2.metric("전략 적용수량", f"{net_strategy_qty:,.0f}주")
    c3.metric("누적 매도수량", f"{sell_qty:,.0f}주")
    c4.metric("현금 잔액", money(cash_balance))
    c5,c6,c7,c8 = st.columns(4)
    c5.metric("일반 재매수", f"{float(txdf[txdf.trade_type=='일반 매수'].quantity.sum()) if len(txdf) else 0:,.0f}주")
    c6.metric("Make up 매수", f"{float(txdf[txdf.trade_type=='Make up 매수'].quantity.sum()) if len(txdf) else 0:,.0f}주")
    c7.metric("매도 목표", f"{net_strategy_qty:,.0f}주")
    c8.metric("진행 Stage", f"{sum(1 for s in stages if stage_metrics(s)['sold']>0)} / {len(stages)}")
    if stages:
        sd=[]
        for s in stages:
            m=stage_metrics(s)
            sd.append({"Stage":s["name"],"매도수량":m["sold"],"일반매수":m["general_buy"],"Make up":m["makeup_buy"],"목표":s["sell_target"]})
        st.subheader("Stage별 진행 현황")
        st.dataframe(pd.DataFrame(sd), use_container_width=True, hide_index=True)
    if len(txdf):
        plot=txdf.copy()
        plot["거래일"]=pd.to_datetime(plot.trade_date)
        plot["현금증감"]=np.where(plot.trade_type=="매도",plot.quantity*plot.price-plot.fee,-(plot.quantity*plot.price+plot.fee))
        plot=plot.sort_values("거래일")
        plot["누적현금"]=initial_cash+plot["현금증감"].cumsum()
        st.subheader("현금 잔액 변화")
        st.plotly_chart(px.line(plot,x="거래일",y="누적현금",markers=True),use_container_width=True)
    else:
        st.info("아직 거래 내역이 없습니다. 풍차돌리기 메뉴에서 Stage를 만들고 거래 내역을 입력하세요.")

# ---------- Windmill stages ----------
elif page == "풍차돌리기":
    st.title("풍차돌리기 · Stage 관리")
    with st.expander("새 Stage 추가", expanded=not bool(stages)):
        with st.form("new_stage"):
            name = st.text_input("Stage 이름", value=f"{len(stages)+1}th")
            stage_no = st.number_input("정렬 번호", min_value=1, value=len(stages)+1)
            start_date = st.date_input("출발일", value=date.today())
            sell_start = st.date_input("매도 시작일", value=date.today(), key="sellstart")
            sell_end = st.date_input("매도 종료일", value=date.today(), key="sellend")
            interval = st.number_input("매수 간격 (%)", min_value=0.1, max_value=50.0, value=3.0, step=0.5)/100
            per_trade = st.number_input("1회 매도·매수 수량", min_value=1, value=80)
            sell_count = st.number_input("매도 횟수", min_value=1, max_value=30, value=5)
            submitted=st.form_submit_button("Stage 생성")
        if submitted:
            sb.table("stages").insert({"user_id":uid,"portfolio_id":pid,"name":name,"stage_no":int(stage_no),
                "start_date":str(start_date),"sell_start":str(sell_start),"sell_end":str(sell_end),
                "buy_interval":interval,"per_trade_qty":int(per_trade),"sell_count":int(sell_count),
                "sell_target":int(per_trade*sell_count)}).execute()
            st.success("Stage를 생성했습니다.")
            st.rerun()
    for s in stages:
        m=stage_metrics(s)
        with st.expander(f"{s['name']}  ·  매도 {m['sold']:,.0f}/{s['sell_target']:,.0f}주", expanded=False):
            a,b,c,d=st.columns(4)
            a.metric("매도 평균가",money(m["avg_sell"]) if m["avg_sell"] else "—")
            b.metric("일반 매수",f"{m['general_buy']:,.0f}주")
            c.metric("Make up",f"{m['makeup_buy']:,.0f}주")
            d.metric("매도 진행률",f"{min(100,m['sold']/max(1,s['sell_target'])*100):.1f}%")
            avg=m["avg_sell"]
            interval=safe_float(s["buy_interval"])
            n=int(s["sell_count"])
            if avg:
                levels=[avg*((1-interval)**(i+1)) for i in range(n)]
                st.write("**등비수열 매수 기준가**")
                st.dataframe(pd.DataFrame({"회차":[f"{i+1}차" for i in range(n)],"기준가":levels,"매수수량":[s["per_trade_qty"]]*n}),hide_index=True,use_container_width=True)
            st.caption(f"출발일 {s['start_date']} · 매도 기간 {s['sell_start']} ~ {s['sell_end']} · 간격 {interval*100:g}%")
            if m["sold"] > net_strategy_qty:
                st.warning("누적 매도수량이 전략 적용수량을 초과했습니다. 실제 수량을 확인하세요. (기록 입력은 허용됩니다.)")
            with st.form(f"stage_edit_{s['id']}"):
                st.write("Stage 설정 수정")
                ename=st.text_input("이름",value=s["name"],key=f"en{s['id']}")
                einterval=st.number_input("매수 간격 (%)",min_value=.1,max_value=50.0,value=float(interval*100),step=.5,key=f"ei{s['id']}")/100
                eqty=st.number_input("1회 수量",min_value=1,value=int(s["per_trade_qty"]),key=f"eq{s['id']}")
                ecount=st.number_input("매도·매수 횟수",min_value=1,max_value=30,value=int(s["sell_count"]),key=f"ec{s['id']}")
                save=st.form_submit_button("Stage 설정 저장")
            if save:
                sb.table("stages").update({"name":ename,"buy_interval":einterval,"per_trade_qty":eqty,"sell_count":ecount,"sell_target":eqty*ecount}).eq("id",s["id"]).execute()
                st.rerun()

# ---------- Transactions ----------
elif page == "거래 내역":
    st.title("실제 거래 원장")
    stage_options={s["name"]:s["id"] for s in stages}
    with st.form("add_tx"):
        c1,c2=st.columns(2)
        td=c1.date_input("거래일",value=date.today())
        typ=c2.selectbox("거래 유형",["매도","일반 매수","Make up 매수"])
        stage_name=st.selectbox("연결 Stage",["(선택 안 함)"]+list(stage_options.keys()))
        c3,c4,c5=st.columns(3)
        qty=c3.number_input("수량",min_value=0.0001,value=80.0,step=1.0)
        price=c4.number_input("체결가 (USD)",min_value=0.0,value=0.0,step=.01,format="%.4f")
        fee=c5.number_input("수수료 (USD)",min_value=0.0,value=0.0,step=.01,format="%.4f")
        memo=st.text_input("메모")
        submitted=st.form_submit_button("거래 저장",use_container_width=True)
    if submitted:
        add_transaction(td,stage_options.get(stage_name),typ,qty,price,fee,memo)
    st.subheader("거래 내역")
    if len(txdf):
        view=txdf.copy()
        view["거래일"]=view.trade_date
        view["유형"]=view.trade_type
        view["수량"]=view.quantity
        view["체결가"]=view.price
        view["수수료"]=view.fee
        st.dataframe(view[["id","거래일","유형","수량","체결가","수수료","memo"]],use_container_width=True,hide_index=True)
        ids=view.id.tolist()
        with st.form("delete_tx"):
            delete_id=st.selectbox("삭제할 거래 ID",ids)
            confirm=st.checkbox("이 거래를 삭제하겠습니다.")
            delete=st.form_submit_button("선택 거래 삭제")
        if delete and confirm:
            sb.table("transactions").delete().eq("id",delete_id).eq("user_id",uid).execute()
            st.rerun()
        csv=txdf.to_csv(index=False).encode("utf-8-sig")
        st.download_button("거래 내역 CSV 다운로드",csv,file_name=f"{symbol}_transactions.csv",mime="text/csv")
    else:
        st.info("거래 내역이 없습니다.")

# ---------- Makeup ----------
elif page == "Make up":
    st.title("Make up · 회복 수량 보충")
    st.caption("주봉 5주선 회복 시 50%, 일봉 30일선 회복 시 100% 확보 목표")
    for s in stages:
        m=stage_metrics(s)
        sold=m["sold"]
        regular=m["general_buy"]
        makeup=m["makeup_buy"]
        target50=sold*.5
        target100=sold
        need50=max(0,target50-regular-makeup)
        need100=max(0,target100-regular-makeup)
        with st.expander(f"{s['name']} · 최초 매도 {sold:,.0f}주",expanded=False):
            c1,c2,c3=st.columns(3)
            c1.metric("일반 매수",f"{regular:,.0f}주")
            c2.metric("기존 Make up",f"{makeup:,.0f}주")
            c3.metric("50% 목표",f"{target50:,.0f}주")
            st.write(f"**주봉 5주선 회복 시 부족 수량: {need50:,.0f}주**")
            st.write(f"**일봉 30일선 회복 시 총 부족 수량: {need100:,.0f}주**")
            st.caption("위 수량은 해당 Stage의 누적 체결 기준이며, 실제 회복 신호를 확인한 뒤 매수하세요.")
            if need50 or need100:
                st.info("실제 Make up 매수는 거래 내역 메뉴에서 'Make up 매수'로 기록하면 자동 반영됩니다.")

# ---------- Analytics ----------
elif page == "전략 분석":
    st.title("전략 분석")
    if len(txdf):
        t=txdf.copy()
        t["현금흐름"]=np.where(t.trade_type=="매도",t.quantity*t.price-t.fee,-(t.quantity*t.price+t.fee))
        st.subheader("거래 유형별 수량")
        q=t.groupby("trade_type",as_index=False).quantity.sum()
        st.plotly_chart(px.bar(q,x="trade_type",y="quantity",color="trade_type"),use_container_width=True)
        st.subheader("Stage별 참고 가격차이 이익")
        rows=[]
        for s in stages:
            m=stage_metrics(s)
            # reference metric, not realized tax/accounting P&L
            rows.append({"Stage":s["name"],"매도평균가":m["avg_sell"],"매수평균가":(m["buy_value"]/(m["general_buy"]+m["makeup_buy"])) if (m["general_buy"]+m["makeup_buy"]) else 0,
                         "매도수량":m["sold"],"매수수량":m["general_buy"]+m["makeup_buy"]})
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        st.caption("Stage 참고 가격차이 이익은 실제 매도·매수 체결의 대응 관계가 확정된 회계상 실현손익과 다를 수 있습니다.")
    else:
        st.info("분석할 거래 데이터가 아직 없습니다.")

# ---------- Price data ----------
elif page == "주가 분석":
    st.title("주가 분석 · 자동 + 수동")
    c1,c2=st.columns([2,1])
    ticker=c1.text_input("티커",value=symbol)
    period=c2.selectbox("조회 기간",["6mo","1y","2y","5y"],index=1)
    if st.button("주가 자동 조회"):
        try:
            hist=yf.Ticker(ticker).history(period=period,auto_adjust=False)
            if hist.empty:
                st.warning("주가 데이터를 가져오지 못했습니다. 티커 또는 데이터 공급자 상태를 확인하세요.")
            else:
                hist=hist.reset_index()
                hist["MA30"]=hist["Close"].rolling(30).mean()
                hist["주봉종가"]=hist["Close"]
                weekly=hist.set_index("Date").resample("W-FRI").agg({"Close":"last"}).dropna()
                weekly["MA5W"]=weekly["Close"].rolling(5).mean()
                st.session_state.price_hist=hist
                st.session_state.weekly_hist=weekly.reset_index()
        except Exception as e:
            st.error(f"자동 조회 실패: {e}")
    if "price_hist" in st.session_state:
        hist=st.session_state.price_hist
        st.metric("최근 종가",money(hist.Close.iloc[-1]))
        st.plotly_chart(px.line(hist,x="Date",y=["Close","MA30"],title=f"{ticker} 일봉 · 30일선"),use_container_width=True)
        weekly=st.session_state.weekly_hist
        st.plotly_chart(px.line(weekly,x="Date",y=["Close","MA5W"],title=f"{ticker} 주봉 · 5주선"),use_container_width=True)
        st.caption("주봉은 금요일 기준 주간 종가로 집계합니다. 진행 중인 주의 값은 확정 주봉이 아닐 수 있습니다.")
    st.subheader("수동 입력")
    with st.form("manual_price"):
        pdate=st.date_input("기준일",value=date.today())
        pclose=st.number_input("종가",min_value=0.0,value=0.0,step=.01)
        pma30=st.number_input("일봉 30일선",min_value=0.0,value=0.0,step=.01)
        pma5w=st.number_input("주봉 5주선",min_value=0.0,value=0.0,step=.01)
        source=st.selectbox("데이터 출처",["수동 입력","자동 조회 후 보정"])
        savep=st.form_submit_button("가격 정보 저장")
    if savep:
        sb.table("price_history").upsert({"user_id":uid,"portfolio_id":pid,"price_date":str(pdate),"close_price":pclose,
            "ma30":pma30,"ma5w":pma5w,"source":source},on_conflict="portfolio_id,price_date").execute()
        st.success("가격 정보를 저장했습니다.")

# ---------- Settings ----------
elif page == "설정":
    st.title("계좌 및 전략 설정")
    with st.form("portfolio_settings"):
        pname=st.text_input("계좌 이름",value=portfolio["name"])
        psymbol=st.text_input("종목 티커",value=portfolio["symbol"])
        pshares=st.number_input("초기 보유수량",min_value=0.0,value=float(portfolio["initial_shares"]),step=1.0)
        pratio=st.number_input("전략 적용 비율 (%)",min_value=0.0,max_value=100.0,value=float(portfolio["strategy_ratio"])*100,step=1.0)/100
        pcash=st.number_input("초기 현금 (USD)",min_value=0.0,value=float(portfolio["initial_cash"]),step=100.0)
        save=st.form_submit_button("설정 저장")
    if save:
        sb.table("portfolios").update({"name":pname,"symbol":psymbol,"initial_shares":pshares,"strategy_ratio":pratio,"initial_cash":pcash}).eq("id",pid).execute()
        st.success("설정을 저장했습니다.")
        st.rerun()
    st.divider()
    st.warning("현재 앱은 거래 기록 및 분석용입니다. 증권사 주문을 자동 실행하지 않습니다. 주가 자동 조회는 참고 데이터이며 공급자 지연·오류가 발생할 수 있습니다.")
