#!/usr/bin/env python3
"""
Cuemath Travel Request Portal — Employee-facing (deployed on Render)
Google Sign-In → submit request → writes to Google Sheet → notifies Akash → track status
"""

import os
import json
import time
import uuid
import threading
from flask import Flask, request, jsonify
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, static_url_path='')

# ── Rate limiting ──
_rate_limits = {}
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 3600

def _check_rate_limit(email):
    now = time.time()
    timestamps = _rate_limits.get(email, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(timestamps) >= RATE_LIMIT_MAX:
        _rate_limits[email] = timestamps
        return False
    timestamps.append(now)
    _rate_limits[email] = timestamps
    return True

# ── Config ──
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
SHEET_ID = os.environ.get('SHEET_ID', '')
TAB_NAME = 'Travel Requests'
ALLOWED_DOMAIN = os.environ.get('ALLOWED_DOMAIN', '')

# ── Google Service Account ──
_sa_creds = None

def _get_sa_creds():
    global _sa_creds
    if _sa_creds:
        return _sa_creds
    private_key = os.environ.get('SA_PRIVATE_KEY', '')
    if private_key:
        info = {
            "type": "service_account",
            "project_id": "citric-banner-488421-j7",
            "private_key_id": "b1f810a3381bf0bd3ce0c5acff8fed4705cad384",
            "private_key": private_key,
            "client_email": "meeting-request-writer@citric-banner-488421-j7.iam.gserviceaccount.com",
            "client_id": "104241191790303953628",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/meeting-request-writer%40citric-banner-488421-j7.iam.gserviceaccount.com",
            "universe_domain": "googleapis.com"
        }
        _sa_creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
    return _sa_creds

_sheets_service = None

def get_sheets_service():
    global _sheets_service
    if _sheets_service:
        return _sheets_service
    creds = _get_sa_creds()
    if creds:
        _sheets_service = build('sheets', 'v4', credentials=creds)
    return _sheets_service


# ── Sheet Headers ──
HEADERS = [
    'Request ID', 'Employee Name', 'Employee Email', 'Band',
    'Travel Type', 'Destination City', 'Destination Country',
    'Start Date', 'End Date', 'Purpose', 'Meetings/Events',
    'Status', 'Current Step', 'Manager Email',
    'Budget Estimate', 'Created At', 'Updated At',
    'Approved By', 'Rejected Reason', 'Admin Notes'
]


def _ensure_tab():
    service = get_sheets_service()
    if not service:
        return
    try:
        meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
        tabs = [s['properties']['title'] for s in meta.get('sheets', [])]
        if TAB_NAME not in tabs:
            service.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={'requests': [{'addSheet': {'properties': {'title': TAB_NAME}}}]}
            ).execute()
            col_letter = chr(64 + len(HEADERS))
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"'{TAB_NAME}'!A1:{col_letter}1",
                valueInputOption='RAW',
                body={'values': [HEADERS]}
            ).execute()
    except Exception:
        pass


def write_request(data):
    service = get_sheets_service()
    if not service:
        raise RuntimeError('Sheet service not configured')
    _ensure_tab()

    request_id = 'TR-' + str(uuid.uuid4())[:8].upper()
    now = time.strftime('%Y-%m-%dT%H:%M:%S')

    # Determine first approval step
    band = data.get('band', 'E1')
    travel_type = data.get('travel_type', 'Domestic')
    if travel_type == 'International':
        step = 'Pending Manager + Dept Head Approval'
    else:
        step = 'Pending Manager Approval'

    row = [
        request_id,
        data['name'],
        data['email'],
        band,
        travel_type,
        data.get('destination_city', ''),
        data.get('destination_country', ''),
        data.get('start_date', ''),
        data.get('end_date', ''),
        data.get('purpose', ''),
        data.get('meetings', ''),
        'Submitted',
        step,
        data.get('manager_email', ''),
        data.get('budget_estimate', ''),
        now,
        now,
        '',  # approved_by
        '',  # rejected_reason
        '',  # admin_notes
    ]

    col_letter = chr(64 + len(HEADERS))
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=f"'{TAB_NAME}'!A:{col_letter}",
        valueInputOption='RAW',
        insertDataOption='INSERT_ROWS',
        body={'values': [row]}
    ).execute()

    return request_id


def get_requests_for_email(email):
    service = get_sheets_service()
    if not service:
        return []
    try:
        col_letter = chr(64 + len(HEADERS))
        result = service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"'{TAB_NAME}'!A2:{col_letter}"
        ).execute()
        rows = result.get('values', [])
        requests = []
        for row in rows:
            # Pad row to full length
            while len(row) < len(HEADERS):
                row.append('')
            if row[2].strip().lower() == email.lower():
                requests.append(dict(zip(
                    ['id', 'name', 'email', 'band', 'travel_type',
                     'destination_city', 'destination_country',
                     'start_date', 'end_date', 'purpose', 'meetings',
                     'status', 'current_step', 'manager_email',
                     'budget_estimate', 'created_at', 'updated_at',
                     'approved_by', 'rejected_reason', 'admin_notes'],
                    row
                )))
        return requests
    except Exception:
        return []


def notify_slack(data, request_id):
    try:
        from slack_sdk import WebClient
        token = os.environ.get('SLACK_USER_TOKEN', '')
        user_id = os.environ.get('SLACK_USER_ID', '')
        if not token or not user_id:
            return
        client = WebClient(token=token)
        dest = data.get('destination_city', '')
        if data.get('destination_country'):
            dest += f", {data['destination_country']}"
        msg = (
            f"*New Travel Request* ({request_id})\n"
            f"From: {data['name']} ({data['email']}) | Band {data.get('band', '?')}\n"
            f"To: {dest} | {data.get('travel_type', 'Domestic')}\n"
            f"Dates: {data.get('start_date', '?')} to {data.get('end_date', '?')}\n"
            f"Purpose: {data.get('purpose', '-')}\n"
        )
        if data.get('budget_estimate'):
            msg += f"Est. Budget: INR {data['budget_estimate']}\n"
        review_url = os.environ.get('REVIEW_URL', 'http://localhost:8083')
        msg += f"\nReview at {review_url}"
        client.chat_postMessage(channel=user_id, text=msg)
    except Exception:
        pass


# ── Routes ──

@app.route('/')
def index():
    with open(os.path.join(BASE_DIR, 'travel_request.html'), 'r') as f:
        html = f.read()
    html = html.replace('{{GOOGLE_CLIENT_ID}}', GOOGLE_CLIENT_ID)
    return html


@app.route('/api/verify-token', methods=['POST'])
def verify_token():
    body = request.get_json()
    token = body.get('credential', '')
    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        email = idinfo.get('email', '')
        name = idinfo.get('name', '')
        if ALLOWED_DOMAIN and not email.endswith(f'@{ALLOWED_DOMAIN}'):
            return jsonify({'error': f'Only @{ALLOWED_DOMAIN} accounts allowed'}), 403
        return jsonify({'ok': True, 'email': email, 'name': name})
    except Exception as e:
        return jsonify({'error': str(e)}), 401


@app.route('/api/submit-request', methods=['POST'])
def submit_request_route():
    body = request.get_json()
    token = body.get('credential', '')

    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        email = idinfo.get('email', '')
        name = idinfo.get('name', '')
    except Exception:
        return jsonify({'error': 'Invalid token'}), 401

    if ALLOWED_DOMAIN and not email.endswith(f'@{ALLOWED_DOMAIN}'):
        return jsonify({'error': f'Only @{ALLOWED_DOMAIN} accounts allowed'}), 403

    if not _check_rate_limit(email):
        return jsonify({'error': 'Too many requests. Try again later.'}), 429

    data = {
        'name': name,
        'email': email,
        'band': body.get('band', ''),
        'travel_type': body.get('travel_type', 'Domestic'),
        'destination_city': body.get('destination_city', '').strip(),
        'destination_country': body.get('destination_country', '').strip(),
        'start_date': body.get('start_date', ''),
        'end_date': body.get('end_date', ''),
        'purpose': body.get('purpose', '').strip(),
        'meetings': body.get('meetings', '').strip(),
        'manager_email': body.get('manager_email', '').strip(),
        'budget_estimate': body.get('budget_estimate', ''),
    }

    if not data['destination_city'] or not data['start_date'] or not data['end_date'] or not data['purpose']:
        return jsonify({'error': 'Destination, dates, and purpose are required'}), 400

    try:
        request_id = write_request(data)
        t = threading.Thread(target=notify_slack, args=(data, request_id), daemon=True)
        t.start()
        return jsonify({'ok': True, 'requestId': request_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/my-requests', methods=['POST'])
def my_requests():
    body = request.get_json()
    token = body.get('credential', '')
    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
        email = idinfo.get('email', '')
    except Exception:
        return jsonify({'error': 'Invalid token'}), 401

    requests_list = get_requests_for_email(email)
    return jsonify(requests_list)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port, debug=True)
