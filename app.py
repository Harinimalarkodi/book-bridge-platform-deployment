"""
Book Bridge Platform
---------------------
A simple Flask web app where users can list books they no longer need,
and other users can search for books and view the owner's contact info
to arrange borrowing/collection.

No login, payment, chat, or admin panel — kept intentionally simple
for a college mini project.
"""

from flask import Flask, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3
import os

# NEW: Groq-powered Library Assistant Agent (separate ai/ module)
from ai.groq_assistant import ask_library_assistant

# NEW: ChromaDB vector store, kept in sync with the books table (RAG)
from ai import vector_store

app = Flask(__name__)
app.secret_key = "book_bridge_secret_key"  # needed for flash messages + sessions

DB_NAME = "database.db"


# ---------------------------------------------------------------------------
# DATABASE HELPERS
# ---------------------------------------------------------------------------

def get_db_connection():
    """Create and return a connection to the SQLite database."""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row  # lets us access columns by name
    return conn


def init_db():
    """Create the books table automatically if it doesn't already exist."""
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            author TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT,
            owner_name TEXT NOT NULL,
            owner_contact TEXT NOT NULL,
            date_added TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --- NEW: users table for registration/login ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            phone TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            date_joined TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # --- NEW: link books to the registered user who added them.
    # Added via ALTER TABLE so existing books/rows are never touched.
    existing_columns = [row["name"] for row in conn.execute("PRAGMA table_info(books)")]
    if "user_id" not in existing_columns:
        conn.execute("ALTER TABLE books ADD COLUMN user_id INTEGER")

    conn.commit()
    conn.close()


def login_required(view_func):
    """Simple decorator to require a logged-in user for a route."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login to continue.", "warning")
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# AI BOOK ASSISTANT (NEW): simple rule-based Q&A, no external AI service.
# Answers are 100% predefined below and matched using basic keyword checks.
# ---------------------------------------------------------------------------

AI_FALLBACK_ANSWER = "Sorry, I can only answer questions related to the Book Bridge platform."

# Each entry: (list of keywords/phrases to look for in the question, answer text)
AI_KNOWLEDGE_BASE = [
    (
        ["what is book bridge", "about book bridge", "what is this"],
        "Book Bridge is a simple platform where people who have books they no longer "
        "need can list them, and others can search for books and contact the owner "
        "to borrow or collect them.",
    ),
    (
        ["register", "sign up", "signup", "create account", "create an account"],
        "To register, click 'Register' in the navigation bar, then fill in your "
        "Name, Email, Phone Number, and Password. Once registered, you can login "
        "and start adding books.",
    ),
    (
        ["login", "log in", "sign in"],
        "To login, click 'Login' in the navigation bar and enter the email and "
        "password you used when registering.",
    ),
    (
        ["add a book", "add book", "list a book", "how do i add"],
        "To add a book, first login to your account, then click 'Add Book' in the "
        "navigation bar and fill in the book's title, author, category, description, "
        "and your contact details.",
    ),
    (
        ["search", "find a book", "find book", "browse"],
        "To search for books, go to the 'View Books' page and use the search box "
        "to look up books by title, author, or category.",
    ),
    (
        ["contact the owner", "contact owner", "reach the owner", "contact a book owner"],
        "On the 'View Books' page, each book listing shows the owner's Name, Email, "
        "and Phone Number. You can click the 'Contact Owner' button to open a "
        "pre-filled Gmail compose window and email them directly.",
    ),
    (
        ["categories", "category", "what kind of books", "types of books"],
        "Book Bridge supports these categories: Fiction, Non-Fiction, Academic, "
        "Children, Biography, Science, and Other.",
    ),
]


def get_ai_response(question):
    """Match the user's question against the predefined knowledge base
    using simple keyword matching. Returns the fallback message if no
    predefined topic matches. This is intentionally simple/rule-based —
    no external AI service (OpenAI, Groq, etc.) is used.
    """
    if not question:
        return AI_FALLBACK_ANSWER

    normalized = question.lower().strip()

    for keywords, answer in AI_KNOWLEDGE_BASE:
        for keyword in keywords:
            if keyword in normalized:
                return answer

    return AI_FALLBACK_ANSWER


# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    """Home page with a short intro and quick stats."""
    conn = get_db_connection()
    total_books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    recent_books = conn.execute(
        "SELECT * FROM books ORDER BY id DESC LIMIT 3"
    ).fetchall()
    conn.close()
    return render_template("index.html", total_books=total_books, recent_books=recent_books)


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_book():
    """Show the Add Book form (GET) and save a new book (POST).
    NEW: requires login, and the new listing is linked to the logged-in
    user's account (session['user_id']) so their registered Name/Email/
    Phone can be shown as complete owner contact details, and so only
    they can delete it later.
    """
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        owner_contact = request.form.get("owner_contact", "").strip()

        # Basic server-side validation
        if not (title and author and category and owner_name and owner_contact):
            flash("Please fill in all required fields.", "danger")
            return redirect(url_for("add_book"))

        conn = get_db_connection()
        cursor = conn.execute(
            """
            INSERT INTO books (title, author, category, description, owner_name, owner_contact, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, author, category, description, owner_name, owner_contact, session["user_id"]),
        )
        conn.commit()
        new_book_id = cursor.lastrowid
        conn.close()

        # NEW: keep ChromaDB in sync — embed the newly added book so the
        # AI Assistant can retrieve it for future questions/recommendations.
        try:
            vector_store.upsert_book(
                {
                    "id": new_book_id,
                    "title": title,
                    "author": author,
                    "category": category,
                    "description": description,
                    "owner_name": owner_name,
                    "owner_contact": owner_contact,
                }
            )
        except Exception:
            pass  # ChromaDB sync issues should never block adding a book

        flash("Book added successfully!", "success")
        return redirect(url_for("view_books"))

    return render_template("add_book.html")


@app.route("/books")
def view_books():
    """Display all books, optionally filtered by a search query."""
    query = request.args.get("q", "").strip()

    # NEW: LEFT JOIN users so we can display the registered owner's
    # complete contact details (Name, Email, Phone) when available.
    # Legacy books (added before login existed) simply have no match
    # here and fall back to their original owner_name/owner_contact.
    base_select = """
        SELECT books.*, users.name AS reg_name, users.email AS reg_email, users.phone AS reg_phone
        FROM books
        LEFT JOIN users ON books.user_id = users.id
    """

    conn = get_db_connection()
    if query:
        like_query = f"%{query}%"
        books = conn.execute(
            base_select + " WHERE title LIKE ? OR author LIKE ? OR category LIKE ? ORDER BY books.id DESC",
            (like_query, like_query, like_query),
        ).fetchall()
    else:
        books = conn.execute(base_select + " ORDER BY books.id DESC").fetchall()
    conn.close()

    return render_template("books.html", books=books, query=query)


@app.route("/delete/<int:book_id>")
@login_required
def delete_book(book_id):
    """Remove a book listing (e.g. once it has been given away).
    NEW: only the logged-in user who owns this listing may delete it.
    """
    conn = get_db_connection()
    book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()

    if book is None:
        conn.close()
        flash("Book listing not found.", "danger")
        return redirect(url_for("view_books"))

    if book["user_id"] != session.get("user_id"):
        conn.close()
        flash("You can only delete your own book listings.", "danger")
        return redirect(url_for("view_books"))

    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
    conn.close()

    # NEW: keep ChromaDB in sync — remove the deleted book's embedding.
    try:
        vector_store.delete_book(book_id)
    except Exception:
        pass  # ChromaDB sync issues should never block deleting a book

    flash("Book listing removed.", "info")
    return redirect(url_for("view_books"))


# ---------------------------------------------------------------------------
# AUTH ROUTES (NEW): Registration / Login / Logout
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    """Register a new user account. Password is hashed before storage."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not (name and email and phone and password and confirm_password):
            flash("Please fill in all fields.", "danger")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("register"))

        conn = get_db_connection()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            conn.close()
            flash("An account with that email already exists. Please login.", "warning")
            return redirect(url_for("login"))

        password_hash = generate_password_hash(password)
        conn.execute(
            "INSERT INTO users (name, email, phone, password_hash) VALUES (?, ?, ?, ?)",
            (name, email, phone, password_hash),
        )
        conn.commit()
        conn.close()

        flash("Registration successful! Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Log a user in by verifying their hashed password."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("home"))

        flash("Invalid email or password.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    """Log the current user out by clearing their session."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


# ---------------------------------------------------------------------------
# AI ASSISTANT ROUTES (NEW)
# ---------------------------------------------------------------------------

@app.route("/ask-ai")
def ask_ai():
    """Render the AI Book Assistant page."""
    return render_template("ai_assistant.html")


@app.route("/ai-response", methods=["POST"])
def ai_response():
    """Return a predefined answer for the user's question as JSON.
    Rule-based only — no external AI service is called.
    """
    question = request.form.get("question", "")
    answer = get_ai_response(question)
    return {"answer": answer}


# ---------------------------------------------------------------------------
# NEW: GROQ-POWERED "AI ASSISTANT" ROUTES (Library Assistant Agent)
# Separate from the rule-based /ask-ai page above, which is unchanged.
# ---------------------------------------------------------------------------

@app.route("/ai-assistant")
def ai_assistant_page():
    """Render the Groq-powered AI Assistant (Library Assistant Agent) page."""
    return render_template("ai_chat.html")


@app.route("/ai-assistant/chat", methods=["POST"])
def ai_assistant_chat():
    """Send the user's question to the Groq Library Assistant Agent
    and return its response as JSON.
    """
    question = request.form.get("question", "")
    answer = ask_library_assistant(question)
    return {"answer": answer}


# ---------------------------------------------------------------------------
# APP ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Create the database + table automatically on first run
    if not os.path.exists(DB_NAME):
        init_db()
    else:
        init_db()  # safe to call again; CREATE TABLE IF NOT EXISTS

    # NEW: keep ChromaDB in sync with SQLite on startup (idempotent upsert),
    # so every existing book is embedded and searchable by the AI Assistant.
    try:
        vector_store.sync_all_books_from_sqlite()
    except Exception:
        pass  # ChromaDB sync issues should never prevent the app from starting

    app.run(debug=True)
