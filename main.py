import os
import io
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import joinedload
from dotenv import load_dotenv
from flask_socketio import SocketIO
import requests
from requests.auth import HTTPBasicAuth
import base64
from datetime import datetime, date, timedelta
import pytz
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, HRFlowable)
from reportlab.lib.enums import TA_RIGHT

load_dotenv(override=True)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
app.secret_key = os.getenv("SECRET_KEY", "super-secret-key")

# ── DB config ──
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST     = os.getenv("DB_HOST")
DB_PORT     = os.getenv("DB_PORT")
DB_NAME     = os.getenv("DB_NAME")

# ── M-Pesa credentials (BOTH sets live in .env, mode selected from DB) ──
MPESA_CREDS = {
    'sandbox': {
        'key':       os.getenv("MPESA_SANDBOX_CONSUMER_KEY"),
        'secret':    os.getenv("MPESA_SANDBOX_CONSUMER_SECRET"),
        'shortcode': os.getenv("MPESA_SANDBOX_SHORTCODE"),
        'passkey':   os.getenv("MPESA_SANDBOX_PASSKEY"),
        'base_url':  'https://sandbox.safaricom.co.ke',
    },
    'live': {
        'key':       os.getenv("MPESA_LIVE_CONSUMER_KEY"),
        'secret':    os.getenv("MPESA_LIVE_CONSUMER_SECRET"),
        'shortcode': os.getenv("MPESA_LIVE_SHORTCODE"),
        'passkey':   os.getenv("MPESA_LIVE_PASSKEY"),
        'base_url':  'https://api.safaricom.co.ke',
    }
}
MPESA_CALLBACK_URL = os.getenv("MPESA_CALLBACK_URL")

# ── Email (stays in .env) ──
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", 587))
SMTP_USER     = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

EAT = pytz.timezone("Africa/Nairobi")

app.config['SQLALCHEMY_DATABASE_URI'] = (
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


# ─────────────────────────────────────────
# Models
# ─────────────────────────────────────────

class MenuItem(db.Model):
    __tablename__ = 'menu_items'
    id        = db.Column(db.Integer, primary_key=True)
    name      = db.Column(db.String(100), nullable=False)
    price     = db.Column(db.Float, nullable=False)
    image_url = db.Column(db.String(300))
    active    = db.Column(db.Boolean, default=True)


class Payment(db.Model):
    __tablename__ = 'payments'
    id                  = db.Column(db.Integer, primary_key=True)
    phone               = db.Column(db.String(20))
    amount              = db.Column(db.Float)
    method              = db.Column(db.String(20))
    pay_channel         = db.Column(db.String(20))
    checkout_request_id = db.Column(db.String(100))
    mpesa_receipt       = db.Column(db.String(50))
    status              = db.Column(db.String(20))
    notes               = db.Column(db.String(300), nullable=True)
    created_at          = db.Column(db.DateTime, default=db.func.now())


class OrderItem(db.Model):
    __tablename__ = "order_items"
    id           = db.Column(db.Integer, primary_key=True)
    payment_id   = db.Column(db.Integer, db.ForeignKey('payments.id'), nullable=False)
    menu_item_id = db.Column(db.Integer, db.ForeignKey('menu_items.id'), nullable=False)
    quantity     = db.Column(db.Integer, nullable=False)
    price        = db.Column(db.Float, nullable=False)
    payment      = db.relationship("Payment", backref="order_items")
    menu_item    = db.relationship("MenuItem")


class Deduction(db.Model):
    __tablename__ = 'deductions'
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(100), nullable=False)
    default_amount = db.Column(db.Float, nullable=False, default=0)


class AppSetting(db.Model):
    """Generic key-value store for all runtime settings."""
    __tablename__ = 'app_settings'
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)


# ─────────────────────────────────────────
# Settings helpers
# ─────────────────────────────────────────

def get_setting(key, default=None):
    row = AppSetting.query.get(key)
    return row.value if row else default


def set_setting(key, value):
    row = AppSetting.query.get(key)
    if row:
        row.value = str(value)
    else:
        db.session.add(AppSetting(key=key, value=str(value)))
    db.session.commit()


def get_mpesa_mode():
    return get_setting('mpesa_mode', 'sandbox')


def get_mpesa_cfg():
    return MPESA_CREDS.get(get_mpesa_mode(), MPESA_CREDS['sandbox'])


# ─────────────────────────────────────────
# Jinja context — inject mode into every template
# ─────────────────────────────────────────

@app.context_processor
def inject_globals():
    try:
        mode = get_mpesa_mode()
    except Exception:
        mode = 'sandbox'
    return dict(mpesa_mode=mode)


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────

def format_currency(value):
    try:
        if value is None:
            return "0"
        num = round(float(value), 2)
        return "{:,.0f}".format(num) if num.is_integer() else "{:,.2f}".format(num)
    except Exception:
        return value

app.jinja_env.filters['format_currency'] = format_currency


def notify_payment_update(payment):
    items = [{"name": i.menu_item.name, "qty": i.quantity, "price": i.price}
             for i in payment.order_items]
    socketio.emit('payment_update', {
        "id":            payment.id,
        "phone":         payment.phone or "—",
        "amount":        payment.amount,
        "method":        payment.method,
        "pay_channel":   payment.pay_channel or "",
        "status":        payment.status,
        "notes":         payment.notes or "",
        "mpesa_receipt": payment.mpesa_receipt or "",
        "items":         items
    }, to=None)


def to_eat(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(EAT)


# ─────────────────────────────────────────
# M-Pesa
# ─────────────────────────────────────────

def get_mpesa_token():
    cfg = get_mpesa_cfg()
    r   = requests.get(
        f"{cfg['base_url']}/oauth/v1/generate?grant_type=client_credentials",
        auth=HTTPBasicAuth(cfg['key'], cfg['secret'])
    )
    r.raise_for_status()
    return r.json().get("access_token")


def initiate_stk_push(phone, amount):
    cfg       = get_mpesa_cfg()
    token     = get_mpesa_token()
    ts        = datetime.now().strftime('%Y%m%d%H%M%S')
    password  = base64.b64encode(
        f"{cfg['shortcode']}{cfg['passkey']}{ts}".encode()).decode()
    payload   = {
        "BusinessShortCode": cfg['shortcode'], "Password": password,
        "Timestamp": ts, "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount), "PartyA": phone, "PartyB": cfg['shortcode'],
        "PhoneNumber": phone, "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": "FedhaFlow", "TransactionDesc": "Cafeteria Order"
    }
    r    = requests.post(f"{cfg['base_url']}/mpesa/stkpush/v1/processrequest",
                         json=payload, headers={"Authorization": f"Bearer {token}"})
    data = r.json()
    if data.get('ResponseCode') == '0':
        return {"success": True, "CheckoutRequestID": data.get('CheckoutRequestID')}
    return {"success": False, "error": data.get('errorMessage', data)}


# ─────────────────────────────────────────
# Daily summary
# ─────────────────────────────────────────

def build_summary_text(payments, label="Today"):
    confirmed = [p for p in payments if p.status == 'Confirmed']
    pending   = [p for p in payments if p.status == 'Pending']
    cash      = sum(p.amount for p in confirmed if p.method == 'Cash')
    till      = sum(p.amount for p in confirmed if p.method == 'M-Pesa' and p.pay_channel == 'Till')
    phone_amt = sum(p.amount for p in confirmed if p.method == 'M-Pesa' and p.pay_channel == 'Phone')
    total     = cash + till + phone_amt

    item_counts = {}
    for p in confirmed:
        for oi in p.order_items:
            item_counts[oi.menu_item.name] = item_counts.get(oi.menu_item.name, 0) + oi.quantity
    top_items = sorted(item_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    lines = [
        f"📊 *FedhaFlow Daily Summary — {label}*", "",
        f"💰 *Total Sales: KES {format_currency(total)}*",
        f"   • Cash:         KES {format_currency(cash)}",
        f"   • M-Pesa Till:  KES {format_currency(till)}",
        f"   • M-Pesa Phone: KES {format_currency(phone_amt)}", "",
        f"📦 *Orders*",
        f"   • Confirmed: {len(confirmed)}",
        f"   • Pending:   {len(pending)}", "",
    ]
    if top_items:
        lines.append("🏆 *Top Items Sold*")
        for name, qty in top_items:
            lines.append(f"   • {name}: {qty} units")
        lines.append("")
    lines.append("— Sent by FedhaFlow POS")
    return "\n".join(lines)


def send_whatsapp_summary(text, numbers):
    import json
    api_keys = {}
    try:
        api_keys = json.loads(get_setting('callmebot_api_keys', '{}'))
    except Exception:
        pass
    results = []
    for number in numbers:
        number = number.strip()
        if not number:
            continue
        api_key = api_keys.get(number, '')
        if not api_key:
            results.append(f"⚠️ No CallMeBot API key for {number}")
            continue
        try:
            encoded = requests.utils.quote(text)
            r = requests.get(
                f"https://api.callmebot.com/whatsapp.php?phone={number}&text={encoded}&apikey={api_key}",
                timeout=10
            )
            results.append(f"✅ WhatsApp {number}: {'sent' if r.status_code==200 else f'HTTP {r.status_code}'}")
        except Exception as e:
            results.append(f"❌ WhatsApp {number}: {e}")
    return results


def send_email_summary(text, addresses):
    if not SMTP_USER or not SMTP_PASSWORD:
        return ["❌ Email not configured — set SMTP_USER and SMTP_PASSWORD in .env"]
    plain = text.replace('*', '')
    results = []
    for address in addresses:
        address = address.strip()
        if not address:
            continue
        try:
            msg            = MIMEMultipart('alternative')
            msg['Subject'] = f"FedhaFlow Daily Summary — {date.today().strftime('%d %b %Y')}"
            msg['From']    = SMTP_USER
            msg['To']      = address
            msg.attach(MIMEText(plain, 'plain'))
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, address, msg.as_string())
            results.append(f"✅ Email {address}: sent")
        except Exception as e:
            results.append(f"❌ Email {address}: {e}")
    return results


def run_daily_summary():
    with app.app_context():
        now       = datetime.now(EAT)
        utc_start = EAT.localize(datetime.combine(now.date(), datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
        utc_end   = EAT.localize(datetime.combine(now.date(), datetime.max.time())).astimezone(pytz.utc).replace(tzinfo=None)
        payments  = Payment.query.options(
            joinedload(Payment.order_items).joinedload(OrderItem.menu_item)
        ).filter(Payment.created_at >= utc_start, Payment.created_at <= utc_end).all()

        text    = build_summary_text(payments, now.strftime('%d %b %Y'))
        results = []

        if get_setting('summary_whatsapp_enabled') == 'true':
            nums     = [n for n in get_setting('summary_whatsapp_numbers','').split(',') if n.strip()]
            results += send_whatsapp_summary(text, nums)

        if get_setting('summary_email_enabled') == 'true':
            emails   = [e for e in get_setting('summary_email_addresses','').split(',') if e.strip()]
            results += send_email_summary(text, emails)

        return results


# ─────────────────────────────────────────
# Routes — pages
# ─────────────────────────────────────────

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
    menu_items = MenuItem.query.filter_by(active=True).all()
    return render_template("menu.html", menu_items=menu_items)


@app.route('/reports')
def reports():
    return render_template("reports.html",
                           deductions=Deduction.query.order_by(Deduction.name).all())


@app.route('/insights')
def insights():
    return render_template("insights.html")


@app.route('/settings')
def settings_page():
    cfg        = {s.key: s.value for s in AppSetting.query.all()}
    menu_items = MenuItem.query.order_by(MenuItem.name).all()
    return render_template("settings.html", cfg=cfg, menu_items=menu_items)


# ─────────────────────────────────────────
# Routes — Settings API
# ─────────────────────────────────────────

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    body = request.get_json()
    for key, value in body.items():
        set_setting(key, value)
    return jsonify({"ok": True})


@app.route('/api/settings/mpesa-mode', methods=['POST'])
def api_set_mpesa_mode():
    mode = request.get_json().get('mode', 'sandbox')
    if mode not in ('sandbox', 'live'):
        return jsonify({"error": "Invalid mode"}), 400
    set_setting('mpesa_mode', mode)
    return jsonify({"ok": True, "mode": mode})


# ─────────────────────────────────────────
# Routes — Menu item management
# ─────────────────────────────────────────

@app.route('/api/menu-items', methods=['GET'])
def api_get_menu_items():
    items = MenuItem.query.order_by(MenuItem.name).all()
    return jsonify([{"id": i.id, "name": i.name, "price": i.price,
                     "image_url": i.image_url or "", "active": i.active}
                    for i in items])


@app.route('/api/menu-items', methods=['POST'])
def api_add_menu_item():
    body = request.get_json()
    name = (body.get('name') or '').strip()
    try:
        price = float(body.get('price', 0))
    except Exception:
        price = 0
    if not name or price <= 0:
        return jsonify({"error": "Name and valid price required"}), 400
    item = MenuItem(name=name, price=price,
                    image_url=(body.get('image_url') or '/static/images/default.jpg').strip(),
                    active=True)
    db.session.add(item)
    db.session.commit()
    return jsonify({"id": item.id, "name": item.name, "price": item.price,
                    "image_url": item.image_url, "active": item.active}), 201


@app.route('/api/menu-items/<int:item_id>', methods=['PUT'])
def api_update_menu_item(item_id):
    item = db.session.get(MenuItem, item_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json()
    if 'name'      in body: item.name      = body['name'].strip()
    if 'price'     in body: item.price     = float(body['price'])
    if 'image_url' in body: item.image_url = body['image_url'].strip()
    if 'active'    in body: item.active    = bool(body['active'])
    db.session.commit()
    return jsonify({"ok": True})


@app.route('/api/menu-items/<int:item_id>', methods=['DELETE'])
def api_delete_menu_item(item_id):
    item = db.session.get(MenuItem, item_id)
    if not item:
        return jsonify({"error": "Not found"}), 404
    item.active = False   # soft-delete keeps order history
    db.session.commit()
    return jsonify({"ok": True})


# ─────────────────────────────────────────
# Routes — Daily summary
# ─────────────────────────────────────────

@app.route('/api/send-summary', methods=['POST'])
def api_send_summary():
    results = run_daily_summary()
    return jsonify({"results": results})


# ─────────────────────────────────────────
# Routes — Orders
# ─────────────────────────────────────────

@app.route('/checkout', methods=['POST'])
def checkout():
    selected_item_ids = request.form.getlist('items')
    method = request.form.get("method")
    phone  = request.form.get("phone") if method == "M-Pesa" else None
    status = "Pending" if method == "M-Pesa" else "Confirmed"

    if not selected_item_ids:
        flash("No items selected!", "warning"); return redirect(url_for('menu'))
    if method == "M-Pesa" and not phone:
        flash("Phone number required for M-Pesa.", "warning"); return redirect(url_for('menu'))

    total = sum(
        (db.session.get(MenuItem, int(iid)).price * int(request.form.get(f'qty_{iid}', 0)))
        for iid in selected_item_ids
        if int(request.form.get(f'qty_{iid}', 0)) > 0 and db.session.get(MenuItem, int(iid))
    )
    if total == 0:
        flash("Select at least one item with quantity > 0!", "warning"); return redirect(url_for('menu'))

    payment = Payment(phone=phone, amount=total, method=method,
                      pay_channel="Till" if method == "M-Pesa" else None, status=status)
    db.session.add(payment)
    db.session.flush()

    for iid in selected_item_ids:
        item = db.session.get(MenuItem, int(iid))
        qty  = int(request.form.get(f'qty_{iid}', 0))
        if qty > 0 and item:
            db.session.add(OrderItem(payment_id=payment.id, menu_item_id=item.id,
                                     quantity=qty, price=item.price))
    db.session.commit()
    notify_payment_update(payment)

    if method == "M-Pesa":
        stk = initiate_stk_push(phone, total)
        if stk.get("success"):
            payment.checkout_request_id = stk["CheckoutRequestID"]
            db.session.commit()
            flash(f"STK Push sent to {phone}.", "info")
        else:
            flash(f"STK Push failed: {stk.get('error')}", "danger")

    flash(f"Order placed! Total: KES {total}", "success")
    return redirect(url_for('dashboard'))


@app.route('/edit-order/<int:payment_id>')
def edit_order(payment_id):
    payment    = Payment.query.options(
        joinedload(Payment.order_items).joinedload(OrderItem.menu_item)
    ).get_or_404(payment_id)
    menu_items = MenuItem.query.filter_by(active=True).all()
    qty_map    = {oi.menu_item_id: oi.quantity for oi in payment.order_items}
    return render_template("menu.html", menu_items=menu_items,
                           edit_payment=payment, qty_map=qty_map)


@app.route('/update-order/<int:payment_id>', methods=['POST'])
def update_order(payment_id):
    payment = db.session.get(Payment, payment_id)
    if not payment:
        flash("Order not found.", "danger"); return redirect(url_for('dashboard'))

    selected_item_ids = request.form.getlist('items')
    method = request.form.get("method")
    phone  = request.form.get("phone") if method == "M-Pesa" else None

    if not selected_item_ids:
        flash("No items selected!", "warning"); return redirect(url_for('edit_order', payment_id=payment_id))
    if method == "M-Pesa" and not phone:
        flash("Phone required for M-Pesa.", "warning"); return redirect(url_for('edit_order', payment_id=payment_id))

    total = sum(
        (db.session.get(MenuItem, int(iid)).price * int(request.form.get(f'qty_{iid}', 0)))
        for iid in selected_item_ids
        if int(request.form.get(f'qty_{iid}', 0)) > 0 and db.session.get(MenuItem, int(iid))
    )
    if total == 0:
        flash("Select at least one item with quantity > 0!", "warning")
        return redirect(url_for('edit_order', payment_id=payment_id))

    OrderItem.query.filter_by(payment_id=payment.id).delete()
    for iid in selected_item_ids:
        item = db.session.get(MenuItem, int(iid))
        qty  = int(request.form.get(f'qty_{iid}', 0))
        if qty > 0 and item:
            db.session.add(OrderItem(payment_id=payment.id, menu_item_id=item.id,
                                     quantity=qty, price=item.price))

    payment.phone   = phone
    payment.amount  = total
    payment.method  = method
    payment.pay_channel = "Till" if method == "M-Pesa" else None
    payment.status  = "Pending" if method == "M-Pesa" else "Confirmed"
    db.session.commit()
    notify_payment_update(payment)

    if method == "M-Pesa":
        stk = initiate_stk_push(phone, total)
        if stk.get("success"):
            payment.checkout_request_id = stk["CheckoutRequestID"]
            db.session.commit()
            flash(f"Order updated. STK Push sent to {phone}.", "info")
        else:
            flash(f"Order updated but STK failed: {stk.get('error')}", "danger")
    else:
        flash(f"Order updated. Total: KES {total}", "success")
    return redirect(url_for('dashboard'))


@app.route('/confirm-manual/<int:payment_id>', methods=['POST'])
def confirm_manual(payment_id):
    payment = db.session.get(Payment, payment_id)
    if not payment:
        flash("Order not found.", "danger"); return redirect(url_for('dashboard'))
    note = request.form.get("note", "").strip()
    payment.notes       = note or "Paid through phone"
    payment.status      = "Confirmed"
    payment.pay_channel = "Phone"
    db.session.commit()
    notify_payment_update(payment)
    flash(f"Payment #{payment_id} confirmed.", "success")
    return redirect(url_for('dashboard'))


@app.route('/delete-order/<int:payment_id>', methods=['POST'])
def delete_order(payment_id):
    payment = db.session.get(Payment, payment_id)
    if not payment:
        flash("Order not found.", "danger"); return redirect(url_for('dashboard'))
    OrderItem.query.filter_by(payment_id=payment_id).delete()
    db.session.delete(payment)
    db.session.commit()
    socketio.emit('payment_deleted', {"id": payment_id}, to=None)
    flash(f"Order #{payment_id} deleted.", "success")
    return redirect(url_for('dashboard'))


@app.route('/resend-stk/<int:payment_id>')
def resend_stk(payment_id):
    payment = db.session.get(Payment, payment_id)
    if payment and payment.status == "Pending" and payment.method == "M-Pesa":
        stk = initiate_stk_push(payment.phone, payment.amount)
        if stk.get("success"):
            payment.checkout_request_id = stk["CheckoutRequestID"]
            db.session.commit()
            flash("STK Push resent.", "info")
        else:
            flash(f"STK failed: {stk.get('error')}", "danger")
    return redirect(url_for('dashboard'))


# ─────────────────────────────────────────
# Routes — M-Pesa callbacks
# ─────────────────────────────────────────

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = request.get_json()
    try:
        body        = data['Body']['stkCallback']
        checkout_id = body.get('CheckoutRequestID')
        payment     = Payment.query.filter_by(
            checkout_request_id=checkout_id, status="Pending").first()
        if payment and body.get('ResultCode') == 0:
            for item in body.get('CallbackMetadata', {}).get('Item', []):
                if item['Name'] == 'MpesaReceiptNumber':
                    payment.mpesa_receipt = item['Value']
            payment.status = "Confirmed"; payment.pay_channel = "Till"
            db.session.commit(); db.session.refresh(payment)
            notify_payment_update(payment)
    except Exception as e:
        print("STK callback error:", e)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/c2b/validation", methods=["POST"])
def c2b_validation():
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/c2b/confirmation", methods=["POST"])
def c2b_confirmation():
    data = request.get_json()
    try:
        amount  = float(data.get("TransAmount", 0))
        phone   = str(data.get("MSISDN", ""))
        receipt = data.get("TransID", "")
        if phone.startswith("0") and len(phone) == 10:
            phone = "254" + phone[1:]
        payment = Payment.query.filter_by(
            phone=phone, amount=amount, method="M-Pesa", status="Pending").first()
        if payment:
            payment.status = "Confirmed"; payment.pay_channel = "Phone"
            payment.mpesa_receipt = receipt; payment.notes = "Paid through phone"
        else:
            # CREATE NEW PAYMENT for pay before order
            payment = Payment(
                phone=phone,
                amount=amount,
                method="M-Pesa",
                pay_channel="Phone",
                status="Confirmed",
                mpesa_receipt=receipt,
                notes="C2B direct payment"
            )
            db.session.add(payment)
        db.session.commit(); db.session.refresh(payment)
        notify_payment_update(payment)
        
    except Exception as e:
        print("C2B error:", e)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


# ─────────────────────────────────────────
# Routes — Data APIs
# ─────────────────────────────────────────

@app.route('/api/payments')
def api_payments():
    payments = Payment.query.options(
        joinedload(Payment.order_items).joinedload(OrderItem.menu_item)
    ).order_by(Payment.id.desc()).all()
    total_sales = 0
    data = []
    for p in payments:
        if p.status == "Confirmed": total_sales += p.amount
        data.append({
            "id": p.id, "phone": p.phone or "—",
            "amount": format_currency(p.amount), "method": p.method,
            "pay_channel": p.pay_channel or "", "status": p.status,
            "notes": p.notes or "", "mpesa_receipt": p.mpesa_receipt or "",
            "items": [{"name": oi.menu_item.name, "qty": format_currency(oi.quantity),
                       "price": format_currency(oi.price)} for oi in p.order_items]
        })
    return jsonify({"payments": data, "total_sales": format_currency(total_sales)})


@app.route('/api/sales-data')
def api_sales_data():
    df, dt = request.args.get('from'), request.args.get('to')
    query  = Payment.query.options(
        joinedload(Payment.order_items).joinedload(OrderItem.menu_item))
    if df and dt:
        try:
            s = EAT.localize(datetime.combine(datetime.strptime(df,"%Y-%m-%d").date(), datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
            e = EAT.localize(datetime.combine(datetime.strptime(dt,"%Y-%m-%d").date(), datetime.max.time())).astimezone(pytz.utc).replace(tzinfo=None)
            query = query.filter(Payment.created_at >= s, Payment.created_at <= e)
        except ValueError:
            pass
    payments = query.order_by(Payment.created_at).all()
    data = []
    for p in payments:
        eat_dt = to_eat(p.created_at)
        data.append({
            "id": p.id, "phone": p.phone or "", "amount": p.amount or 0,
            "method": p.method or "", "pay_channel": p.pay_channel or "",
            "status": p.status or "", "date": eat_dt.date().isoformat() if eat_dt else "",
            "items": [{"name": oi.menu_item.name, "qty": oi.quantity, "price": oi.price}
                      for oi in p.order_items]
        })
    return jsonify({"payments": data})


@app.route('/api/deductions', methods=['GET'])
def get_deductions():
    return jsonify([{"id": d.id, "name": d.name, "default_amount": d.default_amount}
                    for d in Deduction.query.order_by(Deduction.name).all()])


@app.route('/api/deductions', methods=['POST'])
def save_deduction():
    body = request.get_json()
    name = (body.get("name") or "").strip()
    if not name: return jsonify({"error": "Name required"}), 400
    d = Deduction(name=name, default_amount=float(body.get("default_amount", 0)))
    db.session.add(d); db.session.commit()
    return jsonify({"id": d.id, "name": d.name, "default_amount": d.default_amount}), 201


@app.route('/api/deductions/<int:ded_id>', methods=['DELETE'])
def delete_deduction(ded_id):
    d = db.session.get(Deduction, ded_id)
    if not d: return jsonify({"error": "Not found"}), 404
    db.session.delete(d); db.session.commit()
    return jsonify({"deleted": ded_id})


# ─────────────────────────────────────────
# Routes — PDF generation (reports + insights)
# ─────────────────────────────────────────

@app.route('/generate-pdf', methods=['POST'])
def generate_pdf():
    from collections import defaultdict
    body      = request.get_json()
    date_from = body.get("date_from")
    date_to   = body.get("date_to")
    deductions= body.get("deductions", [])
    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d").date()
        d_to   = datetime.strptime(date_to,   "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "Invalid date"}), 400

    s = EAT.localize(datetime.combine(d_from, datetime.min.time())).astimezone(pytz.utc).replace(tzinfo=None)
    e = EAT.localize(datetime.combine(d_to,   datetime.max.time())).astimezone(pytz.utc).replace(tzinfo=None)
    payments = Payment.query.options(
        joinedload(Payment.order_items).joinedload(OrderItem.menu_item)
    ).filter(Payment.created_at >= s, Payment.created_at <= e).order_by(Payment.created_at).all()

    days = defaultdict(list)
    for p in payments:
        eat_dt = to_eat(p.created_at)
        days[eat_dt.date() if eat_dt else date.today()].append(p)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    sty = getSampleStyleSheet()
    def ps(n, p='Normal', **kw): return ParagraphStyle(n, parent=sty[p], **kw)
    norm  = ps('n', fontSize=9)
    bold  = ps('b', fontSize=9, fontName='Helvetica-Bold')
    right = ps('r', fontSize=9, alignment=TA_RIGHT)
    day_h = ps('dh','Heading2',fontSize=13,spaceBefore=16,spaceAfter=6,textColor=colors.HexColor('#1a5276'))
    sec_h = ps('sh',fontSize=10,fontName='Helvetica-Bold',spaceBefore=8,spaceAfter=4,textColor=colors.HexColor('#555'))

    def kes(v): return f"KES {format_currency(v)}"
    def mktbl(rows, widths, hdr=None):
        t = Table(([hdr] if hdr else []) + rows, colWidths=widths)
        cmds = [('FONTSIZE',(0,0),(-1,-1),9),
                ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.white,colors.HexColor('#f4f6f7')]),
                ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#ccc')),
                ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
                ('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3)]
        if hdr: cmds+=[('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1a5276')),
                        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
                        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold')]
        t.setStyle(TableStyle(cmds)); return t

    story = []
    story.append(Paragraph("FedhaFlow", ps('tt','Title',fontSize=18,spaceAfter=4)))
    story.append(Paragraph(
        f"Sales Report — {d_from.strftime('%d %b %Y')}" if d_from==d_to
        else f"Sales Report — {d_from.strftime('%d %b %Y')} to {d_to.strftime('%d %b %Y')}",
        ps('ss',fontSize=10,textColor=colors.grey,spaceAfter=4)))
    story.append(Paragraph(
        f"Generated: {datetime.now(EAT).strftime('%d %b %Y, %I:%M %p')} EAT",
        ps('sg',fontSize=10,textColor=colors.grey,spaceAfter=12)))
    story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#1a5276'),spaceAfter=12))

    gc=gt=gph=gpend=gconf=0

    for dd in sorted(days.keys()):
        dp = days[dd]
        story.append(Paragraph(dd.strftime("%A, %d %B %Y"), day_h))
        cash_o  = [p for p in dp if p.method=="Cash" and p.status=="Confirmed"]
        till_o  = [p for p in dp if p.method=="M-Pesa" and p.pay_channel=="Till" and p.status=="Confirmed"]
        phone_o = [p for p in dp if p.method=="M-Pesa" and p.pay_channel=="Phone" and p.status=="Confirmed"]
        pend_o  = [p for p in dp if p.status=="Pending"]

        def otbl(orders, show_ph=False, show_rcpt=False):
            if not orders: return Paragraph("  No transactions.", norm)
            hdr=[ps('H',fontSize=9,fontName='Helvetica-Bold')]
            cols=[Paragraph("ID",bold),Paragraph("Items",bold)]
            if show_ph: cols.insert(1,Paragraph("Phone",bold))
            if show_rcpt: cols.append(Paragraph("Receipt",bold))
            cols.append(Paragraph("Amount",bold))
            rows=[]
            for p in orders:
                its=", ".join(f"{oi.quantity}×{oi.menu_item.name}" for oi in p.order_items)
                row=[Paragraph(str(p.id),norm),Paragraph(its,norm)]
                if show_ph: row.insert(1,Paragraph(p.phone or "—",norm))
                if show_rcpt: row.append(Paragraph(p.mpesa_receipt or "—",norm))
                row.append(Paragraph(kes(p.amount),right)); rows.append(row)
            if show_ph and show_rcpt: w=[1.2*cm,3*cm,6.5*cm,3.3*cm,2*cm]
            elif show_ph:             w=[1.2*cm,3.5*cm,8*cm,3.3*cm]
            else:                     w=[1.2*cm,11.5*cm,3.3*cm]
            return mktbl(rows,w,hdr=cols)

        dc=sum(p.amount for p in cash_o); dt=sum(p.amount for p in till_o)
        dph=sum(p.amount for p in phone_o); dpnd=sum(p.amount for p in pend_o)
        dtot=dc+dt+dph
        story.append(Paragraph(f"💵  Cash — {kes(dc)}", sec_h))
        story.append(otbl(cash_o))
        story.append(Spacer(1,4))
        story.append(Paragraph(f"📲  M-Pesa Till — {kes(dt)}", sec_h))
        story.append(otbl(till_o, show_ph=True, show_rcpt=True))
        story.append(Spacer(1,4))
        story.append(Paragraph(f"📞  M-Pesa Phone — {kes(dph)}", sec_h))
        story.append(otbl(phone_o, show_ph=True, show_rcpt=True))
        story.append(Spacer(1,4))
        if pend_o:
            story.append(Paragraph(f"⏳  Pending — {kes(dpnd)}", sec_h))
            story.append(otbl(pend_o, show_ph=True))
            story.append(Spacer(1,4))
        sub=Table([[Paragraph("Day Total (Confirmed)",bold),Paragraph(kes(dtot),right)]],
                  colWidths=[13*cm,3*cm])
        sub.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#d5e8f5')),
            ('FONTNAME',(0,0),(-1,-1),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),10),
            ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
            ('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        story.append(sub); story.append(Spacer(1,10))
        gc+=dc; gt+=dt; gph+=dph; gpend+=dpnd; gconf+=dtot

    story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#1a5276'),spaceBefore=8,spaceAfter=8))
    story.append(Paragraph("Summary", day_h))
    srows=[[Paragraph("Cash",norm),Paragraph(kes(gc),right)],
           [Paragraph("M-Pesa Till",norm),Paragraph(kes(gt),right)],
           [Paragraph("M-Pesa Phone",norm),Paragraph(kes(gph),right)],
           [Paragraph("Total Confirmed",bold),Paragraph(kes(gconf),right)]]
    if gpend: srows.append([Paragraph("Pending",norm),Paragraph(kes(gpend),right)])
    story.append(mktbl(srows,[13*cm,3*cm]))
    story.append(Spacer(1,12))

    tded=0
    if deductions:
        story.append(Paragraph("Deductions", day_h))
        drows=[]
        for d in deductions:
            amt=float(d.get("amount",0)); tded+=amt
            drows.append([Paragraph(d.get("name",""),norm),Paragraph(kes(amt),right)])
        drows.append([Paragraph("Total Deductions",bold),Paragraph(kes(tded),right)])
        story.append(mktbl(drows,[13*cm,3*cm])); story.append(Spacer(1,12))

    profit=gconf-tded
    pc=colors.HexColor('#1e8449') if profit>=0 else colors.HexColor('#c0392b')
    pt=Table([[Paragraph("NET PROFIT",ps('pl',fontSize=12,fontName='Helvetica-Bold')),
               Paragraph(kes(profit),ps('pa',fontSize=12,fontName='Helvetica-Bold',alignment=TA_RIGHT,textColor=pc))]],
             colWidths=[13*cm,3*cm])
    pt.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#eafaf1') if profit>=0 else colors.HexColor('#fdecea')),
        ('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
        ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
    story.append(pt)
    doc.build(story); buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"FedhaFlow_Report_{d_from}_{d_to}.pdf",
                     mimetype='application/pdf')


@app.route('/generate-insights-pdf', methods=['POST'])
def generate_insights_pdf():
    from html.parser import HTMLParser
    body      = request.get_json()
    analytics = body.get("analytics", {})
    prose_html= body.get("prose", "")
    chart_imgs= body.get("chart_images", {})
    df, dt    = body.get("date_from",""), body.get("date_to","")

    class S(HTMLParser):
        def __init__(self): super().__init__(); self.r=[]
        def handle_starttag(self,t,a):
            if t in('h3','h4'): self.r.append('\n\n')
            elif t=='li': self.r.append('\n  • ')
        def handle_endtag(self,t):
            if t in('h3','h4'): self.r.append('\n')
        def handle_data(self,d): self.r.append(d)
        def text(self): return ''.join(self.r).strip()
    s=S(); s.feed(prose_html); prose=s.text()

    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=A4,leftMargin=2*cm,rightMargin=2*cm,topMargin=2*cm,bottomMargin=2*cm)
    sty=getSampleStyleSheet()
    def ps(n,p='Normal',**kw): return ParagraphStyle(n,parent=sty[p],**kw)
    norm=ps('n',fontSize=9,spaceAfter=3); bold=ps('b',fontSize=9,fontName='Helvetica-Bold')
    right=ps('r',fontSize=9,alignment=TA_RIGHT)
    h2=ps('h2','Heading2',fontSize=12,spaceBefore=14,spaceAfter=6,textColor=colors.HexColor('#1a5276'))
    story=[]
    story.append(Paragraph("FedhaFlow — Sales Insights",ps('tt','Title',fontSize=18,spaceAfter=4)))
    story.append(Paragraph(f"Period: {df} → {dt}",ps('ss',fontSize=10,textColor=colors.grey,spaceAfter=12)))
    story.append(Paragraph(f"Generated: {datetime.now(EAT).strftime('%d %b %Y, %I:%M %p')} EAT",
                            ps('sg',fontSize=10,textColor=colors.grey,spaceAfter=12)))
    story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#1a5276'),spaceAfter=12))
    story.append(Paragraph("Key Metrics",h2))
    rows=[[Paragraph(k,bold),Paragraph(v,right)] for k,v in [
        ("Total Revenue",f"KES {format_currency(analytics.get('totalRevenue',0))}"),
        ("Confirmed Orders",str(analytics.get('totalOrders',0))),
        ("Avg Order Value",f"KES {format_currency(analytics.get('avgOrder',0))}"),
        ("Largest Order",f"KES {format_currency(analytics.get('maxOrder',0))}"),
        ("Cash",f"KES {format_currency(analytics.get('cashTotal',0))}"),
        ("M-Pesa Till",f"KES {format_currency(analytics.get('tillTotal',0))}"),
        ("M-Pesa Phone",f"KES {format_currency(analytics.get('phoneTotal',0))}"),
        ("Busiest Day",f"{analytics.get('busiestDay','—')} (KES {format_currency(analytics.get('busiestAmt',0))})"),
    ]]
    t=Table(rows,colWidths=[10*cm,6*cm])
    t.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),9),
        ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.white,colors.HexColor('#f4f6f7')]),
        ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#ccc')),
        ('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),
        ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
    story.append(t); story.append(Spacer(1,12))
    for cid,clbl in [('chartPayment','Payment Breakdown'),('chartItems','Top Items'),('chartDaily','Daily Trend')]:
        img=chart_imgs.get(cid,'')
        if img and img.startswith('data:image/png;base64,'):
            story.append(Paragraph(clbl,h2))
            from reportlab.platypus import Image as RLImage
            story.append(RLImage(io.BytesIO(base64.b64decode(img.split(',')[1])),width=14*cm,height=7*cm))
            story.append(Spacer(1,10))
    if prose:
        story.append(HRFlowable(width="100%",thickness=0.5,color=colors.HexColor('#ccc'),spaceAfter=8))
        story.append(Paragraph("AI Analysis",h2))
        for line in prose.split('\n'):
            line=line.strip()
            if line: story.append(Paragraph(line,ps('bul',fontSize=9,leftIndent=12,spaceAfter=2) if line.startswith('•') else norm))
    doc.build(story); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=f"FedhaFlow_Insights_{df}_{dt}.pdf",mimetype='application/pdf')


# ─────────────────────────────────────────
# Seed
# ─────────────────────────────────────────

def seed_menu():
    if MenuItem.query.count() == 0:
        items = [
            MenuItem(name="Smokies",         price=40,  image_url="/static/images/smokies.jpg"),
            MenuItem(name="Chapati",          price=25,  image_url="/static/images/chapati.jpg"),
            MenuItem(name="Kachumbari",       price=1,   image_url="/static/images/kachumbari.jpg"),
            MenuItem(name="Smocha (smokies)", price=70,  image_url="/static/images/smocha.jpg"),
            MenuItem(name="Smocha (sausage)", price=80,  image_url="/static/images/smocha.jpg"),
            MenuItem(name="Sausage",          price=50,  image_url="/static/images/sausage.png"),
            MenuItem(name="Hotdog (sausage)", price=100, image_url="/static/images/hotdog.jpg"),
            MenuItem(name="Hotdog (smokies)", price=80,  image_url="/static/images/hotdog.jpg"),
            MenuItem(name="Buns",             price=25,  image_url="/static/images/buns.jpg"),
        ]
        db.session.bulk_save_objects(items); db.session.commit()


def seed_settings():
    defaults = {
        'mpesa_mode':               'sandbox',
        'summary_whatsapp_enabled': 'false',
        'summary_email_enabled':    'false',
        'summary_whatsapp_numbers': '',
        'summary_email_addresses':  '',
        'callmebot_api_keys':       '{}',
    }
    for k, v in defaults.items():
        if not AppSetting.query.get(k):
            db.session.add(AppSetting(key=k, value=v))
    db.session.commit()


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_menu()
        seed_settings()
    socketio.run(app, host='0.0.0.0', port=4000, debug=True)