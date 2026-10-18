import sqlite3

def create_demo():
    conn = sqlite3.connect("demo.db")
    cur = conn.cursor()
    cur.executescript("""
    DROP TABLE IF EXISTS orders;
    DROP TABLE IF EXISTS products;
    DROP TABLE IF EXISTS customers;
    DROP VIEW IF EXISTS v_recent_orders;

    CREATE TABLE customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        country TEXT DEFAULT 'US',
        balance REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        price REAL NOT NULL,
        stock INTEGER NOT NULL
    );

    CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        product_id INTEGER,
        quantity INTEGER DEFAULT 1,
        status TEXT CHECK(status IN ('pending', 'completed', 'cancelled')) DEFAULT 'pending',
        order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(customer_id) REFERENCES customers(id),
        FOREIGN KEY(product_id) REFERENCES products(id)
    );

    CREATE INDEX idx_orders_customer ON orders(customer_id);
    CREATE INDEX idx_products_category ON products(category);

    CREATE VIEW v_recent_orders AS
    SELECT o.id, c.first_name || ' ' || c.last_name AS customer_name, p.name AS product_name, o.quantity, (o.quantity * p.price) AS total_amount, o.status
    FROM orders o
    JOIN customers c ON o.customer_id = c.id
    JOIN products p ON o.product_id = p.id;
    """)

    customers_data = [
        ('Alice', 'Smith', 'alice@example.com', 'US', 145.50),
        ('Bob', 'Jones', 'bob@example.com', 'UK', 20.00),
        ('Charlie', 'Brown', 'charlie@example.com', 'CA', 312.80),
        ('Diana', 'Prince', 'diana@example.com', 'FR', 950.00),
        ('Evan', 'Wright', 'evan@example.com', 'DE', 0.00),
        ('Fiona', 'Gallagher', 'fiona@example.com', 'US', 54.20),
        ('George', 'Martin', 'george@example.com', 'UK', 1200.00),
        ('Hannah', 'Abbott', 'hannah@example.com', 'CA', 85.00),
        ('Ian', 'Malcolm', 'ian@example.com', 'US', 420.00),
        ('Julia', 'Roberts', 'julia@example.com', 'FR', 65.50)
    ]
    cur.executemany("INSERT INTO customers (first_name, last_name, email, country, balance) VALUES (?, ?, ?, ?, ?)", customers_data)

    products_data = [
        ('Mechanical Keyboard', 'Electronics', 129.99, 45),
        ('Wireless Mouse', 'Electronics', 59.99, 120),
        ('UltraWide Monitor 34"', 'Displays', 499.00, 18),
        ('Ergonomic Chair', 'Furniture', 349.50, 12),
        ('USB-C Docking Station', 'Electronics', 89.90, 60),
        ('Desk Mat XL', 'Accessories', 25.00, 200),
        ('Noise-Cancelling Headphones', 'Audio', 199.99, 35),
        ('Webcam 4K', 'Electronics', 119.00, 50)
    ]
    cur.executemany("INSERT INTO products (name, category, price, stock) VALUES (?, ?, ?, ?)", products_data)

    orders_data = [
        (1, 1, 1, 'completed'),
        (1, 2, 2, 'completed'),
        (2, 4, 1, 'pending'),
        (3, 3, 1, 'completed'),
        (4, 7, 2, 'completed'),
        (5, 6, 3, 'cancelled'),
        (6, 5, 1, 'completed'),
        (7, 3, 2, 'completed'),
        (8, 2, 1, 'pending'),
        (9, 1, 1, 'completed')
    ]
    cur.executemany("INSERT INTO orders (customer_id, product_id, quantity, status) VALUES (?, ?, ?, ?)", orders_data)

    conn.commit()
    conn.close()
    print("Created demo.db successfully!")

if __name__ == "__main__":
    create_demo()
