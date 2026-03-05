# main.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import joinedload
from dotenv import load_dotenv, dotenv_values
from flask_socketio import SocketIO, emit
import requests
from requests.auth import HTTPBasicAuth
import base64
from datetime import datetime

load_dotenv(override=True)  # Load env variables from .env

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")  # replace app.run() with socketio.run()
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")

# PostgreSQL connection string
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
# MPESA DARAJA API CONFIG DETAILS
MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE")
MPESA_PASSKEY = os.getenv("MPESA_PASSKEY")
MPESA_CONSUMER_KEY = os.getenv("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
MPESA_ENV = os.getenv("MPESA_ENV", "sandbox")  # sandbox or production
MPESA_CALLBACK_URL = os.getenv("MPESA_CALLBACK_URL")  # your webhook

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
    created_at = db.Column(db.DateTime, default=db.func.now())
class OrderItem(db.Model):
    __tablename__ = "order_items"

    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer, db.ForeignKey('payments.id'), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)

    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)

    payment = db.relationship("Payment", backref="order_items")
    menu_item = db.relationship("MenuItem")

# ---------------------------
# Routes
# ---------------------------
@app.route('/')
def home():
    return render_template("index.html")

@app.route('/dashboard')
def dashboard():
    payments = Payment.query.options(
        joinedload(Payment.order_items).joinedload(OrderItem.menu_item)
    ).order_by(Payment.id.desc()).all()
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
    status = "Pending" if method == "M-Pesa" else "Confirmed"

    if not selected_item_ids:
        flash("No items selected!", "warning")
        return redirect(url_for('menu'))

    if method == "M-Pesa" and not phone:
        flash("Phone number is required for M-Pesa payments.", "warning")
        return redirect(url_for('menu'))

    total = 0
    for item_id in selected_item_ids:
        item = db.session.get(MenuItem, int(item_id))
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
        status=status
    )
    db.session.add(payment)
    db.session.flush()  # get payment.id
    for item_id in selected_item_ids:
        item = db.session.get(MenuItem, int(item_id))
        qty = int(request.form.get(f'qty_{item_id}', 0))

        if qty > 0:
            order_item = OrderItem(
                payment_id=payment.id,
                menu_item_id=item.id,
                quantity=qty,
                price=item.price
            )
            db.session.add(order_item)
    db.session.commit()
    # Push the new payment to all dashboard clients
    notify_payment_update(payment)
    # --- Trigger STK Push if M-Pesa ---
    if method == "M-Pesa":
        stk_response = initiate_stk_push(phone, total)
        if stk_response.get("success"):
            flash(f"STK Push sent to {phone}. Complete payment on your phone.", "info")
        else:
            flash(f"Failed to send STK Push: {stk_response.get('error')}", "danger")

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

@app.route('/api/payments')
def api_payments():
    payments = Payment.query.options(
        joinedload(Payment.order_items).joinedload(OrderItem.menu_item)
    ).order_by(Payment.id.desc()).all()

    data = []
    total_sales = 0

    for p in payments:
        if p.status == "Confirmed":
            total_sales += p.amount
        items = []
        for item in p.order_items:
            items.append({
                "name": item.menu_item.name,
                "qty": format_currency(item.quantity),
                "price": format_currency(item.price)
            })

        data.append({
            "id": p.id,
            "phone": p.phone if p.phone else "—",
            "amount": format_currency(p.amount),
            "method": p.method,
            "status": p.status,
            "items": items
        })

    return jsonify({
        "payments": data,
        "total_sales": format_currency(total_sales)
    })

def notify_payment_update(payment):
    items = [{"name": i.menu_item.name, "qty": i.quantity, "price": i.price} for i in payment.order_items]
    socketio.emit('payment_update', {
        "id": payment.id,
        "phone": payment.phone or "—",
        "amount": payment.amount,
        "method": payment.method,
        "status": payment.status,
        "items": items
    }, to=None)

# Receive payment confirmations from Safaricom
@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = request.get_json()
    print("STK Callback Received:", data)  # debug log
    try:
        body = data['Body']['stkCallback']
        checkout_id = body.get('CheckoutRequestID')
        result_code = body.get('ResultCode')
        callback_items = body.get('CallbackMetadata', {}).get('Item', [])

        # Extract amount and phone safely
        amount = None
        phone = None
        for item in callback_items:
            if item['Name'] == 'Amount':
                amount = float(item['Value'])
            elif item['Name'] == 'PhoneNumber':
                phone = str(item['Value'])

        # Match payment in DB
        payment = Payment.query.filter_by(phone=phone, status="Pending").first()
        if payment and result_code == 0:
            payment.status = "Confirmed"
            db.session.commit()
            notify_payment_update(payment)
    except Exception as e:
        print("Error processing STK callback:", e)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

# Generate Access Token
def get_mpesa_access_token():
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    if MPESA_ENV == "production":
        url = "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    response = requests.get(url, auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET))
    response.raise_for_status()  # optional, will raise for 4xx/5xx
    data = response.json()
    return data.get("access_token")

# Initiate STK Push
def initiate_stk_push(phone, amount):
    access_token = get_mpesa_access_token()
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password_str = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(password_str.encode('utf-8')).decode('utf-8')

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": "FedhaFlow",
        "TransactionDesc": "Cafeteria Order Payment"
    }

    url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    if MPESA_ENV == "production":
        url = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.post(url, json=payload, headers=headers)
    print("Status code:", response.status_code)
    print("Response text:", response.text)
    data = response.json()

    if data.get('ResponseCode') == '0':
        return {"success": True, "CheckoutRequestID": data.get('CheckoutRequestID')}
    else:
        return {"success": False, "error": data.get('errorMessage', data)}

@app.route('/resend-stk/<int:payment_id>')
def resend_stk(payment_id):
    payment = db.session.get(Payment, payment_id)
    if payment and payment.status == "Pending" and payment.method == "M-Pesa":
        stk_response = initiate_stk_push(payment.phone, payment.amount)
        if stk_response.get("success"):
            flash("STK Push resent successfully.", "info")
        else:
            flash(f"Failed to resend STK Push: {stk_response.get('error')}", "danger")
    return redirect(url_for('dashboard'))

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
app.jinja_env.filters['format_currency'] = format_currency
# ---------------------------
# Run App
# ---------------------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Ensure tables exist
        seed_menu()      # Seed menu if empty
    # app.run(host='0.0.0.0', port=4000, debug=True)
    socketio.run(app, host='0.0.0.0', port=4000, debug=True)