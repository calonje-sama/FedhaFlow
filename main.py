# app.py
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "change_this_to_secure_key"

# ---------------------------
# Dummy Data
# ---------------------------
menu_items = [
    {"id": 1, "name": "Tea", "price": 20},
    {"id": 2, "name": "Chapati", "price": 15},
    {"id": 3, "name": "Rice", "price": 50},
    {"id": 4, "name": "Beef", "price": 120},
]

payments = [
    # Example dummy payment
    {"id": 1, "phone": "0712345678", "amount": 50, "method": "M-Pesa", "status": "Confirmed"},
]

# ---------------------------
# Routes
# ---------------------------

@app.route('/')
def dashboard():
    return render_template("dashboard.html", payments=payments)

@app.route('/menu')
def menu():
    return render_template("menu.html", menu_items=menu_items)

@app.route('/checkout', methods=['POST'])
def checkout():
    selected_items = request.form.getlist('items')  # list of ids as strings
    if not selected_items:
        flash("No items selected!", "warning")
        return redirect(url_for('menu'))

    # Calculate total
    total = sum([item['price'] for item in menu_items if str(item['id']) in selected_items])

    # Store a dummy payment (we'll replace with real DB & M-Pesa later)
    payments.append({
        "id": len(payments) + 1,
        "phone": request.form.get("phone", "Unknown"),
        "amount": total,
        "method": request.form.get("method", "Cash"),
        "status": "Pending"
    })

    flash(f"Order placed! Total: KES {total}", "success")
    return redirect(url_for('dashboard'))

# ---------------------------
# Run App
# ---------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=4000, debug=True)