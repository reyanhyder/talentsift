-- TalentSift launch tables
-- Run this in Supabase SQL Editor if these tables do not exist yet.

create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete cascade,
  plan text not null,
  razorpay_order_id text unique,
  razorpay_payment_id text,
  amount integer,
  status text default 'created',
  webhook_event text,
  created_at timestamptz default now(),
  verified_at timestamptz,
  updated_at timestamptz
);

create table if not exists public.feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete cascade,
  kind text default 'feedback',
  message text not null,
  page text,
  created_at timestamptz default now()
);

create table if not exists public.user_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references public.users(id) on delete set null,
  event_name text not null,
  metadata jsonb default '{}'::jsonb,
  created_at timestamptz default now()
);

create index if not exists idx_payments_user_id on public.payments(user_id);
create index if not exists idx_feedback_user_id on public.feedback(user_id);
create index if not exists idx_user_events_user_id on public.user_events(user_id);
create index if not exists idx_user_events_event_name on public.user_events(event_name);
