"""
routes/main.py — HTML page routes
"""
from flask import Blueprint, render_template, redirect, url_for, session, request
from flask_login import current_user, login_required

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("landing.html")


@main_bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("index.html", user=current_user)


@main_bp.route("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    # Save intended destination
    next_url = request.args.get("next")
    if next_url:
        session["next_url"] = next_url
    return render_template("login.html")
