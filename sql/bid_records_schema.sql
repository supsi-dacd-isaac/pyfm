-- =============================================================================
-- BID RECORDS SCHEMA
-- =============================================================================
-- This script creates the bid_records tables and updates market_ledger
-- with a foreign key to link trades to their originating bids.
--
-- Run this script once to set up the schema.
-- =============================================================================

-- Ensure uuid-ossp extension is available (for uuid_generate_v4)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================================================
-- 1. CREATE BID_RECORDS TABLE
-- =============================================================================
-- Stores what the FSP planned to bid on the market.
-- Created by trader_fsp.py BEFORE placing orders.

CREATE TABLE IF NOT EXISTS public.bid_records (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    fsp_id VARCHAR(100) NOT NULL,
    slot_start TIMESTAMP NOT NULL,
    slot_end TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
    strategy_id VARCHAR(100),
    strategy_name VARCHAR(200),
    strategy_description TEXT,
    total_quantity_mw DECIMAL(10, 6),
    
    -- Price information (all in currency/MW)
    dso_offered_price DECIMAL(10, 4),      -- Price offered by DSO (buyer's price)
    fsp_min_price DECIMAL(10, 4),          -- FSP's minimum acceptable price (from strategy)
    actual_price DECIMAL(10, 4),           -- Actual transaction price (what was agreed)
    currency VARCHAR(10) DEFAULT 'CHF',
    
    status VARCHAR(50) DEFAULT 'pending',
    activated_at TIMESTAMP,
    
    -- Each FSP can only have one bid per time slot
    CONSTRAINT uq_bid_records_fsp_slot UNIQUE(fsp_id, slot_start)
);

-- Add columns to existing table if they don't exist (for migrations)
DO $$
BEGIN
    -- Migration: rename old bid_price to actual_price if it exists
    IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = 'bid_records' AND column_name = 'bid_price'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = 'bid_records' AND column_name = 'actual_price'
    ) THEN
        ALTER TABLE public.bid_records RENAME COLUMN bid_price TO actual_price;
        RAISE NOTICE 'Renamed bid_price to actual_price';
    END IF;

    -- Add dso_offered_price column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = 'bid_records' AND column_name = 'dso_offered_price'
    ) THEN
        ALTER TABLE public.bid_records ADD COLUMN dso_offered_price DECIMAL(10, 4);
        RAISE NOTICE 'Added dso_offered_price column to bid_records';
    END IF;
    
    -- Add fsp_min_price column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = 'bid_records' AND column_name = 'fsp_min_price'
    ) THEN
        ALTER TABLE public.bid_records ADD COLUMN fsp_min_price DECIMAL(10, 4);
        RAISE NOTICE 'Added fsp_min_price column to bid_records';
    END IF;
    
    -- Add actual_price column (if not already renamed from bid_price)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = 'bid_records' AND column_name = 'actual_price'
    ) THEN
        ALTER TABLE public.bid_records ADD COLUMN actual_price DECIMAL(10, 4);
        RAISE NOTICE 'Added actual_price column to bid_records';
    END IF;
    
    -- Add currency column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' AND table_name = 'bid_records' AND column_name = 'currency'
    ) THEN
        ALTER TABLE public.bid_records ADD COLUMN currency VARCHAR(10) DEFAULT 'CHF';
        RAISE NOTICE 'Added currency column to bid_records';
    END IF;
END $$;

-- Index for faster lookups
CREATE INDEX IF NOT EXISTS idx_bid_records_fsp_slot 
    ON public.bid_records(fsp_id, slot_start);

CREATE INDEX IF NOT EXISTS idx_bid_records_status 
    ON public.bid_records(status);

CREATE INDEX IF NOT EXISTS idx_bid_records_created_at 
    ON public.bid_records(created_at);

COMMENT ON TABLE public.bid_records IS 'Stores bid intentions from FSPs before/during market trading';
COMMENT ON COLUMN public.bid_records.status IS 'pending = awaiting activation, activated = flexibility delivered';
COMMENT ON COLUMN public.bid_records.dso_offered_price IS 'Price offered by DSO (buyer) in currency/MW';
COMMENT ON COLUMN public.bid_records.fsp_min_price IS 'FSP minimum acceptable price from strategy in currency/MW';
COMMENT ON COLUMN public.bid_records.actual_price IS 'Actual transaction price agreed in currency/MW';
COMMENT ON COLUMN public.bid_records.currency IS 'Currency for all prices (default: CHF)';


-- =============================================================================
-- 2. CREATE BID_RECORD_ORDERS TABLE
-- =============================================================================
-- Stores the orders that were planned/placed for each bid.

CREATE TABLE IF NOT EXISTS public.bid_record_orders (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    bid_record_id UUID NOT NULL REFERENCES public.bid_records(id) ON DELETE CASCADE,
    portfolio VARCHAR(200),
    regulation_type VARCHAR(50),
    quantity_mw DECIMAL(10, 6),
    unit_price DECIMAL(10, 4),
    time_slot_name VARCHAR(200),
    period_from TIMESTAMP,
    period_to TIMESTAMP,
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bid_record_orders_bid_id 
    ON public.bid_record_orders(bid_record_id);

COMMENT ON TABLE public.bid_record_orders IS 'Orders planned/placed for each bid record';


-- =============================================================================
-- 3. CREATE BID_RECORD_ASSETS TABLE
-- =============================================================================
-- Stores which assets should be activated for each bid.
-- Used by flexi_manager.py to know which assets to control.

CREATE TABLE IF NOT EXISTS public.bid_record_assets (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    bid_record_id UUID NOT NULL REFERENCES public.bid_records(id) ON DELETE CASCADE,
    asset_id VARCHAR(100) NOT NULL,
    description VARCHAR(200),
    asset_type VARCHAR(100),
    available_flexibility_kw DECIMAL(10, 3),
    flexibility_factor DECIMAL(5, 3),
    reference_power_kw DOUBLE PRECISION,
    reference_power_source TEXT,
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
);

ALTER TABLE public.bid_record_assets
    ADD COLUMN IF NOT EXISTS reference_power_kw DOUBLE PRECISION NULL,
    ADD COLUMN IF NOT EXISTS reference_power_source TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_bid_record_assets_bid_id 
    ON public.bid_record_assets(bid_record_id);

CREATE INDEX IF NOT EXISTS idx_bid_record_assets_asset_id 
    ON public.bid_record_assets(asset_id);

COMMENT ON TABLE public.bid_record_assets IS 'Assets to activate for flexibility delivery';
COMMENT ON COLUMN public.bid_record_assets.reference_power_kw IS 'Asset-level reference power used when deriving the bid asset flexibility';
COMMENT ON COLUMN public.bid_record_assets.reference_power_source IS 'Source of reference_power_kw, for example recent_profile_baseline';


-- =============================================================================
-- 4. UPDATE MARKET_LEDGER TABLE
-- =============================================================================
-- Add foreign key to link actual trades to their originating bids.

-- Add the bid_record_id column if it doesn't exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_schema = 'public' 
        AND table_name = 'market_ledger' 
        AND column_name = 'bid_record_id'
    ) THEN
        ALTER TABLE public.market_ledger 
        ADD COLUMN bid_record_id UUID REFERENCES public.bid_records(id) ON DELETE SET NULL;
        
        CREATE INDEX idx_market_ledger_bid_record_id 
            ON public.market_ledger(bid_record_id);
        
        RAISE NOTICE 'Added bid_record_id column to market_ledger';
    ELSE
        RAISE NOTICE 'bid_record_id column already exists in market_ledger';
    END IF;
END $$;

COMMENT ON COLUMN public.market_ledger.bid_record_id IS 'Links this trade to the bid that originated it (for sell-side)';


-- =============================================================================
-- 5. USEFUL VIEWS
-- =============================================================================

-- View: Bids with their trade outcomes
CREATE OR REPLACE VIEW public.v_bid_trade_summary AS
SELECT 
    br.id AS bid_id,
    br.fsp_id,
    br.slot_start,
    br.slot_end,
    br.strategy_id,
    br.strategy_name,
    br.total_quantity_mw AS planned_quantity_mw,
    br.dso_offered_price,
    br.fsp_min_price,
    br.actual_price,
    br.currency,
    br.status AS bid_status,
    br.created_at AS bid_created_at,
    br.activated_at,
    COUNT(ml.id) AS trade_count,
    SUM(ml.flexibility_quantity) AS traded_quantity_mw,
    SUM(ml.flexibility_quantity * ml.price) AS total_revenue_chf,
    CASE WHEN br.total_quantity_mw > 0 AND br.actual_price IS NOT NULL
         THEN br.total_quantity_mw * br.actual_price 
         ELSE 0 END AS potential_revenue_chf,
    -- Price margin: how much above FSP minimum the actual price was
    CASE WHEN br.fsp_min_price > 0 
         THEN br.actual_price - br.fsp_min_price 
         ELSE NULL END AS price_margin
FROM public.bid_records br
LEFT JOIN public.market_ledger ml ON ml.bid_record_id = br.id
GROUP BY br.id, br.fsp_id, br.slot_start, br.slot_end, br.strategy_id, 
         br.strategy_name, br.total_quantity_mw, br.dso_offered_price, 
         br.fsp_min_price, br.actual_price, br.currency, 
         br.status, br.created_at, br.activated_at;

COMMENT ON VIEW public.v_bid_trade_summary IS 'Summary of bids with their actual trade outcomes';


-- View: Assets per bid
CREATE OR REPLACE VIEW public.v_bid_assets AS
SELECT 
    br.id AS bid_id,
    br.fsp_id,
    br.slot_start,
    br.strategy_name,
    bra.asset_id,
    bra.description AS asset_description,
    bra.asset_type,
    bra.available_flexibility_kw,
    bra.flexibility_factor
FROM public.bid_records br
JOIN public.bid_record_assets bra ON bra.bid_record_id = br.id;

COMMENT ON VIEW public.v_bid_assets IS 'Assets included in each bid';


-- =============================================================================
-- 6. CREATE ASSET_ACTIVATIONS TABLE
-- =============================================================================
-- Stores actual activation commands sent to assets.
-- Created by flexi_manager.py when flexibility is delivered.

CREATE TABLE IF NOT EXISTS public.asset_activations (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    bid_record_id UUID REFERENCES public.bid_records(id) ON DELETE SET NULL,
    fsp_id VARCHAR(100) NOT NULL,
    slot_start TIMESTAMP NOT NULL,
    slot_end TIMESTAMP NOT NULL,
    asset_id VARCHAR(100) NOT NULL,
    asset_description VARCHAR(200),
    asset_type VARCHAR(100),
    power_to_activate_kw DECIMAL(10, 3) NOT NULL,
    percentage_of_capacity DECIMAL(5, 2),
    allocation_strategy VARCHAR(50),
    dry_run BOOLEAN DEFAULT FALSE,
    activation_status VARCHAR(50) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
);

-- Indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_asset_activations_fsp_slot 
    ON public.asset_activations(fsp_id, slot_start);

CREATE INDEX IF NOT EXISTS idx_asset_activations_asset_id 
    ON public.asset_activations(asset_id);

CREATE INDEX IF NOT EXISTS idx_asset_activations_slot_start 
    ON public.asset_activations(slot_start);

CREATE INDEX IF NOT EXISTS idx_asset_activations_bid_record 
    ON public.asset_activations(bid_record_id);

COMMENT ON TABLE public.asset_activations IS 'Records of flexibility activation commands sent to assets';
COMMENT ON COLUMN public.asset_activations.power_to_activate_kw IS 'Power curtailment requested in kW';
COMMENT ON COLUMN public.asset_activations.percentage_of_capacity IS 'Percentage of asset capacity being curtailed';
COMMENT ON COLUMN public.asset_activations.activation_status IS 'pending, success, failed';


-- View: Activation history with bid info
CREATE OR REPLACE VIEW public.v_activation_history AS
SELECT 
    aa.id AS activation_id,
    aa.fsp_id,
    aa.slot_start,
    aa.slot_end,
    aa.asset_id,
    aa.asset_description,
    aa.asset_type,
    aa.power_to_activate_kw,
    aa.percentage_of_capacity,
    aa.allocation_strategy,
    aa.dry_run,
    aa.activation_status,
    aa.created_at,
    br.strategy_id,
    br.strategy_name,
    br.total_quantity_mw AS bid_quantity_mw
FROM public.asset_activations aa
LEFT JOIN public.bid_records br ON aa.bid_record_id = br.id
ORDER BY aa.slot_start DESC, aa.asset_id;

COMMENT ON VIEW public.v_activation_history IS 'Complete history of asset activations with bid context';


-- =============================================================================
-- 7. VERIFICATION QUERIES
-- =============================================================================
-- Run these after the migration to verify everything is set up correctly:

-- Check tables exist
-- SELECT table_name FROM information_schema.tables 
-- WHERE table_schema = 'public' AND table_name LIKE 'bid_record%';

-- Check market_ledger has the FK column
-- SELECT column_name, data_type FROM information_schema.columns 
-- WHERE table_name = 'market_ledger' AND column_name = 'bid_record_id';

-- Check foreign keys
-- SELECT tc.constraint_name, tc.table_name, kcu.column_name, 
--        ccu.table_name AS foreign_table_name, ccu.column_name AS foreign_column_name
-- FROM information_schema.table_constraints AS tc 
-- JOIN information_schema.key_column_usage AS kcu ON tc.constraint_name = kcu.constraint_name
-- JOIN information_schema.constraint_column_usage AS ccu ON ccu.constraint_name = tc.constraint_name
-- WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_name LIKE 'bid_record%';
