import logging
import os
import socket
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from threading import Thread

import bcrypt
import pytz
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_apscheduler import APScheduler
from flask_jwt_extended import (JWTManager, create_access_token,
                                get_jwt_identity, jwt_required)
from flask_restx import Api, Resource, fields
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///adms.db'
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET')
app.config['SCHEDULER_API_ENABLED'] = True

# Pengaturan zona waktu Jakarta
JAKARTA_TZ = pytz.timezone('Asia/Jakarta')
app.config['TIMEZONE'] = JAKARTA_TZ

db = SQLAlchemy(app)
jwt = JWTManager(app)
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# Setup logging
handler = RotatingFileHandler('app.log', maxBytes=10000, backupCount=1)
handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

# Models
class IClockMachine(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    serial_number = db.Column(db.String(80), unique=True, nullable=False)
    last_seen = db.Column(db.DateTime)
    timezone = db.Column(db.Integer, nullable=False)

class IClockUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pin = db.Column(db.Integer, unique=True, nullable=False)
    name = db.Column(db.String(80))
    primary = db.Column(db.String(80))
    password = db.Column(db.String(80))
    card = db.Column(db.String(80))
    group = db.Column(db.String(80))
    timezone = db.Column(db.String(80))
    verify = db.Column(db.String(80))
    vice_card = db.Column(db.String(80))
    iclock_machine_id = db.Column(db.Integer, db.ForeignKey('i_clock_machine.id'))

class IClockFingerprint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pin = db.Column(db.Integer, nullable=False)
    fid = db.Column(db.Integer, nullable=False)
    size = db.Column(db.Integer)
    valid = db.Column(db.String(10))
    template = db.Column(db.Text)
    iclock_machine_id = db.Column(db.Integer, db.ForeignKey('i_clock_machine.id'))

class IClockAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    pin = db.Column(db.Integer, nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(10))
    verify = db.Column(db.String(10))
    work_code = db.Column(db.String(20))
    reserved_1 = db.Column(db.String(20))
    reserved_2 = db.Column(db.String(20))
    iclock_machine_id = db.Column(db.Integer, db.ForeignKey('i_clock_machine.id'))

class AttendanceHook(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)

# Utility functions
def get_timezone_offset_string(timezone):
    offset = timedelta(hours=timezone)
    return f"{'+' if offset.total_seconds() >= 0 else '-'}{abs(offset).total_seconds() // 3600:02.0f}00"

def get_current_jakarta_time():
    return datetime.now(JAKARTA_TZ)

# Services
def handle_machine_heartbeat(serial_number):
    if not serial_number:
        app.logger.error("Serial number tidak diberikan")
        return None
    
    machine = IClockMachine.query.filter_by(serial_number=serial_number).first()
    if machine:
        machine.last_seen = get_current_jakarta_time()
        db.session.commit()
        app.logger.info(f"Heartbeat diterima dari mesin {serial_number}")
    else:
        new_machine = IClockMachine(
            serial_number=serial_number, 
            last_seen=get_current_jakarta_time(),
            timezone=int(os.getenv('DEFAULT_TZ', 7))  # Default ke UTC+7 untuk Jakarta
        )
        db.session.add(new_machine)
        db.session.commit()
        app.logger.info(f"Mesin baru {serial_number} ditambahkan")
        machine = new_machine
    return machine

def handle_user_received(serial_number, adms_user):
    user = IClockUser.query.filter_by(pin=adms_user['PIN']).first()
    if not user:
        user = IClockUser(pin=int(adms_user['PIN']))
        machine = IClockMachine.query.filter_by(serial_number=serial_number).first()
        if machine:
            user.iclock_machine_id = machine.id
        app.logger.info(f"Pengguna baru dibuat dengan PIN {adms_user['PIN']}")
    else:
        app.logger.info(f"Memperbarui pengguna dengan PIN {adms_user['PIN']}")
    
    user.name = adms_user['Name']
    user.primary = adms_user['Pri']
    user.password = adms_user['Passwd']
    user.card = adms_user['Card']
    user.group = adms_user['Grp']
    user.timezone = adms_user['TZ']
    user.verify = adms_user['Verify']
    user.vice_card = adms_user['ViceCard']
    db.session.add(user)
    db.session.commit()

def handle_fingerprint_received(serial_number, adms_fingerprint):
    fingerprint = IClockFingerprint.query.filter_by(pin=adms_fingerprint['PIN'], fid=adms_fingerprint['FID']).first()
    if not fingerprint:
        fingerprint = IClockFingerprint(pin=int(adms_fingerprint['PIN']), fid=int(adms_fingerprint['FID']))
        machine = IClockMachine.query.filter_by(serial_number=serial_number).first()
        if machine:
            fingerprint.iclock_machine_id = machine.id
        app.logger.info(f"Sidik jari baru dibuat untuk PIN {adms_fingerprint['PIN']}, FID {adms_fingerprint['FID']}")
    else:
        app.logger.info(f"Memperbarui sidik jari untuk PIN {adms_fingerprint['PIN']}, FID {adms_fingerprint['FID']}")
    
    fingerprint.size = int(adms_fingerprint['Size'])
    fingerprint.valid = adms_fingerprint['Valid']
    fingerprint.template = adms_fingerprint['TMP']
    db.session.add(fingerprint)
    db.session.commit()

def handle_attendance_received(serial_number, adms_attendance, machine):
    iclock_machine = IClockMachine.query.filter_by(serial_number=serial_number).first()
    attendance_records = []
    for att in adms_attendance:
        attendance_date = datetime.strptime(att['date'] + get_timezone_offset_string(machine.timezone), "%Y-%m-%d %H:%M:%S%z")
        jakarta_date = attendance_date.astimezone(JAKARTA_TZ)
        attendance = IClockAttendance(
            pin=int(att['pin']),
            date=jakarta_date,
            status=att['status'],
            verify=att['verify'],
            work_code=att['workCode'],
            reserved_1=att['reserved1'],
            reserved_2=att['reserved2'],
            iclock_machine_id=iclock_machine.id if iclock_machine else None
        )
        attendance_records.append(attendance)
    
    db.session.bulk_save_objects(attendance_records)
    db.session.commit()
    app.logger.info(f"{len(attendance_records)} catatan kehadiran diterima dari mesin {serial_number}")

    # Mengirim data pin ke semua hook yang aktif
    active_hooks = get_active_hooks()
    for hook in active_hooks:
        try:
            response = requests.post(hook.url, json=[
                {
                    'pin': att['pin'], 
                    'date': att['date'],
                    'mesin': serial_number  # Menambahkan serial number mesin
                } for att in adms_attendance
            ])
            if response.ok:
                app.logger.info(f"Data pin berhasil dikirim ke {hook.url}")
            else:
                app.logger.error(f"Gagal mengirim data pin ke {hook.url}: {response.status_code}")
        except requests.RequestException as error:
            app.logger.error(f"Error saat mengirim data pin ke {hook.url}: {str(error)}")

def get_active_hooks():
    return AttendanceHook.query.filter_by(is_active=True).all()

def create_hook(url):
    new_hook = AttendanceHook(url=url)
    db.session.add(new_hook)
    db.session.commit()
    return new_hook

def update_hook(hook_id, url, is_active):
    hook = AttendanceHook.query.get(hook_id)
    if hook:
        hook.url = url
        hook.is_active = is_active
        db.session.commit()
    return hook

def delete_hook(hook_id):
    hook = AttendanceHook.query.get(hook_id)
    if hook:
        db.session.delete(hook)
        db.session.commit()
        return True

# Routes
@app.route('/iclock/cdata', methods=['GET'])
def handshake():
    serial_number = request.args.get('SN')
    app.logger.info(f"Handshake dimulai untuk SN: {serial_number}")
    if not serial_number:
        app.logger.error("Handshake gagal: Serial number tidak diberikan")
        return "ERROR: Serial number tidak diberikan", 400
    
    app.logger.info(f"Handshake request diterima dari {serial_number}")
    machine = handle_machine_heartbeat(serial_number)
    if not machine:
        app.logger.error(f"Gagal memproses mesin dengan SN: {serial_number}")
        return "ERROR: Gagal memproses mesin", 500
    
    response = [
        f"GET OPTION FROM: {serial_number}",
        "STAMP=9999",
        f"ATTLOGSTAMP={int(get_current_jakarta_time().timestamp())}",
        f"OPERLOGStamp={int(get_current_jakarta_time().timestamp())}",
        f"ATTPHOTOStamp={int(get_current_jakarta_time().timestamp())}",
        "ErrorDelay=30",
        "Delay=10",
        "TransTimes=00:00;23:59",
        "TransInterval=1",
        "TransFlag=TransData AttLog\tOpLog\tEnrollUser\tChgUser\tEnrollFP\tChgFP\tFPImag",
        f"TimeZone={machine.timezone}",
        "Realtime=1",
        "Encrypt=None",
    ]
    app.logger.info(f"Handshake berhasil untuk SN: {serial_number}")
    app.logger.debug(f"Response: {response}")
    return "\r\n".join(response)

@app.route('/iclock/cdata', methods=['POST'])
def receive_data():
    serial_number = request.args.get('SN')
    table = request.args.get('table')
    body_lines = request.data.decode().strip().split("\n")

    data = {
        'serialNumber': serial_number,
        'table': table,
    }

    machine = handle_machine_heartbeat(serial_number)

    if table == 'ATTLOG':
        body_data = [line.split("\t") for line in body_lines]
        att_log = [{
            'pin': v[0],
            'date': v[1],
            'status': v[2],
            'verify': v[3],
            'workCode': v[4],
            'reserved1': v[5],
            'reserved2': v[6],
        } for v in body_data]
        handle_attendance_received(serial_number, att_log, machine)
        data['data'] = att_log
    elif table == 'OPERLOG':
        operations = []
        for line in body_lines:
            operation, *rest = line.split(' ')
            line_data = ' '.join(rest).split("\t")
            if operation == 'OPLOG':
                operations.append({
                    'operation': operation,
                    'data': {
                        'type': line_data[0],
                        'status': line_data[1],
                        'date': line_data[2],
                        'pin': line_data[3],
                        'value1': line_data[4],
                        'value2': line_data[5],
                        'value3': line_data[6],
                    }
                })
            elif operation == 'USER':
                user_data = {}
                for item in line_data:
                    key, value = item.split('=')
                    user_data[key] = value
                handle_user_received(serial_number, user_data)
                operations.append({
                    'operation': operation,
                    'data': user_data
                })
            elif operation == 'FP':
                fp_data = {}
                for item in line_data:
                    key, *value = item.split('=')
                    fp_data[key] = '='.join(value)
                handle_fingerprint_received(serial_number, fp_data)
                operations.append({
                    'operation': operation,
                    'data': fp_data
                })
            else:
                operations.append({
                    'operation': operation,
                    'data': line_data
                })
        data['data'] = operations
    else:
        data['data'] = body_lines

    app.logger.info(f"Machine Event: {data}")
    return f"OK: {len(body_lines)}"

@app.route('/iclock/getrequest', methods=['GET'])
def send_data():
    serial_number = request.args.get('SN')
    app.logger.info(f"HEARTBEAT: {request.args}")
    handle_machine_heartbeat(serial_number)
    return "OK"

@app.route('/iclock/devicecmd', methods=['POST'])
def status_data():
    app.logger.info(f"Command Response: {request.args}")
    return "OK"

@app.route('/api/hooks', methods=['GET'])
def get_hooks():
    hooks = AttendanceHook.query.all()
    return jsonify([{'id': h.id, 'url': h.url, 'is_active': h.is_active} for h in hooks])

@app.route('/api/hooks', methods=['POST'])
def add_hook():
    data = request.json
    new_hook = create_hook(data['url'])
    return jsonify({'id': new_hook.id, 'url': new_hook.url, 'is_active': new_hook.is_active}), 201

@app.route('/api/hooks/<int:hook_id>', methods=['PUT'])
def update_hook_route(hook_id):
    data = request.json
    updated_hook = update_hook(hook_id, data['url'], data['is_active'])
    if updated_hook:
        return jsonify({'id': updated_hook.id, 'url': updated_hook.url, 'is_active': updated_hook.is_active})
    return jsonify({'error': 'Hook not found'}), 404

@app.route('/api/hooks/<int:hook_id>', methods=['DELETE'])
def delete_hook_route(hook_id):
    if delete_hook(hook_id):
        return '', 204
    return jsonify({'error': 'Hook not found'}), 404

@app.route('/webhooks')
def webhooks_page():
    hooks = AttendanceHook.query.all()
    return render_template('webhooks.html', hooks=hooks)

def init_db():
    with app.app_context():
        db.create_all()

def attempt_connection(max_retries=5, delay=5):
    for attempt in range(max_retries):
        try:
            # Kode untuk melakukan koneksi
            return True  # Jika berhasil
        except ConnectionError:
            app.logger.warning(f"Koneksi gagal, mencoba lagi dalam {delay} detik...")
            time.sleep(delay)
    return False

def handle_connection(client_socket):
    try:
        # Baca byte pertama untuk menentukan jenis koneksi
        first_bytes = client_socket.recv(1, socket.MSG_PEEK)
        if not first_bytes:
            return
            
        # Jika byte pertama adalah 22 (0x16), ini adalah handshake SSL
        if first_bytes[0] == 0x16:
            app.logger.info("Koneksi SSL terdeteksi - mengabaikan")
            return
            
        while True:
            data = client_socket.recv(1024)
            if not data:
                break
            # Hanya proses data non-SSL
            try:
                processed_data = data.decode('utf-8').upper()
                client_socket.send(processed_data.encode('utf-8'))
            except UnicodeDecodeError:
                app.logger.warning("Menerima data non-UTF8, mengabaikan")
                break
    except Exception as e:
        app.logger.error(f"Error dalam handle_connection: {str(e)}")
    finally:
        client_socket.close()

def start_server():
    app.logger.info("Server dimulai")
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.bind(('0.0.0.0', 8082))
        server_socket.listen(5)
        app.logger.info("Server mendengarkan di 0.0.0.0:8082")
        
        while True:
            try:
                client_socket, addr = server_socket.accept()
                app.logger.info(f"Koneksi diterima dari {addr}")
                client_thread = Thread(target=handle_connection, args=(client_socket,))
                client_thread.daemon = True
                client_thread.start()
            except Exception as e:
                app.logger.error(f"Error saat menerima koneksi: {str(e)}")
    except Exception as e:
        app.logger.error(f"Error saat memulai server: {str(e)}")
    finally:
        server_socket.close()

def send_all_data_to_webhooks():
    with app.app_context():
        app.logger.info("Mengirim semua data ke webhooks")
        active_hooks = get_active_hooks()
        if not active_hooks:
            app.logger.info("Tidak ada webhook aktif")
            return

        # Mengambil semua data kehadiran dari database dengan informasi mesin
        all_attendance = db.session.query(
            IClockAttendance, 
            IClockMachine.serial_number
        ).join(
            IClockMachine, 
            IClockAttendance.iclock_machine_id == IClockMachine.id
        ).all()

        data_to_send = [{
            'pin': att.pin, 
            'date': att.date.isoformat(),
            'machine_sn': machine_sn  # Menambahkan serial number mesin
        } for att, machine_sn in all_attendance]

        for hook in active_hooks:
            try:
                response = requests.post(hook.url, json=data_to_send)
                if response.ok:
                    app.logger.info(f"Semua data berhasil dikirim ke {hook.url}")
                else:
                    app.logger.error(f"Gagal mengirim data ke {hook.url}: {response.status_code}")
            except requests.RequestException as error:
                app.logger.error(f"Error saat mengirim data ke {hook.url}: {str(error)}")

if __name__ == '__main__':
    init_db()
    app.logger.info("Database diinisialisasi")
    with app.app_context():
        send_all_data_to_webhooks()
    
    # Jalankan socket server di thread terpisah
    server_thread = Thread(target=start_server)
    server_thread.daemon = True
    server_thread.start()
    
    # Jalankan Flask app
    app.run(debug=True, use_reloader=False, host='0.0.0.0', port=5555)
