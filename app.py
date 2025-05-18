import os
import json
import requests
from bs4 import BeautifulSoup
import pandas as pd
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash

# ──────────────────────────────────────────────────────────────────────────────
# SCRAPER / ANALYZER / DATABASE CLASSES
# ──────────────────────────────────────────────────────────────────────────────

# Create data directory if it doesn't exist
DATA_DIR = "scraped_data"
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)


class BookScraper:
    def __init__(self):
        self.base_url = "http://books.toscrape.com/"
        self.books = []

    def fetch_webpage(self, url):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            )
        }
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text
            else:
                print(f"Error: status code {resp.status_code}")
                return None
        except Exception as e:
            print(f"Error fetching page: {e}")
            return None

    def scrape_books_from_page(self, page_num=1):
        url = f"{self.base_url}catalogue/page-{page_num}.html"
        html = self.fetch_webpage(url)
        if not html:
            return False

        soup = BeautifulSoup(html, "html.parser")
        container = soup.select("article.product_pod")
        if not container:
            return False

        for book in container:
            title = book.h3.a["title"]
            price = book.select_one("p.price_color").text
            availability = book.select_one("p.availability").text.strip()
            rating = book.select_one("p.star-rating")["class"][1]
            href = book.h3.a["href"]
            if "catalogue/" not in href:
                href = "catalogue/" + href
            full_url = self.base_url + href

            self.books.append({
                "title": title,
                "price": price,
                "availability": availability,
                "rating": rating,
                "url": full_url
            })

        return True

    def scrape_multiple_pages(self, num_pages=1):
        for i in range(1, num_pages + 1):
            print(f"Scraping page {i}...")
            if not self.scrape_books_from_page(i):
                break
        print(f"Total books scraped: {len(self.books)}")
        return self.books

    def save_to_json(self, filename="books_data.json"):
        path = os.path.join(DATA_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.books, f, indent=2)
        print(f"Saved JSON to {path}")


class BookDataAnalyzer:
    def __init__(self, books_data=None, json_file=None):
        self.df = pd.DataFrame()
        if books_data:
            self.df = pd.DataFrame(books_data)
            self._preprocess()
        elif json_file:
            path = os.path.join(DATA_DIR, json_file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.df = pd.DataFrame(data)
                self._preprocess()
                print(f"Loaded {len(self.df)} records from JSON")
            except Exception as e:
                print(f"Error loading JSON: {e}")

    def _preprocess(self):
        # strip currency symbol and convert
        self.df["price_numeric"] = (
            self.df["price"]
            .str.replace("Â£", "", regex=False)
            .str.replace("£", "", regex=False)
            .astype(float)
        )
        # map word ratings to ints
        rm = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
        self.df["rating_numeric"] = self.df["rating"].map(rm)

    def is_empty(self):
        return self.df.empty

    def get_summary_stats(self):
        if self.df.empty:
            return {"total_books": 0}
        return {
            "total_books": len(self.df),
            "avg_price": self.df["price_numeric"].mean(),
            "min_price": self.df["price_numeric"].min(),
            "max_price": self.df["price_numeric"].max(),
            "avg_rating": self.df["rating_numeric"].mean(),
        }

    def get_best_value_book(self, min_rating=4, n=5):
        if self.df.empty:
            return pd.DataFrame()
        filt = self.df[self.df["rating_numeric"] >= min_rating]
        return filt.sort_values("price_numeric").head(n)


class BookDatabase:
    def __init__(self):
        db_path = os.path.join(DATA_DIR, "books_database.db")
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception as e:
                print(f"Could not remove old DB: {e}")
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self):
        sql = """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            rating INTEGER NOT NULL,
            availability TEXT,
            url TEXT
        )
        """
        self.conn.execute(sql)
        self.conn.commit()

    def insert_books(self, df: pd.DataFrame):
        if df.empty:
            return
        cur = self.conn.cursor()
        cur.execute("SELECT title FROM books")
        existing = {r[0] for r in cur.fetchall()}

        count = 0
        for _, row in df.iterrows():
            if row["title"] in existing:
                continue
            try:
                cur.execute(
                    "INSERT INTO books (title, price, rating, availability, url) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["title"],
                        row["price_numeric"],
                        row["rating_numeric"],
                        row["availability"],
                        row["url"],
                    )
                )
                count += 1
            except Exception as e:
                print(f"Insert error for {row['title']}: {e}")

        self.conn.commit()
        print(f"Inserted {count} new books.")

    def get_book_by_id(self, book_id):
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, title, price, rating, availability, url "
            "FROM books WHERE id = ?", (book_id,)
        )
        return cur.fetchone()


# ──────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ──────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(24)

db = BookDatabase()
analyzer = None


@app.route("/")
@app.route("/books")
def list_books():
    global analyzer
    if analyzer is None or analyzer.is_empty():
        analyzer = BookDataAnalyzer(json_file="books_data.json")
        if analyzer.is_empty():
            flash("No data available. Please scrape first.", "warning")
            return redirect(url_for("scrape_books"))

    cur = db.conn.cursor()
    cur.execute(
        "SELECT id, title, price, rating, availability FROM books ORDER BY id"
    )
    books = cur.fetchall()
    return render_template("books.html", books=books)


@app.route("/books/<int:book_id>")
def book_detail(book_id):
    row = db.get_book_by_id(book_id)
    if not row:
        flash(f"No book found with ID {book_id}", "danger")
        return redirect(url_for("list_books"))
    return render_template("book_detail.html", book=row)


@app.route("/search", methods=["GET", "POST"])
def search():
    results = []
    if request.method == "POST":
        try:
            min_r = int(request.form.get("min_rating", 1))
            max_p = float(request.form.get("max_price", 9999))
        except ValueError:
            flash("Enter valid numbers.", "danger")
            return redirect(url_for("search"))

        cur = db.conn.cursor()
        cur.execute(
            "SELECT id, title, price, rating, availability FROM books "
            "WHERE rating >= ? AND price <= ? "
            "ORDER BY rating DESC, price ASC",
            (min_r, max_p)
        )
        results = cur.fetchall()
        if not results:
            flash("No matches found.", "info")

    return render_template("search.html", results=results)

@app.route("/data")
def view_json():
    """
    Load the scraped JSON file and render it in a <pre> block.
    """
    json_path = os.path.join(DATA_DIR, "books_data.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            books = json.load(f)
    except Exception as e:
        flash(f"Could not load JSON: {e}", "danger")
        return redirect(url_for("list_books"))

    # Pass the raw list of dicts to the template
    return render_template("raw_data.html", books=books)

@app.route("/raw")
def raw_data():
    """
    Load the scraped JSON and display it in a friendly table.
    """
    json_path = os.path.join(DATA_DIR, "books_data.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            books = json.load(f)
    except Exception as e:
        flash(f"Could not load JSON: {e}", "danger")
        return redirect(url_for("list_books"))

    # Clean up the price field (remove stray Â)
    for b in books:
        if b.get("price"):
            b["price_clean"] = b["price"].replace("Â", "")
        else:
            b["price_clean"] = ""

    return render_template("raw_data.html", books=books)




@app.route("/scrape")
def scrape_books():
    global analyzer
    scraper = BookScraper()
    books = scraper.scrape_multiple_pages(num_pages=1)
    scraper.save_to_json()
    analyzer = BookDataAnalyzer(books_data=books)
    db.insert_books(analyzer.df)
    flash(f"Scraped {len(books)} books.", "success")
    return redirect(url_for("list_books"))


if __name__ == "__main__":
    # Ensure templates/ and static/ are next to this file
    app.run(debug=True)
