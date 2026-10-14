# Smart Inventory & Supplier Management System

A Streamlit + PostgreSQL prototype for a small food business such as a pizza shop.

## Main workflow

1. Staff open **Daily Stock Update** before closing.
2. Staff enter only how much of each item was used that day.
3. PostgreSQL updates current inventory and keeps daily usage history.
4. **Home** answers one question: is the normal weekly order safe for next week?
5. **Upcoming Events** contains the detailed forecast reasons.
6. **Weekly Orders** groups recommended quantities by supplier.
7. Draft orders can be placed and later marked as delivered.

## Main pages

- Home
- Suppliers
- Products
- Daily Stock Update
- Upcoming Events
- Weekly Orders

## Database

PostgreSQL is the source of truth. Products, suppliers, supplier-product links,
daily usage, stock movements, purchase orders and settings are stored in the
`pizza_inventory` database.

No demo products or suppliers are inserted automatically.

## Run the project

Copy your existing `.env` file into the project folder, then run:

    python -m pip install -r requirements.txt
    python -m streamlit run app.py

The real `.env` is not included in this package because it can contain your
PostgreSQL password. A safe example is in `project_docs/.env.example`.
