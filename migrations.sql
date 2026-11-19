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
