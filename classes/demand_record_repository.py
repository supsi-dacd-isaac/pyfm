# import section
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
import psycopg2
import psycopg2.extras


class DemandRecordRepository:
    """
    Repository for storing and retrieving demand records in PostgreSQL.
    
    This provides persistent storage for DSO (buyer) flexibility demands.
    Similar to BidRecordRepository for FSPs (sellers).
    
    Database schema (in public schema):
    - public.demand_records: Main demand record table (what DSO requested)
    - public.demand_record_orders: Individual orders placed for each demand
    
    Relationship with market_ledger:
    - market_ledger stores what actually happened (trades)
    - demand_records stores what DSO requested
    - market_ledger.demand_record_id links to demand_records.id
    """
    
    SCHEMA = "public"
    TABLE_DEMAND_RECORDS = "demand_records"
    TABLE_ORDERS = "demand_record_orders"
    
    def __init__(self, pg_interface, logger: logging.Logger):
        """
        Initialize the repository.
        
        :param pg_interface: PostgreSQLInterface instance with active connection
        :param logger: Logger instance
        """
        self.conn = pg_interface.conn
        self.logger = logger
        
        # Ensure tables exist
        self._ensure_tables()
    
    def _ensure_tables(self):
        """
        Create the demand record tables if they don't exist.
        Also adds demand_record_id foreign key to market_ledger if not present.
        
        Note: Run sql/demand_records_schema.sql for the full schema with comments and views.
        """
        cur = self.conn.cursor()
        
        try:
            # Ensure uuid-ossp extension is available
            cur.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
            
            # Main demand records table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS} (
                    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    dso_id VARCHAR(100) NOT NULL,
                    slot_start TIMESTAMP NOT NULL,
                    slot_end TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL,
                    quantity_up_mw DECIMAL(10, 6),
                    quantity_down_mw DECIMAL(10, 6),
                    quantity_unit VARCHAR(10) DEFAULT 'MW',
                    price_offered DECIMAL(10, 4),
                    currency VARCHAR(10) DEFAULT 'CHF',
                    regulation_type VARCHAR(50),
                    grid_area_id VARCHAR(100),
                    grid_area_name VARCHAR(200),
                    request_source VARCHAR(100),
                    request_reason TEXT,
                    status VARCHAR(50) DEFAULT 'pending',
                    matched_quantity_mw DECIMAL(10, 6),
                    matched_at TIMESTAMP,
                    CONSTRAINT uq_demand_records_dso_slot UNIQUE(dso_id, slot_start)
                )
            """)
            
            # Orders table
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.SCHEMA}.{self.TABLE_ORDERS} (
                    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
                    demand_record_id UUID NOT NULL REFERENCES {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}(id) ON DELETE CASCADE,
                    regulation_type VARCHAR(50),
                    quantity_mw DECIMAL(10, 6),
                    unit_price DECIMAL(10, 4),
                    period_from TIMESTAMP,
                    period_to TIMESTAMP,
                    order_side VARCHAR(20) DEFAULT 'Buy',
                    order_status VARCHAR(50),
                    nodes_order_id VARCHAR(200),
                    created_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') NOT NULL
                )
            """)
            
            # Create indexes
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_demand_records_dso_slot 
                ON {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}(dso_id, slot_start)
            """)
            
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_demand_records_status 
                ON {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}(status)
            """)
            
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_demand_record_orders_demand_id 
                ON {self.SCHEMA}.{self.TABLE_ORDERS}(demand_record_id)
            """)
            
            # Add foreign key column to market_ledger if it doesn't exist
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_schema = 'public' 
                AND table_name = 'market_ledger' 
                AND column_name = 'demand_record_id'
            """)
            
            if cur.fetchone() is None:
                self.logger.info("Adding demand_record_id column to market_ledger")
                cur.execute(f"""
                    ALTER TABLE {self.SCHEMA}.market_ledger 
                    ADD COLUMN IF NOT EXISTS demand_record_id UUID 
                    REFERENCES {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}(id) ON DELETE SET NULL
                """)
                
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_market_ledger_demand_record_id 
                    ON public.market_ledger(demand_record_id)
                """)
            
            self.conn.commit()
            self.logger.info("Demand record tables ensured in schema '%s'", self.SCHEMA)
            
        except Exception as e:
            self.logger.error("Error creating demand tables: %s", str(e))
            self.conn.rollback()
            raise
        finally:
            cur.close()
    
    def save_demand_record(
        self,
        dso_id: str,
        slot_start: datetime,
        slot_end: datetime,
        quantity_up_mw: float = None,
        quantity_down_mw: float = None,
        quantity_unit: str = "MW",
        price_offered: float = None,
        currency: str = "CHF",
        regulation_type: str = None,
        grid_area_id: str = None,
        grid_area_name: str = None,
        request_source: str = None,
        request_reason: str = None,
        orders: List[Dict] = None
    ) -> str:
        """
        Save a demand record to the database.
        
        :param dso_id: DSO identifier
        :param slot_start: Start of time slot
        :param slot_end: End of time slot
        :param quantity_up_mw: Upward regulation requested (MW)
        :param quantity_down_mw: Downward regulation requested (MW)
        :param quantity_unit: Unit (default: MW)
        :param price_offered: Price offered by DSO
        :param currency: Currency (default: CHF)
        :param regulation_type: Up, Down, or Both
        :param grid_area_id: Grid area ID (optional)
        :param grid_area_name: Grid area name (optional)
        :param request_source: Source of request (forecast, manual, etc.)
        :param request_reason: Why flexibility was requested
        :param orders: List of order dictionaries (optional)
        :return: UUID of the inserted/updated demand record (as string)
        """
        cur = self.conn.cursor()
        
        try:
            # Check if record already exists (update if so)
            cur.execute(f"""
                SELECT id FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                WHERE dso_id = %s AND slot_start = %s
            """, (dso_id, slot_start))
            
            existing = cur.fetchone()
            
            if existing:
                # Update existing record
                demand_record_id = existing[0]
                cur.execute(f"""
                    UPDATE {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                    SET quantity_up_mw = %s, quantity_down_mw = %s, quantity_unit = %s,
                        price_offered = %s, currency = %s, regulation_type = %s,
                        grid_area_id = %s, grid_area_name = %s,
                        request_source = %s, request_reason = %s,
                        created_at = CURRENT_TIMESTAMP, status = 'pending'
                    WHERE id = %s
                """, (quantity_up_mw, quantity_down_mw, quantity_unit,
                      price_offered, currency, regulation_type,
                      grid_area_id, grid_area_name,
                      request_source, request_reason, demand_record_id))
                
                # Delete old orders (refresh on update)
                cur.execute(f"DELETE FROM {self.SCHEMA}.{self.TABLE_ORDERS} WHERE demand_record_id = %s", 
                           (demand_record_id,))
                
                self.logger.info("Updated existing demand record ID %s", demand_record_id)
            else:
                # Insert new record
                cur.execute(f"""
                    INSERT INTO {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                    (dso_id, slot_start, slot_end, quantity_up_mw, quantity_down_mw, 
                     quantity_unit, price_offered, currency, regulation_type,
                     grid_area_id, grid_area_name, request_source, request_reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (dso_id, slot_start, slot_end, quantity_up_mw, quantity_down_mw,
                      quantity_unit, price_offered, currency, regulation_type,
                      grid_area_id, grid_area_name, request_source, request_reason))
                
                demand_record_id = cur.fetchone()[0]
                self.logger.info("Inserted new demand record ID %s", demand_record_id)
            
            # Insert orders if provided
            if orders:
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
                        (demand_record_id, regulation_type, quantity_mw, unit_price, 
                         period_from, period_to, order_status, nodes_order_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        demand_record_id,
                        order.get("regulation_type"),
                        order.get("quantity_mw"),
                        order.get("unit_price"),
                        period_from,
                        period_to,
                        order.get("order_status", "placed"),
                        order.get("nodes_order_id")
                    ))
            
            self.conn.commit()
            return str(demand_record_id)
            
        except Exception as e:
            self.logger.error("Error saving demand record: %s", str(e))
            self.conn.rollback()
            raise
        finally:
            cur.close()
    
    def get_demand_record(self, dso_id: str, slot_start: datetime) -> Optional[Dict]:
        """
        Get a demand record for a specific DSO and time slot.
        
        :param dso_id: DSO identifier
        :param slot_start: Start of time slot
        :return: Demand record dictionary or None
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            # Get main record
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                WHERE dso_id = %s AND slot_start = %s
            """, (dso_id, slot_start))
            
            record = cur.fetchone()
            if not record:
                return None
            
            record = dict(record)
            demand_record_id = record["id"]
            
            # Get orders
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_ORDERS}
                WHERE demand_record_id = %s
            """, (demand_record_id,))
            
            record["orders"] = [dict(row) for row in cur.fetchall()]
            
            self.logger.info("Loaded demand record ID %s for %s @ %s", 
                           demand_record_id, dso_id, slot_start)
            
            return record
            
        except Exception as e:
            self.logger.error("Error getting demand record: %s", str(e))
            return None
        finally:
            cur.close()
    
    def get_demand_for_slot(self, slot_start: datetime) -> List[Dict]:
        """
        Get all demand records for a specific time slot (from all DSOs).
        
        :param slot_start: Start of time slot
        :return: List of demand record dictionaries
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                WHERE slot_start = %s
            """, (slot_start,))
            
            return [dict(row) for row in cur.fetchall()]
            
        except Exception as e:
            self.logger.error("Error getting demand for slot: %s", str(e))
            return []
        finally:
            cur.close()
    
    def mark_matched(
        self, 
        dso_id: str, 
        slot_start: datetime, 
        matched_quantity_mw: float,
        partial: bool = False
    ) -> bool:
        """
        Mark a demand record as matched (fully or partially).
        
        :param dso_id: DSO identifier
        :param slot_start: Start of time slot
        :param matched_quantity_mw: How much was actually matched
        :param partial: True if only partially matched
        :return: True if updated, False otherwise
        """
        cur = self.conn.cursor()
        
        try:
            status = "partial" if partial else "matched"
            cur.execute(f"""
                UPDATE {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                SET status = %s, matched_quantity_mw = %s, matched_at = CURRENT_TIMESTAMP
                WHERE dso_id = %s AND slot_start = %s
            """, (status, matched_quantity_mw, dso_id, slot_start))
            
            self.conn.commit()
            return cur.rowcount > 0
            
        except Exception as e:
            self.logger.error("Error marking demand record as matched: %s", str(e))
            self.conn.rollback()
            return False
        finally:
            cur.close()
    
    def get_pending_demands(self, dso_id: str = None) -> List[Dict]:
        """
        Get all pending demand records that haven't been fully matched.
        
        :param dso_id: Optional DSO filter
        :return: List of demand record dictionaries
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            if dso_id:
                cur.execute(f"""
                    SELECT * FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                    WHERE dso_id = %s AND status IN ('pending', 'partial') 
                    AND slot_start > CURRENT_TIMESTAMP
                    ORDER BY slot_start ASC
                """, (dso_id,))
            else:
                cur.execute(f"""
                    SELECT * FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                    WHERE status IN ('pending', 'partial') 
                    AND slot_start > CURRENT_TIMESTAMP
                    ORDER BY slot_start ASC
                """)
            
            return [dict(row) for row in cur.fetchall()]
            
        except Exception as e:
            self.logger.error("Error getting pending demands: %s", str(e))
            return []
        finally:
            cur.close()
    
    def get_history(
        self, 
        dso_id: str, 
        start_date: datetime, 
        end_date: datetime
    ) -> List[Dict]:
        """
        Get demand record history for a period.
        
        :param dso_id: DSO identifier
        :param start_date: Start of period
        :param end_date: End of period
        :return: List of demand record dictionaries
        """
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        try:
            cur.execute(f"""
                SELECT * FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                WHERE dso_id = %s AND slot_start >= %s AND slot_start < %s
                ORDER BY slot_start ASC
            """, (dso_id, start_date, end_date))
            
            return [dict(row) for row in cur.fetchall()]
            
        except Exception as e:
            self.logger.error("Error getting demand history: %s", str(e))
            return []
        finally:
            cur.close()
    
    def cleanup_old_records(self, days_to_keep: int = 30) -> int:
        """
        Delete demand records older than specified days.
        
        :param days_to_keep: Number of days to keep
        :return: Number of deleted records
        """
        cur = self.conn.cursor()
        
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
            
            cur.execute(f"""
                DELETE FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                WHERE slot_start < %s
            """, (cutoff,))
            
            deleted = cur.rowcount
            self.conn.commit()
            
            self.logger.info("Cleaned up %d old demand records", deleted)
            return deleted
            
        except Exception as e:
            self.logger.error("Error cleaning up old records: %s", str(e))
            self.conn.rollback()
            return 0
        finally:
            cur.close()
    
    def get_total_demand_for_slot(self, slot_start: datetime) -> Dict:
        """
        Get aggregated demand for a slot across all DSOs.
        
        :param slot_start: Start of time slot
        :return: Dictionary with total_up_mw, total_down_mw, dso_count
        """
        cur = self.conn.cursor()
        
        try:
            cur.execute(f"""
                SELECT 
                    COALESCE(SUM(quantity_up_mw), 0) AS total_up_mw,
                    COALESCE(SUM(quantity_down_mw), 0) AS total_down_mw,
                    COUNT(*) AS dso_count
                FROM {self.SCHEMA}.{self.TABLE_DEMAND_RECORDS}
                WHERE slot_start = %s
            """, (slot_start,))
            
            row = cur.fetchone()
            return {
                "total_up_mw": float(row[0]),
                "total_down_mw": float(row[1]),
                "dso_count": row[2]
            }
            
        except Exception as e:
            self.logger.error("Error getting total demand: %s", str(e))
            return {"total_up_mw": 0, "total_down_mw": 0, "dso_count": 0}
        finally:
            cur.close()
