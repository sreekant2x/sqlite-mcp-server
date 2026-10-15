"""Build a small, deterministic bookstore database to try the server against.

    python -m sqlite_mcp.sample_data bookstore.db
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE authors (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT NOT NULL
);
CREATE TABLE books (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    author_id   INTEGER NOT NULL REFERENCES authors(id),
    genre       TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    published   INTEGER NOT NULL
);
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    city        TEXT NOT NULL,
    joined_on   TEXT NOT NULL
);
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ordered_on  TEXT NOT NULL
);
CREATE TABLE order_items (
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    book_id     INTEGER NOT NULL REFERENCES books(id),
    quantity    INTEGER NOT NULL,
    PRIMARY KEY (order_id, book_id)
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE VIEW book_sales AS
    SELECT b.id AS book_id, b.title, SUM(oi.quantity) AS copies_sold,
           SUM(oi.quantity * b.price_cents) / 100.0 AS revenue
    FROM books b JOIN order_items oi ON oi.book_id = b.id
    GROUP BY b.id;
"""

AUTHORS = [
    ("Chinua Achebe", "Nigeria"),
    ("Ursula K. Le Guin", "USA"),
    ("Haruki Murakami", "Japan"),
    ("R. K. Narayan", "India"),
    ("Gabriel Garcia Marquez", "Colombia"),
    ("Octavia E. Butler", "USA"),
]

BOOKS = [
    ("Things Fall Apart", 1, "Literary fiction", 1299, 1958),
    ("Arrow of God", 1, "Literary fiction", 1199, 1964),
    ("A Wizard of Earthsea", 2, "Fantasy", 999, 1968),
    ("The Left Hand of Darkness", 2, "Science fiction", 1499, 1969),
    ("The Dispossessed", 2, "Science fiction", 1599, 1974),
    ("Norwegian Wood", 3, "Literary fiction", 1399, 1987),
    ("Kafka on the Shore", 3, "Magical realism", 1699, 2002),
    ("Malgudi Days", 4, "Short stories", 899, 1943),
    ("The Guide", 4, "Literary fiction", 1099, 1958),
    ("One Hundred Years of Solitude", 5, "Magical realism", 1799, 1967),
    ("Love in the Time of Cholera", 5, "Literary fiction", 1599, 1985),
    ("Kindred", 6, "Science fiction", 1399, 1979),
    ("Parable of the Sower", 6, "Science fiction", 1499, 1993),
]

CITIES = ["Bengaluru", "Kolkata", "Mumbai", "Pune", "Delhi", "Hyderabad", "Chennai"]
FIRST = ["Aarav", "Diya", "Ishaan", "Meera", "Rohan", "Sana", "Kabir", "Anaya", "Vivaan", "Tara"]
LAST = ["Sharma", "Iyer", "Das", "Khan", "Patel", "Reddy", "Bose", "Nair"]


def build(path: str | Path, seed: int = 7) -> Path:
    path = Path(path)
    if path.exists():
        path.unlink()
    rng = random.Random(seed)
    start = date(2025, 1, 1)

    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO authors (name, country) VALUES (?, ?)", AUTHORS)
        conn.executemany(
            "INSERT INTO books (title, author_id, genre, price_cents, published) VALUES (?, ?, ?, ?, ?)", BOOKS
        )
        customers = [
            (f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.choice(CITIES), str(start + timedelta(rng.randrange(300))))
            for _ in range(40)
        ]
        conn.executemany("INSERT INTO customers (name, city, joined_on) VALUES (?, ?, ?)", customers)

        for order_id in range(1, 151):
            customer_id = rng.randint(1, len(customers))
            joined = date.fromisoformat(customers[customer_id - 1][2])
            ordered_on = joined + timedelta(rng.randrange(1, 120))
            conn.execute(
                "INSERT INTO orders (id, customer_id, ordered_on) VALUES (?, ?, ?)",
                (order_id, customer_id, str(ordered_on)),
            )
            for book_id in rng.sample(range(1, len(BOOKS) + 1), rng.randint(1, 3)):
                conn.execute(
                    "INSERT INTO order_items (order_id, book_id, quantity) VALUES (?, ?, ?)",
                    (order_id, book_id, rng.choice([1, 1, 1, 2, 3])),
                )
    conn.close()
    return path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "bookstore.db"
    print(f"Wrote {build(target)}")
