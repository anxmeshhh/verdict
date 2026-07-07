import re
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-in-production"

# In-memory user store: email -> {"password_hash": str, "name": str}
users = {}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email):
    return bool(email) and bool(EMAIL_RE.match(email))


def is_valid_password(password):
    return bool(password) and len(password) >= 8


@app.route("/")
def home():
    if session.get("user"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/signup", methods=["GET"])
def signup_page():
    return render_template("signup.html")


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if not session.get("user"):
        return redirect(url_for("login_page"))
    return render_template("dashboard.html", name=users[session["user"]]["name"])


@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name:
        return jsonify({"error": "Name is required."}), 400
    if not is_valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if not is_valid_password(password):
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if email in users:
        return jsonify({"error": "An account with this email already exists."}), 409

    users[email] = {"password_hash": generate_password_hash(password), "name": name}
    session["user"] = email
    return jsonify({"message": "Account created.", "email": email}), 201


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = users.get(email)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid email or password."}), 401

    session["user"] = email
    return jsonify({"message": "Logged in.", "email": email}), 200


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user", None)
    return jsonify({"message": "Logged out."}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
