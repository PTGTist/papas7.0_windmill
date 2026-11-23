-- PAPAS 7.0 Windmill: Supabase SQL (v2)
-- Supabase Dashboard > SQL Editor에서 전체 실행하세요.
-- 처음 설치할 때와 이전 버전을 이미 실행한 DB 모두에서 그대로 다시 실행해도 됩니다.
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- 계좌
create table if not exists public.portfolios (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null default 'SOXL 기본 계좌',
  symbol text not null default 'SOXL',
  initial_shares numeric not null default 1600,
  strategy_ratio numeric not null default 0.5,
  initial_cash numeric not null default 0,
  currency text not null default 'USD',
  created_at timestamptz not null default now()
);
alter table public.portfolios add column if not exists per_trade_qty numeric not null default 80;
alter table public.portfolios add column if not exists sell_count integer not null default 5;
alter table public.portfolios add column if not exists interval_schedule numeric[] not null default '{0.05,0.045,0.04,0.035,0.03}';
alter table public.portfolios add column if not exists makeup1_rule text not null default 'above';
alter table public.portfolios add column if not exists makeup2_rule text not null default 'weekly300';
alter table public.portfolios add column if not exists makeup_basis text not null default 'amount';

-- ---------------------------------------------------------------- Stage
create table if not exists public.stages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  name text not null,
  stage_no integer not null,
  start_date date,
  sell_start date,
  sell_end date,
  buy_interval numeric not null default 0.03,
  per_trade_qty numeric not null default 80,
  sell_count integer not null default 5,
  sell_target numeric not null default 400,
  created_at timestamptz not null default now()
);
alter table public.stages add column if not exists sell_days date[];
alter table public.stages add column if not exists closed_at date;

-- ---------------------------------------------------------------- 거래 원장
create table if not exists public.transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  stage_id uuid references public.stages(id) on delete set null,
  trade_date date not null,
  trade_type text not null,
  quantity numeric not null check (quantity > 0),
  price numeric not null check (price >= 0),
  fee numeric not null default 0 check (fee >= 0),
  memo text,
  created_at timestamptz not null default now()
);
alter table public.transactions add column if not exists round_no integer;
-- 거래 유형: 매도 / 재매수 / 1차 메이크업 / 2차 메이크업 (이전 버전 값은 바꿔 줌)
alter table public.transactions drop constraint if exists transactions_trade_type_check;
update public.transactions set trade_type = '재매수' where trade_type = '일반 매수';
update public.transactions set trade_type = '1차 메이크업' where trade_type = 'Make up 매수';
alter table public.transactions add constraint transactions_trade_type_check
  check (trade_type in ('매도','재매수','1차 메이크업','2차 메이크업'));
alter table public.transactions drop constraint if exists transactions_round_no_check;
alter table public.transactions add constraint transactions_round_no_check
  check (round_no is null or (round_no between 1 and 30));
-- 체결가 0 입력 방지(기존 행은 검사하지 않음)
alter table public.transactions drop constraint if exists transactions_price_positive;
alter table public.transactions add constraint transactions_price_positive check (price > 0) not valid;

-- ---------------------------------------------------------------- 메이크업 신호 확인
create table if not exists public.makeups (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  stage_id uuid not null references public.stages(id) on delete cascade,
  signal_date date,
  confirmed boolean not null default false,
  memo text,
  created_at timestamptz not null default now()
);
alter table public.makeups add column if not exists phase integer;
alter table public.makeups drop constraint if exists makeups_signal_type_check;
do $$ begin
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'makeups' and column_name = 'signal_type') then
    alter table public.makeups alter column signal_type drop not null;
    update public.makeups set phase = case when signal_type like '주봉%' then 1 else 2 end where phase is null;
  end if;
end $$;
create unique index if not exists makeups_stage_phase_uq on public.makeups(stage_id, phase);

-- ---------------------------------------------------------------- 가격 캐시(자동 조회 실패 시 사용)
create table if not exists public.price_cache (
  user_id uuid not null references auth.users(id) on delete cascade,
  symbol text not null,
  day date not null,
  open numeric not null,
  high numeric not null,
  low numeric not null,
  close numeric not null,
  primary key (user_id, symbol, day)
);

-- 이전 버전 테이블(price_history, alerts)은 사용하지 않지만 남겨 둡니다.
create table if not exists public.alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  alert_type text not null,
  message text not null,
  is_read boolean not null default false,
  created_at timestamptz not null default now()
);

create index if not exists stages_portfolio_idx on public.stages(portfolio_id);
create index if not exists transactions_portfolio_idx on public.transactions(portfolio_id, trade_date);
create index if not exists makeups_portfolio_idx on public.makeups(portfolio_id);

-- ---------------------------------------------------------------- 사용자별 보안 정책
do $$
declare t text;
begin
  foreach t in array array['portfolios','stages','transactions','makeups','alerts','price_cache']
  loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists "users manage own %I" on public.%I', t, t);
    execute format('create policy "users manage own %I" on public.%I for all using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id)', t, t);
  end loop;
end $$;

-- ---------------------------------------------------------------- 새 사용자의 첫 계좌 자동 생성
create or replace function public.create_default_portfolio()
returns trigger language plpgsql security definer set search_path = public
as $$
begin
  insert into public.portfolios(user_id, name, symbol, initial_shares, strategy_ratio, initial_cash)
  values(new.id, 'SOXL 기본 계좌', 'SOXL', 1600, 0.5, 0);
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_papas on auth.users;
create trigger on_auth_user_created_papas
after insert on auth.users
for each row execute procedure public.create_default_portfolio();
