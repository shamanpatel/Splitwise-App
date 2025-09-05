from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
import os

app = Flask(__name__, static_folder=None)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    paid_expenses = db.relationship('Expense', backref='payer', foreign_keys='Expense.payer_id', lazy=True)
    owed_expenses = db.relationship('ExpenseSplit', backref='ower', foreign_keys='ExpenseSplit.user_id', lazy=True)

class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    currency = db.Column(db.String(10), nullable=False, default='USD')
    payer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    splits = db.relationship('ExpenseSplit', backref='expense', cascade='all, delete-orphan', lazy=True)

class ExpenseSplit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expense_id = db.Column(db.Integer, db.ForeignKey('expense.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    percentage = db.Column(db.Float, nullable=True)  # can be null for equal split

with app.app_context():
    db.create_all()

# --- Routes ---
@app.route('/')
def index():
    return render_template('index.html')

# Users
@app.route('/users', methods=['GET'])
def list_users():
    users = User.query.order_by(User.id).all()
    return jsonify([{'id': u.id, 'username': u.username, 'email': u.email} for u in users])

@app.route('/users', methods=['POST'])
def create_user():
    data = request.get_json() or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip()
    if not username or not email:
        return jsonify({'error': 'username and email required'}), 400
    if User.query.filter((User.username == username) | (User.email == email)).first():
        return jsonify({'error': 'username or email already exists'}), 400
    u = User(username=username, email=email)
    db.session.add(u)
    db.session.commit()
    return jsonify({'id': u.id, 'username': u.username, 'email': u.email}), 201

# Expenses
@app.route('/expenses', methods=['GET'])
def list_expenses():
    expenses = Expense.query.order_by(Expense.id.desc()).all()
    def split_dict(s):
        return {'id': s.id, 'user_id': s.user_id, 'amount': round(float(s.amount), 2), 'percentage': s.percentage}
    resp = []
    for e in expenses:
        resp.append({
            'id': e.id,
            'description': e.description,
            'amount': round(float(e.amount), 2),
            'currency': e.currency,
            'payer_id': e.payer_id,
            'splits': [split_dict(s) for s in e.splits]
        })
    return jsonify(resp)

@app.route('/expenses', methods=['POST'])
def create_expense():
    data = request.get_json() or {}
    required = ['description', 'amount', 'currency', 'payer_id', 'splits']
    if any(k not in data for k in required):
        return jsonify({'error': 'missing fields'}), 400
    description = data['description'].strip()
    try:
        amount = float(data['amount'])
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid amount'}), 400
    currency = data['currency']
    payer_id = int(data['payer_id'])
    splits = data['splits'] or []

    if amount <= 0 or not splits:
        return jsonify({'error': 'invalid split/amount'}), 400

    # Validate users
    if not User.query.get(payer_id):
        return jsonify({'error': 'invalid payer'}), 400
    user_ids = {int(s['user_id']) for s in splits}
    if any(User.query.get(uid) is None for uid in user_ids):
        return jsonify({'error': 'one or more split users are invalid'}), 400

    # Ensure split totals make sense (allow minor rounding error)
    total_split = round(sum(float(s['amount']) for s in splits), 2)
    if abs(total_split - round(amount, 2)) > 0.01:
        return jsonify({'error': f'split totals ({total_split}) do not equal amount ({round(amount,2)})'}), 400

    e = Expense(description=description, amount=amount, currency=currency, payer_id=payer_id)
    db.session.add(e)
    db.session.flush()  # get e.id
    for s in splits:
        db.session.add(ExpenseSplit(
            expense_id=e.id,
            user_id=int(s['user_id']),
            amount=float(s['amount']),
            percentage=(None if s.get('percentage') is None else float(s['percentage']))
        ))
    db.session.commit()
    return jsonify({'id': e.id}), 201

# Balances
# Old logic:
# @app.route('/balances', methods=['GET'])
# def get_balances():
#     users = User.query.all()
#     # Calculate how much each user paid
#     paid_map = {u.id: 0.0 for u in users}
#     owes_map = {u.id: 0.0 for u in users}

#     for e in Expense.query.all():
#         paid_map[e.payer_id] += float(e.amount)
#         for s in e.splits:
#             owes_map[s.user_id] += float(s.amount)

#     result = {}
#     for u in users:
#         total_paid = round(paid_map[u.id], 2)
#         total_owes = round(owes_map[u.id], 2)
#         net = round(total_paid - total_owes, 2)
#         result[u.id] = {
#             'user_id': u.id,
#             'username': u.username,
#             'total_paid': total_paid,
#             'total_owes': total_owes,
#             'total_owed': total_paid,  # for compatibility with frontend label "Owed"
#             'net_balance': net
#         }
#     return jsonify(result)

# New (corrected) logic:
@app.route('/balances', methods=['GET'])
def get_balances():
    users = User.query.all()
    balances = {user.id: {'total_paid': 0.0, 'total_owes': 0.0, 'net_balance': 0.0} for user in users}
    
    # Calculate how much each user paid
    for expense in Expense.query.all():
        payer_id = expense.payer_id
        if payer_id in balances:
            balances[payer_id]['total_paid'] += float(expense.amount)
            
    # Calculate how much each user owes
    for split in ExpenseSplit.query.all():
        user_id = split.user_id
        if user_id in balances:
            balances[user_id]['total_owes'] += float(split.amount)
            
    result = {}
    for user in users:
        total_paid = round(balances[user.id]['total_paid'], 2)
        total_owes = round(balances[user.id]['total_owes'], 2)
        net = round(total_paid - total_owes, 2)
        
        result[user.id] = {
            'user_id': user.id,
            'username': user.username,
            'total_paid': total_paid,
            'total_owes': total_owes,
            'net_balance': net
        }
    
    return jsonify(result)

# User report
@app.route('/user_report/<int:user_id>', methods=['GET'])
def user_report(user_id):
    u = User.query.get(user_id)
    if not u:
        return jsonify({'error': 'user not found'}), 404

    paid = [{'description': e.description, 'amount': round(float(e.amount),2), 'currency': e.currency}
            for e in Expense.query.filter_by(payer_id=user_id).all()]

    owed_splits = ExpenseSplit.query.filter_by(user_id=user_id).all()
    owes = [{
        'description': s.expense.description,
        'amount': round(float(s.amount),2),
        'currency': s.expense.currency,
        'percentage': s.percentage
    } for s in owed_splits]

    return jsonify({'paid': paid, 'owes': owes})

# Dangerous but handy for development
@app.route('/clear_all', methods=['POST'])
def clear_all():
    db.session.query(ExpenseSplit).delete()
    db.session.query(Expense).delete()
    db.session.query(User).delete()
    db.session.commit()
    return jsonify({'status': 'cleared'})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
