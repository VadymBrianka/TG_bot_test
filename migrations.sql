-- FinTrack Hub — Supabase Migrations (Optional / Recommended)
-- Run this in Supabase SQL Editor to enable atomic transactions, custom categories,
-- budgets, recurring payments, price alerts, and user preferences.

-- 1. Extend users table with preferences, base currency, and role
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS default_account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS base_currency TEXT NOT NULL DEFAULT 'UAH',
  ADD COLUMN IF NOT EXISTS weekly_digest BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS monthly_digest BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS budget_alerts_enabled BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS daily_reminder BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS notifications_enabled BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user',
  ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS subscription_plan TEXT NOT NULL DEFAULT 'free',
  ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMPTZ;

-- Subscription payments & crypto payment requests
CREATE TABLE IF NOT EXISTS subscription_payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  telegram_id BIGINT NOT NULL,
  plan TEXT NOT NULL CHECK (plan IN ('pro', 'vip')),
  period TEXT NOT NULL CHECK (period IN ('month', 'year')),
  payment_method TEXT NOT NULL CHECK (payment_method IN ('stars', 'crypto')),
  currency TEXT NOT NULL,
  amount NUMERIC(18, 6) NOT NULL,
  tx_hash TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'paid')),
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Global app settings (e.g. crypto wallet addresses)
CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Subscription giveaways
CREATE TABLE IF NOT EXISTS giveaways (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  title TEXT NOT NULL,
  plan TEXT NOT NULL CHECK (plan IN ('pro', 'vip')),
  duration_days INT NOT NULL DEFAULT 30 CHECK (duration_days > 0),
  winners_count INT NOT NULL DEFAULT 1 CHECK (winners_count > 0),
  participants JSONB NOT NULL DEFAULT '[]'::jsonb,
  winners JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished')),
  created_by BIGINT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- VIP Demo Crypto Trading Bot: demo balance, autonomous bot config, and full positions history
CREATE TABLE IF NOT EXISTS trading_wallets (
  user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  demo_usdt NUMERIC(18, 4) NOT NULL DEFAULT 10000.0,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trading_bots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  auto_select BOOLEAN NOT NULL DEFAULT TRUE,
  symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
  risk_profile TEXT NOT NULL DEFAULT 'balanced',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Safe migration if trading_bots already existed from an earlier schema
ALTER TABLE trading_bots
  ADD COLUMN IF NOT EXISTS auto_select BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS symbols JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS risk_profile TEXT NOT NULL DEFAULT 'balanced',
  ADD COLUMN IF NOT EXISTS leverage INT NOT NULL DEFAULT 5;

ALTER TABLE trading_positions
  ADD COLUMN IF NOT EXISTS side TEXT NOT NULL DEFAULT 'long',
  ADD COLUMN IF NOT EXISTS leverage INT NOT NULL DEFAULT 1;

-- Family / Shared budget groups (VIP)
CREATE TABLE IF NOT EXISTS family_groups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL DEFAULT 'Сімʼя',
  invite_code TEXT UNIQUE NOT NULL,
  owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS family_members (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id UUID NOT NULL REFERENCES family_groups(id) ON DELETE CASCADE,
  user_id UUID UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  joined_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trading_positions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  strategy TEXT NOT NULL DEFAULT 'AI-Momentum',
  usdt_invested NUMERIC(18, 4) NOT NULL,
  quantity NUMERIC(18, 8) NOT NULL,
  entry_price NUMERIC(18, 4) NOT NULL,
  exit_price NUMERIC(18, 4),
  pnl_usdt NUMERIC(18, 4) DEFAULT 0,
  pnl_pct NUMERIC(8, 2) DEFAULT 0,
  tp_pct NUMERIC(6, 2) NOT NULL DEFAULT 2.8,
  sl_pct NUMERIC(6, 2) NOT NULL DEFAULT 1.8,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  close_reason TEXT DEFAULT '',
  opened_at TIMESTAMPTZ DEFAULT NOW(),
  closed_at TIMESTAMPTZ
);

-- Broadcast history table for admin panel
CREATE TABLE IF NOT EXISTS broadcast_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  admin_tg_id BIGINT NOT NULL,
  target_filter TEXT NOT NULL DEFAULT 'all',
  message_text TEXT NOT NULL,
  sent_count INT NOT NULL DEFAULT 0,
  blocked_count INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Optional: grant admin role to the primary admin
-- UPDATE users SET role = 'admin' WHERE telegram_id = 464800908;

-- 2. Extend categories table for custom user categories
ALTER TABLE categories
  ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;

-- 3. Monthly category budgets
CREATE TABLE IF NOT EXISTS budgets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  monthly_limit NUMERIC(14, 2) NOT NULL CHECK (monthly_limit > 0),
  currency TEXT NOT NULL DEFAULT 'UAH',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, category_id)
);

-- 4. Recurring / Scheduled payments
CREATE TABLE IF NOT EXISTS recurring_payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id UUID REFERENCES accounts(id) ON DELETE SET NULL,
  category_id UUID REFERENCES categories(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
  type TEXT NOT NULL DEFAULT 'expense' CHECK (type IN ('expense', 'income')),
  day_of_month INT NOT NULL DEFAULT 1 CHECK (day_of_month BETWEEN 1 AND 31),
  next_run_date DATE NOT NULL DEFAULT CURRENT_DATE,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. Asset price alerts
CREATE TABLE IF NOT EXISTS price_alerts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ticker TEXT NOT NULL,
  asset_class TEXT NOT NULL DEFAULT 'crypto' CHECK (asset_class IN ('stock', 'crypto')),
  condition TEXT NOT NULL CHECK (condition IN ('above', 'below')),
  target_price NUMERIC(18, 4) NOT NULL CHECK (target_price > 0),
  is_triggered BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. Debts / Loans tracker
CREATE TABLE IF NOT EXISTS debts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CHECK (direction IN ('owed_to_me', 'i_owe')),
  person_name TEXT NOT NULL,
  amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
  currency TEXT NOT NULL DEFAULT 'UAH',
  note TEXT DEFAULT '',
  due_date DATE,
  is_settled BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 7. Atomic transaction creation RPC
CREATE OR REPLACE FUNCTION create_transaction_atomic(
  p_user_id UUID,
  p_account_id UUID,
  p_to_account_id UUID,
  p_category_id UUID,
  p_amount NUMERIC,
  p_target_amount NUMERIC,
  p_type TEXT,
  p_note TEXT,
  p_transaction_date TIMESTAMPTZ DEFAULT NOW()
) RETURNS UUID AS $$
DECLARE
  v_tx_id UUID;
BEGIN
  IF p_type = 'expense' THEN
    UPDATE accounts SET balance = balance - p_amount
    WHERE id = p_account_id AND user_id = p_user_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Account not found'; END IF;
  ELSIF p_type = 'income' THEN
    UPDATE accounts SET balance = balance + p_amount
    WHERE id = p_account_id AND user_id = p_user_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Account not found'; END IF;
  ELSIF p_type = 'transfer' THEN
    UPDATE accounts SET balance = balance - p_amount
    WHERE id = p_account_id AND user_id = p_user_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Source account not found'; END IF;

    IF p_to_account_id IS NOT NULL THEN
      UPDATE accounts SET balance = balance + COALESCE(p_target_amount, p_amount)
      WHERE id = p_to_account_id AND user_id = p_user_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Destination account not found'; END IF;
    END IF;
  END IF;

  INSERT INTO transactions (
    user_id, account_id, to_account_id, category_id,
    amount, target_amount, type, note, transaction_date
  ) VALUES (
    p_user_id, p_account_id, p_to_account_id, p_category_id,
    p_amount, p_target_amount, p_type, p_note, COALESCE(p_transaction_date, NOW())
  ) RETURNING id INTO v_tx_id;

  RETURN v_tx_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 7. Atomic goal funds modification RPC
CREATE OR REPLACE FUNCTION modify_goal_funds_atomic(
  p_user_id UUID,
  p_goal_id UUID,
  p_account_id UUID,
  p_amount_goal NUMERIC,
  p_amount_acc NUMERIC,
  p_action TEXT,
  p_note TEXT
) RETURNS VOID AS $$
DECLARE
  v_acc_bal NUMERIC;
  v_goal_bal NUMERIC;
BEGIN
  SELECT balance INTO v_acc_bal FROM accounts WHERE id = p_account_id AND user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Account not found'; END IF;

  SELECT current_amount INTO v_goal_bal FROM goals WHERE id = p_goal_id AND user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Goal not found'; END IF;

  IF p_action = 'deposit' THEN
    IF v_acc_bal < p_amount_acc THEN
      RAISE EXCEPTION 'Insufficient account balance';
    END IF;
    UPDATE accounts SET balance = balance - p_amount_acc WHERE id = p_account_id;
    UPDATE goals SET current_amount = current_amount + p_amount_goal WHERE id = p_goal_id;
  ELSIF p_action = 'withdraw' THEN
    IF v_goal_bal < p_amount_goal THEN
      RAISE EXCEPTION 'Insufficient goal balance';
    END IF;
    UPDATE goals SET current_amount = current_amount - p_amount_goal WHERE id = p_goal_id;
    UPDATE accounts SET balance = balance + p_amount_acc WHERE id = p_account_id;
  ELSE
    RAISE EXCEPTION 'Invalid action';
  END IF;

  INSERT INTO transactions (user_id, account_id, amount, target_amount, type, note)
  VALUES (p_user_id, p_account_id, ROUND(p_amount_acc, 2), ROUND(p_amount_goal, 2), 'transfer', p_note);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 8. User activity tracking & active promo code
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMPTZ DEFAULT NOW(),
  ADD COLUMN IF NOT EXISTS active_promo_code TEXT DEFAULT NULL;

-- 9. Transaction tags column
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}';

-- 10. Auto-categorization rules (Pro+)
CREATE TABLE IF NOT EXISTS category_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  keyword TEXT NOT NULL,
  category_id UUID NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (user_id, keyword)
);

-- 11. Paid service subscriptions tracker (Pro+)
CREATE TABLE IF NOT EXISTS service_subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  amount NUMERIC(14, 2) NOT NULL CHECK (amount > 0),
  currency TEXT NOT NULL DEFAULT 'UAH',
  billing_cycle TEXT NOT NULL DEFAULT 'monthly' CHECK (billing_cycle IN ('monthly', 'yearly')),
  next_billing_date DATE NOT NULL,
  reminder_days INT NOT NULL DEFAULT 2,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  last_reminded_date DATE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 12. Promo codes (trial & discount)
CREATE TABLE IF NOT EXISTS promo_codes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code TEXT UNIQUE NOT NULL,
  promo_type TEXT NOT NULL CHECK (promo_type IN ('trial', 'discount')),
  plan TEXT NOT NULL DEFAULT 'pro' CHECK (plan IN ('pro', 'vip')),
  duration_days INT NOT NULL DEFAULT 14,
  discount_percent INT NOT NULL DEFAULT 0,
  max_uses INT NOT NULL DEFAULT 100,
  used_count INT NOT NULL DEFAULT 0,
  expires_at TIMESTAMPTZ,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS promo_redemptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  promo_id UUID NOT NULL REFERENCES promo_codes(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  redeemed_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (promo_id, user_id)
);

-- 13. Feature usage & funnel analytics events
CREATE TABLE IF NOT EXISTS analytics_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_name TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

