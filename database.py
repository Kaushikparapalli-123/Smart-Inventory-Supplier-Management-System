"""
PostgreSQL data layer for Smart Inventory.

The database is the source of truth. Products, suppliers, supplier-product
relationships, inventory counts, purchase orders and configuration are all
stored in PostgreSQL. No demo products or suppliers are inserted automatically.
"""

import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime

from dotenv import load_dotenv
import psycopg2
import psycopg2.extras

load_dotenv()

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "dbname": os.environ.get("DB_NAME", "pizza_inventory"),
}


class _Connection:
    def __init__(self, raw_conn):
        self._raw = raw_conn

    def execute(self, sql, params=None):
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


@contextmanager
def get_connection():
    raw = psycopg2.connect(**DB_CONFIG)
    conn = _Connection(raw)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create/upgrade the PostgreSQL schema without inserting business data."""
    statements = [
        """
        CREATE TABLE IF NOT EXISTS supplier (
            supplier_id     SERIAL PRIMARY KEY,
            name            VARCHAR(255) NOT NULL,
            contact_email   VARCHAR(255),
            phone           VARCHAR(50),
            lead_time_days  INT DEFAULT 7,
            active          BOOLEAN NOT NULL DEFAULT TRUE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS product (
            product_id          SERIAL PRIMARY KEY,
            sku                 VARCHAR(50) UNIQUE NOT NULL,
            name                VARCHAR(255) NOT NULL,
            category            VARCHAR(100),
            reorder_threshold   INT NOT NULL DEFAULT 0,
            current_stock       NUMERIC(12,2) NOT NULL DEFAULT 0,
            unit                VARCHAR(30) NOT NULL DEFAULT 'units',
            usual_order_qty     INT NOT NULL DEFAULT 0,
            max_stock_level     INT,
            shelf_life_days     INT,
            active              BOOLEAN NOT NULL DEFAULT TRUE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS product_supplier (
            product_id   INT NOT NULL REFERENCES product(product_id) ON DELETE CASCADE,
            supplier_id  INT NOT NULL REFERENCES supplier(supplier_id),
            unit_cost    DECIMAL(10,2),
            is_primary   SMALLINT NOT NULL DEFAULT 0,
            PRIMARY KEY (product_id, supplier_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS stock_movement (
            movement_id    SERIAL PRIMARY KEY,
            product_id     INT NOT NULL REFERENCES product(product_id),
            movement_type  VARCHAR(20) NOT NULL
                           CHECK (movement_type IN ('purchase', 'sale', 'adjustment')),
            quantity       NUMERIC(12,2) NOT NULL,
            timestamp      VARCHAR(20) NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS purchase_order (
            order_id      SERIAL PRIMARY KEY,
            supplier_id   INT NOT NULL REFERENCES supplier(supplier_id),
            status        VARCHAR(20) NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'sent', 'received')),
            created_at    VARCHAR(20) NOT NULL,
            sent_at       VARCHAR(20),
            received_at   VARCHAR(20)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS purchase_order_item (
            order_id    INT NOT NULL REFERENCES purchase_order(order_id) ON DELETE CASCADE,
            product_id  INT NOT NULL REFERENCES product(product_id),
            quantity    INT NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (order_id, product_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_stock_count (
            count_id      SERIAL PRIMARY KEY,
            count_date    DATE UNIQUE NOT NULL,
            status        VARCHAR(20) NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'completed')),
            started_at    VARCHAR(20) NOT NULL,
            completed_at  VARCHAR(20)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_stock_count_item (
            count_id    INT NOT NULL REFERENCES daily_stock_count(count_id) ON DELETE CASCADE,
            product_id  INT NOT NULL REFERENCES product(product_id),
            quantity    NUMERIC(12,2) NOT NULL CHECK (quantity >= 0),
            PRIMARY KEY (count_id, product_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_usage (
            usage_date     DATE NOT NULL,
            product_id     INT NOT NULL REFERENCES product(product_id),
            quantity_used  NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (quantity_used >= 0),
            movement_id    INT REFERENCES stock_movement(movement_id) ON DELETE SET NULL,
            updated_at     VARCHAR(20) NOT NULL,
            PRIMARY KEY (usage_date, product_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS business_setting (
            setting_key    VARCHAR(100) PRIMARY KEY,
            setting_value  TEXT
        );
        """,
        "ALTER TABLE supplier ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE product ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;",
        "ALTER TABLE product ADD COLUMN IF NOT EXISTS unit VARCHAR(30) NOT NULL DEFAULT 'units';",
        "ALTER TABLE product ADD COLUMN IF NOT EXISTS usual_order_qty INT NOT NULL DEFAULT 0;",
        "ALTER TABLE product ADD COLUMN IF NOT EXISTS max_stock_level INT;",
        "ALTER TABLE product ADD COLUMN IF NOT EXISTS shelf_life_days INT;",
        "DROP VIEW IF EXISTS inventory_overview;",
        "ALTER TABLE product ALTER COLUMN current_stock TYPE NUMERIC(12,2) USING current_stock::numeric;",
        "ALTER TABLE stock_movement ALTER COLUMN quantity TYPE NUMERIC(12,2) USING quantity::numeric;",
        "ALTER TABLE daily_stock_count_item ALTER COLUMN quantity TYPE NUMERIC(12,2) USING quantity::numeric;",
        "ALTER TABLE daily_usage ALTER COLUMN quantity_used TYPE NUMERIC(12,2) USING quantity_used::numeric;",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS one_primary_supplier_per_product
        ON product_supplier(product_id) WHERE is_primary = 1;
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_stock_movement_product_time
        ON stock_movement(product_id, timestamp);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_purchase_order_status
        ON purchase_order(status, supplier_id);
        """,
        "DROP VIEW IF EXISTS inventory_overview;",
        """
        CREATE VIEW inventory_overview AS
        SELECT
            p.product_id,
            p.name AS product_name,
            p.category,
            p.current_stock,
            p.unit,
            p.reorder_threshold AS safety_stock,
            p.usual_order_qty,
            p.max_stock_level,
            p.shelf_life_days,
            CASE WHEN p.current_stock < p.reorder_threshold THEN 'LOW' ELSE 'OK' END AS stock_status,
            COALESCE(s.name, 'No supplier') AS primary_supplier,
            s.lead_time_days,
            ps.unit_cost
        FROM product p
        LEFT JOIN product_supplier ps
               ON ps.product_id = p.product_id AND ps.is_primary = 1
        LEFT JOIN supplier s
               ON s.supplier_id = ps.supplier_id
        WHERE p.active = TRUE;
        """,
    ]

    with get_connection() as conn:
        for stmt in statements:
            conn.execute(stmt)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key, default=None):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT setting_value FROM business_setting WHERE setting_key=%s",
            (key,),
        ).fetchone()
        return row["setting_value"] if row else default


def set_setting(key, value):
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO business_setting(setting_key, setting_value)
            VALUES (%s, %s)
            ON CONFLICT (setting_key)
            DO UPDATE SET setting_value=EXCLUDED.setting_value
            """,
            (key, str(value)),
        )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

def _generate_internal_sku():
    return "AUTO-" + uuid.uuid4().hex[:12].upper()


def get_products_with_alerts(include_inactive=False):
    where = "" if include_inactive else "WHERE active=TRUE"
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT product_id, name, category, current_stock, unit,
                   reorder_threshold, usual_order_qty, max_stock_level,
                   shelf_life_days, active,
                   CASE WHEN current_stock < reorder_threshold THEN 1 ELSE 0 END AS needs_reorder
            FROM product
            {where}
            ORDER BY active DESC, category, name
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_products_admin(include_inactive=True):
    where = "" if include_inactive else "WHERE p.active=TRUE"
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                p.product_id,
                p.name,
                p.category,
                p.current_stock,
                p.unit,
                p.reorder_threshold,
                p.usual_order_qty,
                p.max_stock_level,
                p.shelf_life_days,
                p.active,
                ps.supplier_id AS primary_supplier_id,
                s.name AS primary_supplier_name,
                ps.unit_cost
            FROM product p
            LEFT JOIN product_supplier ps
                   ON ps.product_id=p.product_id AND ps.is_primary=1
            LEFT JOIN supplier s ON s.supplier_id=ps.supplier_id
            {where}
            ORDER BY p.active DESC, p.category, p.name
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_manager_inventory():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.product_id,
                p.name AS product_name,
                p.category,
                p.current_stock,
                p.unit,
                p.reorder_threshold,
                p.usual_order_qty,
                p.max_stock_level,
                p.shelf_life_days,
                COALESCE(s.name, 'No supplier') AS supplier_name,
                s.supplier_id,
                s.lead_time_days,
                ps.unit_cost,
                CASE WHEN p.current_stock < p.reorder_threshold THEN 1 ELSE 0 END AS needs_reorder
            FROM product p
            LEFT JOIN product_supplier ps
                   ON ps.product_id=p.product_id AND ps.is_primary=1
            LEFT JOIN supplier s
                   ON s.supplier_id=ps.supplier_id AND s.active=TRUE
            WHERE p.active=TRUE
            ORDER BY p.category, p.name
            """
        ).fetchall()
        return [dict(r) for r in rows]


def add_manager_product(
    name,
    category,
    unit,
    reorder_threshold,
    current_stock,
    usual_order_qty=0,
    max_stock_level=None,
    shelf_life_days=None,
    supplier_id=None,
    unit_cost=None,
):
    name = (name or "").strip()
    category = (category or "Other").strip()
    unit = (unit or "units").strip()
    if not name:
        raise ValueError("Product name is required.")

    current_stock = float(current_stock)
    safety = int(reorder_threshold)
    usual = int(usual_order_qty)
    max_stock = None if max_stock_level in (None, "", 0) else int(max_stock_level)
    shelf_life = None if shelf_life_days in (None, "", 0) else int(shelf_life_days)

    if min(current_stock, safety, usual) < 0:
        raise ValueError("Stock values cannot be negative.")
    if max_stock is not None and max_stock < safety:
        raise ValueError("Maximum stock must be at least as high as the safety stock.")

    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO product
                (sku, name, category, reorder_threshold, current_stock, unit,
                 usual_order_qty, max_stock_level, shelf_life_days, active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            RETURNING product_id
            """,
            (
                _generate_internal_sku(), name, category, safety, current_stock, unit,
                usual, max_stock, shelf_life,
            ),
        )
        product_id = cur.fetchone()["product_id"]

        if supplier_id is not None:
            conn.execute(
                """
                INSERT INTO product_supplier(product_id, supplier_id, unit_cost, is_primary)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (product_id, supplier_id)
                DO UPDATE SET unit_cost=EXCLUDED.unit_cost, is_primary=1
                """,
                (
                    product_id,
                    int(supplier_id),
                    None if unit_cost in (None, "") else float(unit_cost),
                ),
            )
        return product_id


def update_manager_product(
    product_id,
    name,
    category,
    unit,
    reorder_threshold,
    usual_order_qty,
    max_stock_level=None,
    shelf_life_days=None,
    supplier_id=None,
    unit_cost=None,
):
    name = (name or "").strip()
    category = (category or "Other").strip()
    unit = (unit or "units").strip()
    if not name:
        raise ValueError("Product name is required.")

    safety = int(reorder_threshold)
    usual = int(usual_order_qty)
    max_stock = None if max_stock_level in (None, "", 0) else int(max_stock_level)
    shelf_life = None if shelf_life_days in (None, "", 0) else int(shelf_life_days)
    if min(safety, usual) < 0:
        raise ValueError("Stock values cannot be negative.")
    if max_stock is not None and max_stock < safety:
        raise ValueError("Maximum stock must be at least as high as the safety stock.")

    with get_connection() as conn:
        conn.execute(
            """
            UPDATE product
            SET name=%s, category=%s, unit=%s, reorder_threshold=%s,
                usual_order_qty=%s, max_stock_level=%s, shelf_life_days=%s
            WHERE product_id=%s
            """,
            (
                name, category, unit, safety, usual, max_stock, shelf_life,
                int(product_id),
            ),
        )

        conn.execute(
            "UPDATE product_supplier SET is_primary=0 WHERE product_id=%s",
            (int(product_id),),
        )
        if supplier_id is not None:
            conn.execute(
                """
                INSERT INTO product_supplier(product_id, supplier_id, unit_cost, is_primary)
                VALUES (%s, %s, %s, 1)
                ON CONFLICT (product_id, supplier_id)
                DO UPDATE SET unit_cost=EXCLUDED.unit_cost, is_primary=1
                """,
                (
                    int(product_id),
                    int(supplier_id),
                    None if unit_cost in (None, "") else float(unit_cost),
                ),
            )


def set_product_active(product_id, active):
    with get_connection() as conn:
        conn.execute(
            "UPDATE product SET active=%s WHERE product_id=%s",
            (bool(active), int(product_id)),
        )


def delete_product_permanently(product_id):
    """Delete only products with no operational history."""
    with get_connection() as conn:
        checks = [
            ("stock_movement", "product_id"),
            ("purchase_order_item", "product_id"),
            ("daily_stock_count_item", "product_id"),
        ]
        for table, column in checks:
            found = conn.execute(
                f"SELECT 1 FROM {table} WHERE {column}=%s LIMIT 1",
                (int(product_id),),
            ).fetchone()
            if found:
                raise ValueError(
                    "This product has history and cannot be permanently deleted. Archive it instead."
                )
        conn.execute("DELETE FROM product WHERE product_id=%s", (int(product_id),))


# ---------------------------------------------------------------------------
# Suppliers and supplier-product relationships
# ---------------------------------------------------------------------------

def get_suppliers(include_inactive=False):
    where = "" if include_inactive else "WHERE active=TRUE"
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM supplier {where} ORDER BY active DESC, name"
        ).fetchall()
        return [dict(r) for r in rows]


def add_supplier(name, contact_email, phone, lead_time_days):
    name = (name or "").strip()
    if not name:
        raise ValueError("Supplier name is required.")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO supplier(name, contact_email, phone, lead_time_days, active)
            VALUES (%s, %s, %s, %s, TRUE)
            RETURNING supplier_id
            """,
            (name, (contact_email or "").strip(), (phone or "").strip(), int(lead_time_days)),
        )
        return cur.fetchone()["supplier_id"]


def update_supplier(supplier_id, name, contact_email, phone, lead_time_days):
    name = (name or "").strip()
    if not name:
        raise ValueError("Supplier name is required.")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE supplier
            SET name=%s, contact_email=%s, phone=%s, lead_time_days=%s
            WHERE supplier_id=%s
            """,
            (
                name,
                (contact_email or "").strip(),
                (phone or "").strip(),
                int(lead_time_days),
                int(supplier_id),
            ),
        )


def set_supplier_active(supplier_id, active):
    with get_connection() as conn:
        conn.execute(
            "UPDATE supplier SET active=%s WHERE supplier_id=%s",
            (bool(active), int(supplier_id)),
        )


def delete_supplier_permanently(supplier_id):
    with get_connection() as conn:
        if conn.execute(
            "SELECT 1 FROM product_supplier WHERE supplier_id=%s LIMIT 1",
            (int(supplier_id),),
        ).fetchone():
            raise ValueError("Remove this supplier from all products before deleting it.")
        if conn.execute(
            "SELECT 1 FROM purchase_order WHERE supplier_id=%s LIMIT 1",
            (int(supplier_id),),
        ).fetchone():
            raise ValueError(
                "This supplier has order history and cannot be permanently deleted. Archive it instead."
            )
        conn.execute("DELETE FROM supplier WHERE supplier_id=%s", (int(supplier_id),))


def get_product_supplier_links(product_id=None, supplier_id=None):
    clauses = []
    params = []
    if product_id is not None:
        clauses.append("ps.product_id=%s")
        params.append(int(product_id))
    if supplier_id is not None:
        clauses.append("ps.supplier_id=%s")
        params.append(int(supplier_id))
    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT
                ps.product_id,
                p.name AS product_name,
                p.category,
                p.unit,
                ps.supplier_id,
                s.name AS supplier_name,
                ps.unit_cost,
                ps.is_primary,
                s.lead_time_days,
                s.active AS supplier_active
            FROM product_supplier ps
            JOIN product p ON p.product_id=ps.product_id
            JOIN supplier s ON s.supplier_id=ps.supplier_id
            {where}
            ORDER BY s.name, ps.is_primary DESC, p.name
            """,
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def get_supplier_catalog(supplier_id):
    return get_product_supplier_links(supplier_id=supplier_id)


def get_unlinked_products_for_supplier(supplier_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.product_id, p.name, p.category, p.unit
            FROM product p
            WHERE p.active=TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM product_supplier ps
                  WHERE ps.product_id=p.product_id AND ps.supplier_id=%s
              )
            ORDER BY p.category, p.name
            """,
            (int(supplier_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_supplier_primary_products(supplier_id):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT p.product_id, p.name
            FROM product p
            JOIN product_supplier ps
              ON ps.product_id=p.product_id AND ps.is_primary=1
            WHERE ps.supplier_id=%s AND p.active=TRUE
            ORDER BY p.name
            """,
            (int(supplier_id),),
        ).fetchall()
        return [dict(r) for r in rows]


def link_product_supplier(product_id, supplier_id, unit_cost=None, is_primary=False):
    with get_connection() as conn:
        if is_primary:
            conn.execute(
                "UPDATE product_supplier SET is_primary=0 WHERE product_id=%s",
                (int(product_id),),
            )
        conn.execute(
            """
            INSERT INTO product_supplier(product_id, supplier_id, unit_cost, is_primary)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (product_id, supplier_id)
            DO UPDATE SET unit_cost=EXCLUDED.unit_cost, is_primary=EXCLUDED.is_primary
            """,
            (
                int(product_id),
                int(supplier_id),
                None if unit_cost in (None, "") else float(unit_cost),
                1 if is_primary else 0,
            ),
        )


def set_primary_supplier(product_id, supplier_id):
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM product_supplier WHERE product_id=%s AND supplier_id=%s",
            (int(product_id), int(supplier_id)),
        ).fetchone()
        if not exists:
            raise ValueError("Link this supplier to the product first.")
        conn.execute(
            "UPDATE product_supplier SET is_primary=0 WHERE product_id=%s",
            (int(product_id),),
        )
        conn.execute(
            """
            UPDATE product_supplier SET is_primary=1
            WHERE product_id=%s AND supplier_id=%s
            """,
            (int(product_id), int(supplier_id)),
        )


def unlink_product_supplier(product_id, supplier_id):
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT is_primary FROM product_supplier
            WHERE product_id=%s AND supplier_id=%s
            """,
            (int(product_id), int(supplier_id)),
        ).fetchone()
        if not row:
            return
        conn.execute(
            "DELETE FROM product_supplier WHERE product_id=%s AND supplier_id=%s",
            (int(product_id), int(supplier_id)),
        )
        if row["is_primary"]:
            replacement = conn.execute(
                """
                SELECT supplier_id FROM product_supplier
                WHERE product_id=%s ORDER BY supplier_id LIMIT 1
                """,
                (int(product_id),),
            ).fetchone()
            if replacement:
                conn.execute(
                    """
                    UPDATE product_supplier SET is_primary=1
                    WHERE product_id=%s AND supplier_id=%s
                    """,
                    (int(product_id), replacement["supplier_id"]),
                )



# ---------------------------------------------------------------------------
# Daily usage entry
# ---------------------------------------------------------------------------

def get_daily_usage(usage_date=None):
    """Return every active product and today's saved usage quantity."""
    usage_date = usage_date or date.today()

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                p.product_id,
                p.name AS product_name,
                p.category,
                p.unit,
                COALESCE(d.quantity_used, 0) AS quantity_used
            FROM product p
            LEFT JOIN daily_usage d
                   ON d.product_id = p.product_id
                  AND d.usage_date = %s
            WHERE p.active = TRUE
            ORDER BY COALESCE(p.category, 'Other'), p.name
            """,
            (usage_date,),
        ).fetchall()
        return [dict(row) for row in rows]


def save_daily_usage(usage_date, product_quantities):
    """
    Save the amount used today and update current stock safely.

    Re-saving the same day replaces the previous amount instead of subtracting
    it twice. A matching 'sale' stock_movement is kept in sync so forecasting
    can learn from daily usage.
    """
    usage_date = usage_date or date.today()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    with get_connection() as conn:
        for product_id, new_quantity in product_quantities.items():
            product_id = int(product_id)
            new_quantity = float(new_quantity or 0)

            if new_quantity < 0:
                raise ValueError("Usage cannot be negative.")

            product = conn.execute(
                """
                SELECT current_stock
                FROM product
                WHERE product_id=%s
                FOR UPDATE
                """,
                (product_id,),
            ).fetchone()

            if not product:
                continue

            existing = conn.execute(
                """
                SELECT quantity_used, movement_id
                FROM daily_usage
                WHERE usage_date=%s AND product_id=%s
                FOR UPDATE
                """,
                (usage_date, product_id),
            ).fetchone()

            old_quantity = float(existing["quantity_used"]) if existing else 0.0
            movement_id = existing["movement_id"] if existing else None

            current_stock = float(product["current_stock"])
            available_before_today_usage = current_stock + old_quantity

            if new_quantity > available_before_today_usage:
                raise ValueError(
                    "Usage entered is greater than the available stock for one or more products."
                )

            new_stock = available_before_today_usage - new_quantity

            if movement_id:
                if new_quantity == 0:
                    conn.execute(
                        "DELETE FROM stock_movement WHERE movement_id=%s",
                        (movement_id,),
                    )
                    movement_id = None
                else:
                    conn.execute(
                        """
                        UPDATE stock_movement
                        SET quantity=%s, timestamp=%s
                        WHERE movement_id=%s
                        """,
                        (-new_quantity, timestamp, movement_id),
                    )
            elif new_quantity > 0:
                cur = conn.execute(
                    """
                    INSERT INTO stock_movement
                        (product_id, movement_type, quantity, timestamp)
                    VALUES (%s, 'sale', %s, %s)
                    RETURNING movement_id
                    """,
                    (product_id, -new_quantity, timestamp),
                )
                movement_id = cur.fetchone()["movement_id"]

            conn.execute(
                """
                INSERT INTO daily_usage
                    (usage_date, product_id, quantity_used, movement_id, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (usage_date, product_id)
                DO UPDATE SET
                    quantity_used = EXCLUDED.quantity_used,
                    movement_id = EXCLUDED.movement_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (usage_date, product_id, new_quantity, movement_id, timestamp),
            )

            conn.execute(
                "UPDATE product SET current_stock=%s WHERE product_id=%s",
                (new_stock, product_id),
            )


def daily_usage_saved(usage_date=None):
    usage_date = usage_date or date.today()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM daily_usage WHERE usage_date=%s",
            (usage_date,),
        ).fetchone()
        return int(row["cnt"]) > 0


# ---------------------------------------------------------------------------
# Stock and nightly inventory count
# ---------------------------------------------------------------------------

def get_stock_movements(limit=500):
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT m.movement_id, m.product_id, p.name AS product_name, p.unit,
                   m.movement_type, m.quantity, m.timestamp
            FROM stock_movement m
            JOIN product p ON p.product_id=m.product_id
            ORDER BY m.timestamp DESC, m.movement_id DESC
            LIMIT %s
            """,
            (int(limit),),
        ).fetchall()
        return [dict(r) for r in rows]


def record_stock_movement(product_id, movement_type, quantity):
    if movement_type not in {"purchase", "sale", "adjustment"}:
        raise ValueError("Invalid movement type.")
    quantity = float(quantity)
    if quantity == 0:
        raise ValueError("Quantity cannot be zero.")

    if movement_type == "purchase":
        signed_qty = abs(quantity)
    elif movement_type == "sale":
        signed_qty = -abs(quantity)
    else:
        signed_qty = quantity

    with get_connection() as conn:
        product = conn.execute(
            "SELECT current_stock FROM product WHERE product_id=%s FOR UPDATE",
            (int(product_id),),
        ).fetchone()
        if not product:
            raise ValueError("Product not found.")
        new_stock = float(product["current_stock"]) + signed_qty
        if new_stock < 0:
            raise ValueError("This would make stock negative.")

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn.execute(
            """
            INSERT INTO stock_movement(product_id, movement_type, quantity, timestamp)
            VALUES (%s, %s, %s, %s)
            """,
            (int(product_id), movement_type, signed_qty, ts),
        )
        conn.execute(
            "UPDATE product SET current_stock=%s WHERE product_id=%s",
            (new_stock, int(product_id)),
        )


def set_current_stock(product_id, new_count):
    new_count = float(new_count)
    if new_count < 0:
        raise ValueError("Stock count cannot be negative.")
    with get_connection() as conn:
        product = conn.execute(
            "SELECT current_stock FROM product WHERE product_id=%s FOR UPDATE",
            (int(product_id),),
        ).fetchone()
        if not product:
            raise ValueError("Product not found.")
        old_count = float(product["current_stock"])
        delta = new_count - old_count
        if delta == 0:
            return 0
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        movement_type = "sale" if delta < 0 else "adjustment"
        conn.execute(
            """
            INSERT INTO stock_movement(product_id, movement_type, quantity, timestamp)
            VALUES (%s, %s, %s, %s)
            """,
            (int(product_id), movement_type, delta, ts),
        )
        conn.execute(
            "UPDATE product SET current_stock=%s WHERE product_id=%s",
            (new_count, int(product_id)),
        )
        return delta


def _get_or_create_daily_count(conn, count_date):
    row = conn.execute(
        "SELECT * FROM daily_stock_count WHERE count_date=%s",
        (count_date,),
    ).fetchone()
    if row:
        return row
    cur = conn.execute(
        """
        INSERT INTO daily_stock_count(count_date, status, started_at)
        VALUES (%s, 'draft', %s)
        RETURNING *
        """,
        (count_date, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    return cur.fetchone()


def get_daily_count(count_date=None):
    count_date = count_date or date.today()
    with get_connection() as conn:
        count = _get_or_create_daily_count(conn, count_date)
        items = conn.execute(
            """
            SELECT d.product_id, d.quantity, p.name, p.category, p.unit
            FROM daily_stock_count_item d
            JOIN product p ON p.product_id=d.product_id
            WHERE d.count_id=%s
            ORDER BY p.category, p.name
            """,
            (count["count_id"],),
        ).fetchall()
        result = dict(count)
        result["items"] = [dict(i) for i in items]
        return result


def save_daily_count_items(count_date, product_quantities):
    with get_connection() as conn:
        count = _get_or_create_daily_count(conn, count_date)
        if count["status"] == "completed":
            raise ValueError("Today's stock count is already completed.")
        for product_id, quantity in product_quantities.items():
            if quantity is None:
                continue
            quantity = float(quantity)
            if quantity < 0:
                raise ValueError("Stock count cannot be negative.")
            conn.execute(
                """
                INSERT INTO daily_stock_count_item(count_id, product_id, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (count_id, product_id)
                DO UPDATE SET quantity=EXCLUDED.quantity
                """,
                (count["count_id"], int(product_id), quantity),
            )


def get_daily_count_status(count_date=None):
    count_date = count_date or date.today()
    with get_connection() as conn:
        count = _get_or_create_daily_count(conn, count_date)
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM product WHERE active=TRUE"
        ).fetchone()["cnt"]
        counted = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM daily_stock_count_item d
            JOIN product p ON p.product_id=d.product_id
            WHERE d.count_id=%s AND p.active=TRUE
            """,
            (count["count_id"],),
        ).fetchone()["cnt"]
        return {
            "count_id": count["count_id"],
            "count_date": count["count_date"],
            "status": count["status"],
            "started_at": count["started_at"],
            "completed_at": count["completed_at"],
            "total_products": int(total),
            "counted_products": int(counted),
            "complete_ready": int(total) > 0 and int(total) == int(counted),
        }


def complete_daily_count(count_date=None):
    """
    Make the nightly physical count the source of truth.

    If counted stock is lower than the recorded stock, the difference is saved
    as a sale/usage movement. If it is higher, the difference is recorded as an
    adjustment. This lets future forecasts learn from nightly counts.
    """
    count_date = count_date or date.today()
    with get_connection() as conn:
        count = _get_or_create_daily_count(conn, count_date)
        if count["status"] == "completed":
            return False

        products = conn.execute(
            "SELECT product_id, current_stock FROM product WHERE active=TRUE ORDER BY product_id FOR UPDATE"
        ).fetchall()
        items = conn.execute(
            "SELECT product_id, quantity FROM daily_stock_count_item WHERE count_id=%s",
            (count["count_id"],),
        ).fetchall()
        item_map = {int(i["product_id"]): float(i["quantity"]) for i in items}

        missing = [p["product_id"] for p in products if int(p["product_id"]) not in item_map]
        if missing:
            raise ValueError("Count every active product before completing the nightly inventory.")

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        for product in products:
            product_id = int(product["product_id"])
            old = float(product["current_stock"])
            new = item_map[product_id]
            delta = new - old
            if delta != 0:
                movement_type = "sale" if delta < 0 else "adjustment"
                conn.execute(
                    """
                    INSERT INTO stock_movement(product_id, movement_type, quantity, timestamp)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (product_id, movement_type, delta, ts),
                )
                conn.execute(
                    "UPDATE product SET current_stock=%s WHERE product_id=%s",
                    (new, product_id),
                )

        conn.execute(
            """
            UPDATE daily_stock_count
            SET status='completed', completed_at=%s
            WHERE count_id=%s
            """,
            (ts, count["count_id"]),
        )
        return True


def get_last_completed_count():
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT count_date, completed_at
            FROM daily_stock_count
            WHERE status='completed'
            ORDER BY count_date DESC, completed_at DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def get_orders_grouped(include_received=False):
    where = "WHERE po.status IN ('draft','sent','received')" if include_received else "WHERE po.status IN ('draft','sent')"
    with get_connection() as conn:
        orders = conn.execute(
            f"""
            SELECT po.order_id, po.supplier_id, po.status, po.created_at,
                   po.sent_at, po.received_at, s.name AS supplier_name
            FROM purchase_order po
            JOIN supplier s ON s.supplier_id=po.supplier_id
            {where}
            ORDER BY
                CASE po.status WHEN 'draft' THEN 1 WHEN 'sent' THEN 2 ELSE 3 END,
                s.name, po.created_at DESC
            """
        ).fetchall()
        result = []
        for order in orders:
            items = conn.execute(
                """
                SELECT poi.product_id, poi.quantity, p.name AS product_name,
                       p.unit, p.category
                FROM purchase_order_item poi
                JOIN product p ON p.product_id=poi.product_id
                WHERE poi.order_id=%s
                ORDER BY p.category, p.name
                """,
                (order["order_id"],),
            ).fetchall()
            d = dict(order)
            d["items"] = [dict(i) for i in items]
            result.append(d)
        return result


def get_open_order_item_statuses():
    """Return the current draft/sent order state for each product.

    Sent orders take priority because they are already placed and should not be
    added to another draft order while waiting for delivery.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                poi.product_id,
                poi.quantity,
                po.order_id,
                po.status,
                s.name AS supplier_name
            FROM purchase_order_item poi
            JOIN purchase_order po ON po.order_id=poi.order_id
            JOIN supplier s ON s.supplier_id=po.supplier_id
            WHERE po.status IN ('draft', 'sent')
            ORDER BY
                poi.product_id,
                CASE po.status WHEN 'sent' THEN 1 ELSE 2 END,
                po.created_at DESC
            """
        ).fetchall()

    result = {}
    for row in rows:
        product_id = int(row["product_id"])
        # First row wins; query ordering makes sent orders win over drafts.
        if product_id not in result:
            result[product_id] = dict(row)
    return result


def get_on_order_quantities():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT poi.product_id, SUM(poi.quantity) AS quantity
            FROM purchase_order_item poi
            JOIN purchase_order po ON po.order_id=poi.order_id
            WHERE po.status IN ('draft','sent')
            GROUP BY poi.product_id
            """
        ).fetchall()
        return {int(r["product_id"]): int(r["quantity"] or 0) for r in rows}


def upsert_draft_order_item(product_id, quantity):
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("Order quantity must be greater than zero.")

    with get_connection() as conn:
        sent = conn.execute(
            """
            SELECT 1 FROM purchase_order_item poi
            JOIN purchase_order po ON po.order_id=poi.order_id
            WHERE poi.product_id=%s AND po.status='sent'
            LIMIT 1
            """,
            (int(product_id),),
        ).fetchone()
        if sent:
            raise ValueError("This product is already in an order that has been sent.")

        supplier = conn.execute(
            """
            SELECT ps.supplier_id
            FROM product_supplier ps
            JOIN supplier s ON s.supplier_id=ps.supplier_id
            WHERE ps.product_id=%s AND ps.is_primary=1 AND s.active=TRUE
            """,
            (int(product_id),),
        ).fetchone()
        if not supplier:
            raise ValueError("This product does not have an active primary supplier.")

        supplier_id = int(supplier["supplier_id"])
        draft = conn.execute(
            """
            SELECT order_id FROM purchase_order
            WHERE supplier_id=%s AND status='draft'
            ORDER BY created_at DESC LIMIT 1
            """,
            (supplier_id,),
        ).fetchone()
        if draft:
            order_id = int(draft["order_id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO purchase_order(supplier_id, status, created_at)
                VALUES (%s, 'draft', %s)
                RETURNING order_id
                """,
                (supplier_id, datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            order_id = int(cur.fetchone()["order_id"])

        conn.execute(
            """
            INSERT INTO purchase_order_item(order_id, product_id, quantity)
            VALUES (%s, %s, %s)
            ON CONFLICT (order_id, product_id)
            DO UPDATE SET quantity=EXCLUDED.quantity
            """,
            (order_id, int(product_id), quantity),
        )
        return order_id


def update_order_item_quantity(order_id, product_id, quantity):
    quantity = int(quantity)
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE purchase_order_item SET quantity=%s
            WHERE order_id=%s AND product_id=%s
              AND EXISTS (
                  SELECT 1 FROM purchase_order
                  WHERE order_id=%s AND status='draft'
              )
            """,
            (quantity, int(order_id), int(product_id), int(order_id)),
        )


def remove_order_item(order_id, product_id):
    with get_connection() as conn:
        status = conn.execute(
            "SELECT status FROM purchase_order WHERE order_id=%s",
            (int(order_id),),
        ).fetchone()
        if not status or status["status"] != "draft":
            return False
        conn.execute(
            "DELETE FROM purchase_order_item WHERE order_id=%s AND product_id=%s",
            (int(order_id), int(product_id)),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS cnt FROM purchase_order_item WHERE order_id=%s",
            (int(order_id),),
        ).fetchone()["cnt"]
        if int(remaining) == 0:
            conn.execute("DELETE FROM purchase_order WHERE order_id=%s", (int(order_id),))
        return True


def mark_order_sent(order_id):
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE purchase_order SET status='sent', sent_at=%s
            WHERE order_id=%s AND status='draft'
            """,
            (datetime.now().strftime("%Y-%m-%d %H:%M"), int(order_id)),
        )


def mark_order_received(order_id):
    with get_connection() as conn:
        order = conn.execute(
            "SELECT status FROM purchase_order WHERE order_id=%s FOR UPDATE",
            (int(order_id),),
        ).fetchone()
        if not order or order["status"] != "sent":
            return False

        items = conn.execute(
            "SELECT product_id, quantity FROM purchase_order_item WHERE order_id=%s",
            (int(order_id),),
        ).fetchall()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        for item in items:
            conn.execute(
                """
                INSERT INTO stock_movement(product_id, movement_type, quantity, timestamp)
                VALUES (%s, 'purchase', %s, %s)
                """,
                (item["product_id"], item["quantity"], ts),
            )
            conn.execute(
                """
                UPDATE product SET current_stock=current_stock + %s
                WHERE product_id=%s
                """,
                (item["quantity"], item["product_id"]),
            )

        conn.execute(
            """
            UPDATE purchase_order SET status='received', received_at=%s
            WHERE order_id=%s
            """,
            (ts, int(order_id)),
        )
        return True


if __name__ == "__main__":
    init_db()
    print("Database schema ready. No sample data was inserted.")
