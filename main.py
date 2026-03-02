# main.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()  # Load env variables from .env

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")

# PostgreSQL connection string
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

app.config['SQLALCHEMY_DATABASE_URI'] = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ---------------------------
# Database Models
# ---------------------------
class MenuItem(db.Model):
    __tablename__ = 'menu_items'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)
    image_url = db.Column(db.String(300))  # path to static image

class Payment(db.Model):
    __tablename__ = 'payments'
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20))
    amount = db.Column(db.Float)
    method = db.Column(db.String(20))   # Cash / M-Pesa
    status = db.Column(db.String(20))   # Pending / Confirmed

# ---------------------------
# Routes
# ---------------------------
@app.route('/')
def home():
    return render_template("index.html")

@app.route('/dashboard')
def dashboard():
    payments = Payment.query.order_by(Payment.id.desc()).all()
    return render_template("dashboard.html", payments=payments)

@app.route('/menu')
def menu():
    menu_items = MenuItem.query.all()
    return render_template("menu.html", menu_items=menu_items)

@app.route('/checkout', methods=['POST'])
def checkout():
    selected_item_ids = request.form.getlist('items')  # list of ids as strings
    method = request.form.get("method")
    phone = request.form.get("phone") if method == "M-Pesa" else None

    if not selected_item_ids:
        flash("No items selected!", "warning")
        return redirect(url_for('menu'))

    if method == "M-Pesa" and not phone:
        flash("Phone number is required for M-Pesa payments.", "warning")
        return redirect(url_for('menu'))

    total = 0
    for item_id in selected_item_ids:
        item = MenuItem.query.get(int(item_id))
        qty = int(request.form.get(f'qty_{item_id}', 0))  # get quantity for this item
        if qty > 0:
            total += item.price * qty

    if total == 0:
        flash("You must select at least one item quantity greater than 0!", "warning")
        return redirect(url_for('menu'))

    # Save payment to DB
    payment = Payment(
        phone=phone,
        amount=total,
        method=method,
        status="Pending"
    )
    db.session.add(payment)
    db.session.commit()

    flash(f"Order placed! Total: KES {total}", "success")
    return redirect(url_for('dashboard'))

# ---------------------------
# Seed Initial Menu Items (run once)
# ---------------------------
def seed_menu():
    if MenuItem.query.count() == 0:
        items = [
            MenuItem(name="Smokies", price=40, image_url="/static/images/smokies.jpg"),
            MenuItem(name="Chapati", price=25, image_url="/static/images/chapati.jpg"),
            MenuItem(name="Kachumbari", price=5, image_url="/static/images/kachumbari.jpg"),
            MenuItem(name="Smocha (smokies)", price=70, image_url="/static/images/smocha.jpg"),
            MenuItem(name="Smocha (sausage)", price=80, image_url="/static/images/smocha.jpg"),
            MenuItem(name="Sausage", price=50, image_url="/static/images/sausage.png"),
            MenuItem(name="Hotdog (sausage)", price=100, image_url="/static/images/hotdog.jpg"),
            MenuItem(name="Hotdog (smokies)", price=80, image_url="/static/images/hotdog.jpg"),
            MenuItem(name="Buns", price=25, image_url="/static/images/buns.jpg"),
        ]
        db.session.bulk_save_objects(items)
        db.session.commit()

@app.template_filter('format_currency')
def format_currency(value):
    try:
        if value is None:
            return "0"
        num = round(float(value), 2)
        if num.is_integer():
            return "{:,.0f}".format(num)
        return "{:,.2f}".format(num)
    except:
        return value
# ---------------------------
# Run App
# ---------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Ensure tables exist
        seed_menu()      # Seed menu if empty
    app.run(host='0.0.0.0', port=4000, debug=True)