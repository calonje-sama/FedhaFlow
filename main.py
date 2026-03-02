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
    if not selected_item_ids:
        flash("No items selected!", "warning")
        return redirect(url_for('menu'))

    # Calculate total
    menu_items = MenuItem.query.filter(MenuItem.id.in_(selected_item_ids)).all()
    total = sum(item.price for item in menu_items)

    # Save payment to DB
    payment = Payment(
        phone=request.form.get("phone", "Unknown"),
        amount=total,
        method=request.form.get("method", "Cash"),
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
            MenuItem(name="Tea", price=20),
            MenuItem(name="Chapati", price=15),
            MenuItem(name="Rice", price=50),
            MenuItem(name="Beef", price=120),
        ]
        db.session.bulk_save_objects(items)
        db.session.commit()
        print("Seeded initial menu items.")

# ---------------------------
# Run App
# ---------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Ensure tables exist
        seed_menu()      # Seed menu if empty
    app.run(host='0.0.0.0', port=4000, debug=True)