# import section
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import psycopg2
import psycopg2.extras


class BidRecordRepository:
    """
    Repository for storing and retrieving bid records in PostgreSQL.
    
    This provides persistent storage for bid information that needs to be
    shared between trader_fsp.py (bidding) and flexi_manager.py (activation).
    
    Database schema (in public schema, alongside market_ledger):
    - public.bid_records: Main bid record table (what we intend to bid)
    - public.bid_record_orders: Orders placed in each bid
    - public.bid_record_assets: Assets to activate for each bid
    
    Relationship with market_ledger:
    - market_ledger stores what actually happened (trades)
    - bid_records stores what we intended to bid
    - market_ledger.bid_record_id links to bid_records.id
    """
    
    SCHEMA = "public"
    TABLE_BID_RECORDS = "bid_records"
    TABLE_ORDERS = "bid_record_orders"
    TABLE_ASSETS = "bid_record_assets"
    
    def __init__(self, pg_interface, logger: logging.Logger):
        """
        Initialize the repository.
        
        :param pg_interface: PostgreSQLInterface instance with active connection
        :param logger: Logger instance
        """
        self.conn = pg_interface.conn
        self.logger = logger
        
        # Ensure tables exist
        self._ensure_schema()
        self._ensure_tables()
    
    def _ensure_schema(self):
        """
        For public schema, nothing to create.
        This method is kept for compatibility but does nothing.
        """
        pass  # public schema always exists
    
    def _ensure_tables(self):
        """
        Create the bid record tables if they don't exist.
        Also adds bid_record_id foreign key to market_ledger if not present.
        
        Note: Tables use UUID primary keys for consistency with market_ledger.
        Run sql/bid_records_schema.sql for the full schema with comments and views.
        """
        cur = self.conn.cursor()
        
        try:
            # Ensure uuid-ossp extension is available
            cur.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
            
            # Main bid records table (UUID primary key)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.TABLE_BID_RECORDS} (
                    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    fsp_id VARCHAR(100) NOT NULL,
                    slot_start TIMESTAMP NOT NULL,
                    slot_end TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
                    strategy_id VARCHAR(100),
                    strategy_name VARCHAR(200),
                    strategy_description TEXT,
                    total_quantity_mw DECIMAL(10, 6),
                    status VARCHAR(50) DEFAULT 'pending',
                    activated_at TIMESTAMP,
                    CONSTRAINT uq_bid_records_fsp_slot UNIQUE(fsp_id, slot_start)
                )
            """)
            
            # Orders table (what we planned to bid)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.TABLE_ORDERS} (
                    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    bid_record_id UUID NOT NULL REFERENCES {self.SCHEMA}.{self.TABLE_BID_RECORDS}(id) ON DELETE CASCADE,
                    portfolio VARCHAR(200),
                    regulation_type VARCHAR(50),
                    quantity_mw DECIMAL(10, 6),
                    unit_price DECIMAL(10, 4),
                    time_slot_name VARCHAR(200),
                    period_from TIMESTAMP,
                    period_to TIMESTAMP,
                    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
                )
            """)
            
            # Assets to activate table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.TABLE_ASSETS} (
                    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    bid_record_id UUID NOT NULL REFERENCES {self.SCHEMA}.{self.TABLE_BID_RECORDS}(id) ON DELETE CASCADE,
                    asset_id VARCHAR(100) NOT NULL,
                    description VARCHAR(200),
                    asset_type VARCHAR(100),
                    available_flexibility_kw DECIMAL(10, 3),
                    flexibility_factor DECIMAL(5, 3),
                    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
                )
            """)
            
            # Create indexes for faster lookups
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_bid_records_fsp_slot 
                ON {self.SCHEMA}.{self.TABLE_BID_RECORDS}(fsp_id, slot_start)
            """)
            
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_bid_records_status 
                ON {self.SCHEMA}.{self.TABLE_BID_RECORDS}(status)
            """)
            
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_bid_record_orders_bid_id 
                ON {self.SCHEMA}.{self.TABLE_ORDERS}(bid_record_id)
            """)
            
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_bid_record_assets_bid_id 
                ON {self.SCHEMA}.{self.TABLE_ASSETS}(bid_record_id)
            """)
            
            # Add foreign key column to market_ledger if it doesn't exist
            # This links actual trades to the bid that originated them
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = 'public' 
                AND table_name = 'market_ledger' 
                AND column_name = 'bid_record_id'
            """)
            
            if cur.fetchone() is None:
                self.logger.info("Adding bid_record_id column to market_ledger")
                cur.execute(f"""
                    ALTER TABLE {self.SCHEMA}.market_ledger 
                    ADD COLUMN IF NOT EXISTS bid_record_id UUID 
                    REFERENCES {self.SCHEMA}.{self.TABLE_BID_RECORDS}(id) ON DELETE SET NULL
                """)
                
                # Create index for the foreign key
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_market_ledger_bid_record_id 
                    ON public.market_ledger(bid_record_id)
                """)
            
            self.conn.commit()
            self.logger.info("Bid record tables ensured in schema '%s'", self.SCHEMA)
            
        except Exception as e:
            self.logger.error("Error creating tables: %s", str(e))
            self.conn.rollback()
            raise
        finally:
            cur.close()
    
    def save_bid_record(
        self,
        fsp_id: str,
        slot_start: datetime,
        slot_end: datetime,
        orders: List[Dict],
        strategy_id: str = None,
        strategy_name: str = None,
        strategy_description: str = None,
        assets_to_activate: List[Dict] = None,
        total_quantity_mw: float = None
    ) -> str:
        """
        Save a bid record to the database.
        
        :param fsp_id: FSP identifier
        :param slot_start: Start of time slot
        :param slot_end: End of time slot
        :param orders: List of order dictionaries
        :param strategy_id: Strategy ID used (optional)
        :param strategy_name: Strategy name (optional)
        :param strategy_description: Strategy description (optional)
        :param assets_to_activate: List of asset dictionaries (optional)
        :param total_quantity_mw: Total quantity bid in MW (optional)
        :return: UUID of the inserted/updated bid record (as string)
        """
        cur = self.conn.cursor()
        
        try:
            # Calculate total quantity if not provided
            if total_quantity_mw is None:
                total_quantity_mw = sum(o.get("quantity_mw", 0) for o in orders)
            
            # Check if record already exists (update if so)
            cur.execute(f"""
                SELECT id FROM {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                WHERE fsp_id = %s AND slot_start = %s
            """, (fsp_id, slot_start))
            
            existing = cur.fetchone()
            
            if existing:
                # Update existing record
                bid_record_id = existing[0]
                cur.execute(f"""
                    UPDATE {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                    SET strategy_id = %s, strategy_name = %s, strategy_description = %s,
                        total_quantity_mw = %s, created_at = CURRENT_TIMESTAMP, status = 'pending'
                    WHERE id = %s
                """, (strategy_id, strategy_name, strategy_description, total_quantity_mw, bid_record_id))
                
                # Delete old orders (always refresh orders on update)
                cur.execute(f"DELETE FROM {self.SCHEMA}.{self.TABLE_ORDERS} WHERE bid_record_id = %s", (bid_record_id,))
                
                # Only delete assets if new ones are provided (None means keep existing)
                if assets_to_activate is not None:
                    cur.execute(f"DELETE FROM {self.SCHEMA}.{self.TABLE_ASSETS} WHERE bid_record_id = %s", (bid_record_id,))
                
                self.logger.info("Updated existing bid record ID %s", bid_record_id)
            else:
                # Insert new record
                cur.execute(f"""
                    INSERT INTO {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                    (fsp_id, slot_start, slot_end, strategy_id, strategy_name, strategy_description, total_quantity_mw)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (fsp_id, slot_start, slot_end, strategy_id, strategy_name, strategy_description, total_quantity_mw))
                
                bid_record_id = cur.fetchone()[0]
                self.logger.info("Inserted new bid record ID %s", bid_record_id)
            
            # Insert orders
            for order in orders:
                period_from = order.get("period_from")
                period_to = order.get("period_to")
                
                # Parse datetime strings if needed
                if isinstance(period_from, str):
                    period_from = datetime.fromisoformat(period_from.replace("Z", "+00:00")).replace(tzinfo=None)
                if isinstance(period_to, str):
                    period_to = datetime.fromisoformat(period_to.replace("Z", "+00:00")).replace(tzinfo=None)
                
                cur.execute(f"""
                    INSERT INTO {self.SCHEMA}.{self.TABLE_ORDERS}
                    (bid_record_id, portfolio, regulation_type, quantity_mw, unit_price, time_slot_name, period_from, period_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    bid_record_id,
                    order.get("portfolio"),
                    order.get("regulation_type"),
                    order.get("quantity_mw"),
                    order.get("unit_price"),
                    order.get("time_slot"),
                    period_from,
                    period_to
                ))
            
            # Insert assets
            if assets_to_activate:
                for asset in assets_to_activate:
                    cur.execute(f"""
                        INSERT INTO {self.SCHEMA}.{self.TABLE_ASSETS}
                        (bid_record_id, asset_id, description, asset_type, available_flexibility_kw, flexibility_factor)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        bid_record_id,
                        asset.get("asset_id"),
                        asset.get("description"),
                        asset.get("asset_type"),
                        asset.get("available_flexibility_kw"),
                        asset.get("flexibility_factor")
                    ))
            
            self.conn.commit()
            # Return UUID as string for consistency
            return str(bid_record_id)
            
        except Exception as e:
            self.logger.error("Error saving bid record: %s", str(e))
            self.conn.rollback()
            raise
        finally:
            cur.close()
    
    def get_bid_record(self, fsp_id: str, slot_start: datetime) -> Optional[Dict]:
        """
        Get a bid record for a specific FSP and time slot.
        
        :param fsp_id: FSP identifier
        :param slot_start: Start of time slot
        :return: Bid record dictionary or None
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            # Get main record
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                WHERE fsp_id = %s AND slot_start = %s
            """, (fsp_id, slot_start))
            
            record = cur.fetchone()
            if not record:
                return None
            
            record = dict(record)
            bid_record_id = record["id"]
            
            # Get orders
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_ORDERS}
                WHERE bid_record_id = %s
            """, (bid_record_id,))
            
            record["orders"] = [dict(row) for row in cur.fetchall()]
            
            # Get assets
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_ASSETS}
                WHERE bid_record_id = %s
            """, (bid_record_id,))
            
            record["assets_to_activate"] = [dict(row) for row in cur.fetchall()]
            
            # Build strategy dict for compatibility
            record["strategy"] = {
                "id": record.get("strategy_id"),
                "name": record.get("strategy_name"),
                "description": record.get("strategy_description"),
            } if record.get("strategy_id") else None
            
            self.logger.info("Loaded bid record ID %s for %s @ %s", 
                           bid_record_id, fsp_id, slot_start)
            
            return record
            
        except Exception as e:
            self.logger.error("Error getting bid record: %s", str(e))
            return None
        finally:
            cur.close()
    
    def get_allowed_assets(self, fsp_id: str, slot_start: datetime) -> List[str]:
        """
        Get list of asset IDs that should be activated.
        
        :param fsp_id: FSP identifier
        :param slot_start: Start of time slot
        :return: List of asset IDs
        """
        cur = self.conn.cursor()
        
        try:
            cur.execute(f"""
                SELECT a.asset_id 
                FROM {self.SCHEMA}.{self.TABLE_ASSETS} a
                JOIN {self.SCHEMA}.{self.TABLE_BID_RECORDS} b ON a.bid_record_id = b.id
                WHERE b.fsp_id = %s AND b.slot_start = %s
            """, (fsp_id, slot_start))
            
            return [row[0] for row in cur.fetchall()]
            
        except Exception as e:
            self.logger.error("Error getting allowed assets: %s", str(e))
            return []
        finally:
            cur.close()
    
    def mark_activated(self, fsp_id: str, slot_start: datetime) -> bool:
        """
        Mark a bid record as activated.
        
        :param fsp_id: FSP identifier
        :param slot_start: Start of time slot
        :return: True if updated, False otherwise
        """
        cur = self.conn.cursor()
        
        try:
            cur.execute(f"""
                UPDATE {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                SET status = 'activated', activated_at = CURRENT_TIMESTAMP
                WHERE fsp_id = %s AND slot_start = %s
            """, (fsp_id, slot_start))
            
            self.conn.commit()
            return cur.rowcount > 0
            
        except Exception as e:
            self.logger.error("Error marking bid record as activated: %s", str(e))
            self.conn.rollback()
            return False
        finally:
            cur.close()
    
    def get_pending_activations(self, fsp_id: str = None) -> List[Dict]:
        """
        Get all pending bid records that haven't been activated yet.
        
        :param fsp_id: Optional FSP filter
        :return: List of bid record dictionaries
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            if fsp_id:
                cur.execute(f"""
                    SELECT * FROM {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                    WHERE fsp_id = %s AND status = 'pending' AND slot_start > CURRENT_TIMESTAMP
                    ORDER BY slot_start ASC
                """, (fsp_id,))
            else:
                cur.execute(f"""
                    SELECT * FROM {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                    WHERE status = 'pending' AND slot_start > CURRENT_TIMESTAMP
                    ORDER BY slot_start ASC
                """)
            
            return [dict(row) for row in cur.fetchall()]
            
        except Exception as e:
            self.logger.error("Error getting pending activations: %s", str(e))
            return []
        finally:
            cur.close()
    
    def get_history(
        self, 
        fsp_id: str, 
        start_date: datetime, 
        end_date: datetime
    ) -> List[Dict]:
        """
        Get bid record history for a period.
        
        :param fsp_id: FSP identifier
        :param start_date: Start of period
        :param end_date: End of period
        :return: List of bid record dictionaries
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                WHERE fsp_id = %s AND slot_start >= %s AND slot_start < %s
                ORDER BY slot_start ASC
            """, (fsp_id, start_date, end_date))
            
            return [dict(row) for row in cur.fetchall()]
            
        except Exception as e:
            self.logger.error("Error getting history: %s", str(e))
            return []
        finally:
            cur.close()
    
    def cleanup_old_records(self, days_to_keep: int = 30) -> int:
        """
        Delete bid records older than specified days.
        
        :param days_to_keep: Number of days to keep
        :return: Number of deleted records
        """
        cur = self.conn.cursor()
        
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
            
            cur.execute(f"""
                DELETE FROM {self.SCHEMA}.{self.TABLE_BID_RECORDS}
                WHERE slot_start < %s
            """, (cutoff,))
            
            deleted = cur.rowcount
            self.conn.commit()
            
            self.logger.info("Cleaned up %d old bid records", deleted)
            return deleted
            
        except Exception as e:
            self.logger.error("Error cleaning up old records: %s", str(e))
            self.conn.rollback()
            return 0
        finally:
            cur.close()
