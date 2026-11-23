-- PAPAS 7.0 Windmill: Supabase SQL
-- Supabase Dashboard > SQL Editor에서 전체 실행하세요.
create extension if not exists pgcrypto;

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

create table if not exists public.transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  stage_id uuid references public.stages(id) on delete set null,
  trade_date date not null,
  trade_type text not null check (trade_type in ('매도','일반 매수','Make up 매수')),
  quantity numeric not null check (quantity > 0),
  price numeric not null check (price >= 0),
  fee numeric not null default 0 check (fee >= 0),
  memo text,
  created_at timestamptz not null default now()
);

create table if not exists public.price_history (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  price_date date not null,
  close_price numeric not null default 0,
  ma30 numeric,
  ma5w numeric,
  source text not null default '수동 입력',
  created_at timestamptz not null default now(),
  unique(portfolio_id, price_date)
);

create table if not exists public.makeups (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  stage_id uuid not null references public.stages(id) on delete cascade,
  signal_type text not null check (signal_type in ('주봉 5주선 50%','일봉 30일선 100%')),
  signal_date date,
  confirmed boolean not null default false,
  memo text,
  created_at timestamptz not null default now()
);

create table if not exists public.alerts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  portfolio_id uuid not null references public.portfolios(id) on delete cascade,
  alert_type text not null,
  message text not null,
  is_read boolean not null default false,
  created_at timestamptz not null default now()
);

alter table public.portfolios enable row level security;
alter table public.stages enable row level security;
alter table public.transactions enable row level security;
alter table public.price_history enable row level security;
alter table public.makeups enable row level security;
alter table public.alerts enable row level security;

do $$
declare t text;
begin
  foreach t in array array['portfolios','stages','transactions','price_history','makeups','alerts']
  loop
    execute format('drop policy if exists "users manage own %I" on public.%I', t, t);
    execute format('create policy "users manage own %I" on public.%I for all using (auth.uid() = user_id) with check (auth.uid() = user_id)', t, t);
  end loop;
end $$;

-- 새 사용자의 첫 계좌 자동 생성
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
