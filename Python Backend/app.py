from flask import Flask, request, jsonify
from datetime import datetime, date
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pydantic import BaseModel, ValidationError
from typing import Optional, Dict
import httpx
import uuid
import os
from dotenv import load_dotenv
import json

# ---------------- load config ----------------
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "mysql+pymysql://root:@localhost:3306/sikes")
FLOWISE_PREDICTION_URL = os.getenv("FLOWISE_PREDICTION_URL",
                                   "http://localhost:3000/api/v1/prediction/9171ef91-ea8d-4236-92a1-af4238c14320")
WAHA_SEND_URL = os.getenv("WAHA_SEND_URL", "http://localhost:3001/api/sendText")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "e10ca481e1914c0da60e23ae7a2f4693")
FLOWISE_TIMEOUT = float(os.getenv("FLOWISE_TIMEOUT", "60"))

# ---------------- db setup ----------------
engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# ---------------- models ----------------
class User(Base):
    __tablename__ = "users"
    id = sa.Column(sa.Integer, primary_key=True)
    phone = sa.Column(sa.String(30), unique=True, index=True, nullable=False)
    name = sa.Column(sa.String(200), nullable=True)
    bpjs_number = sa.Column(sa.String(100), nullable=True)
    fktp_id = sa.Column(sa.Integer, nullable=True)
    created_at = sa.Column(sa.DateTime, default=datetime.utcnow)

class FKTP(Base):
    __tablename__ = "fktps"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(200))
    Alamat = sa.Column(sa.String(200))
    phone = sa.Column(sa.String(30), unique=True, index=True, nullable=False)
    created_at = sa.Column(sa.DateTime, default=datetime.utcnow)

class RequestFKTP(Base):
    __tablename__ = "requests"
    id = sa.Column(sa.Integer, primary_key=True)
    request_id = sa.Column(sa.String(64), unique=True, index=True, nullable=False)
    user_id = sa.Column(sa.Integer, nullable=True)
    fktp_id = sa.Column(sa.Integer, nullable=True)
    patient_phone = sa.Column(sa.String(30), nullable=False)
    bpjs_number = sa.Column(sa.String(100), nullable=True)
    message = sa.Column(sa.Text, nullable=True)
    status = sa.Column(sa.String(20), default="pending")
    raw_reply = sa.Column(sa.Text, nullable=True)
    formatted_reply = sa.Column(sa.Text, nullable=True)
    created_at = sa.Column(sa.DateTime, default=datetime.utcnow)
    updated_at = sa.Column(sa.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class MessageLog(Base):
    __tablename__ = "messages"
    id = sa.Column(sa.Integer, primary_key=True)
    request_id = sa.Column(sa.String(64), index=True, nullable=True)
    sender = sa.Column(sa.String(30))
    phone = sa.Column(sa.String(30))
    message = sa.Column(sa.Text)
    created_at = sa.Column(sa.DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)
app = Flask(__name__)


def get_db():
    return SessionLocal()

def send_to_waha(chat_id: str, text: str):
    """
    WAHA sendText with API KEY support
    """
    headers = {
        "x-api-key": WAHA_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "session": "default",
        "chatId": chat_id,
        "text": text,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(WAHA_SEND_URL, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print("[WAHA SEND ERROR]", e)
        return None
    
def call_flowise_predict(flowise_url, session_id, question, customVariables=None):
    payload = {
        "question": question,
        "overrideConfig": {
            "sessionId": session_id,
            "customVariables": customVariables or {}
        }
    }

    with httpx.Client(timeout=FLOWISE_TIMEOUT) as client:
        r = client.post(flowise_url, json=payload)
        r.raise_for_status()
        return r.json()

# ---------------- Pydantic schemas ----------------
class RegisterUserReq(BaseModel):
    phone: str
    name: Optional[str] = None
    bpjs_number: Optional[str] = None
    fktp_id: Optional[int] = None

class NotifyFktpReq(BaseModel):
    user_id: Optional[int]
    patient_phone: str
    bpjs_number: Optional[str]
    fktp_id: int
    message: str

class SendPatientReq(BaseModel):
    patient_phone: str
    message: str

class TriggerFlowiseReq(BaseModel):
    flowise_url: str
    session_id: str
    question: str
    customVariables: Optional[Dict] = {}

class StoreFktpReplyReq(BaseModel):
    request_id: str
    raw_reply: str
    formatted_reply: Optional[str] = None

# ---------------------------------------------------
# TOOL ENDPOINTS 
# ---------------------------------------------------
@app.get("/check_role")
def check_role():
    phone = request.args.get("phone")
    phone = phone.split('_')[0]
    if not phone.endswith("@lid"):
        phone = phone + "@lid"
    db = get_db()

    f = db.query(FKTP).filter(FKTP.phone == phone).first()
    if f:
        print("fktp")
        return jsonify({"role": "fktp", "fktp_id": f.id, "fktp_name": f.name})
    u = db.query(User).filter(User.phone == phone).first()
    if u:
        print("user")
        return {"role": "patient", "user_id": u.id}
    print("user")
    return jsonify({"role": "patient"})

@app.get("/check_user")
def check_user():
    phone = request.args.get("phone")
    phone = phone.split('_')[0]
    if not phone.endswith("@lid"):
        phone = phone + "@lid"
    db = get_db()
    u = db.query(User).filter(User.phone == phone).first()

    if not u:
        return jsonify({"registered": False})

    return jsonify({
        "registered": True,
        "user_id": u.id,
        "name": u.name,
        "bpjs_number": u.bpjs_number,
        "fktp_id": u.fktp_id
    })

@app.post("/register_user")
def register_user():
    try:
        req = RegisterUserReq(**request.json)
    except ValidationError as e:
        return jsonify(e.errors()), 400

    db = get_db()
    
    existing = db.query(User).filter(User.phone == req.phone.split('_')[0]).first()
    if existing:
        return jsonify({"status": "already_registered", "user_id": existing.id})
    
    # fktp = db.query(FKTP).filter(FKTP.id == req.fktp_idfirst()
    # fktp_id = fktp.id if fktp else None

    user = User(
        phone=req.phone.split('_')[0],
        name=req.name,
        bpjs_number=req.bpjs_number,
        fktp_id=req.fktp_id
    )
    db.add(user)
    db.commit()

    return jsonify({"status": "success", "user_id": user.id})

@app.post("/notify_fktp")
def notify_fktp():
    try:
        req = NotifyFktpReq(**request.json)
    except ValidationError as e:
        raw = request.get_json(force=True)

        # DEBUG LOG — print semua yang dikirim Flowise
        print("\n=== /notify_fktp RAW BODY ===")
        print(json.dumps(raw, indent=4))

        return jsonify(e.errors()), 400

    db = get_db()
    rid = "req_" + uuid.uuid4().hex[:16]

    row = RequestFKTP(
        request_id=rid,
        user_id=req.user_id,
        fktp_id=req.fktp_id,
        patient_phone=req.patient_phone,
        bpjs_number=req.bpjs_number,
        message=req.message
    )

    db.add(row)
    db.commit()

    # Log
    db.add(MessageLog(request_id=rid, sender="system", phone=None, message=f"notify_fktp:{req.message}"))
    db.commit()

    # Send WAHA notif to FKTP
    f = db.query(FKTP).filter(FKTP.id == req.fktp_id).first()
    if not f:
        return jsonify({"status": "failed", "reason": "fktp_not_found"})

    body = (
        f"[REQUEST_ID:{rid}]\n"
        f"Permintaan konsultasi pasien\n"
        f"BPJS: {req.bpjs_number or '-'}\n"
        f"Pesan: {req.message}"
    )

    send_to_waha(f.phone, body)

    return jsonify({"status": "sent", "request_id": rid})

@app.get("/get_fktp_reply")
def get_fktp_reply():
    request_id = request.args.get("request_id")
    db = get_db()
    r = db.query(RequestFKTP).filter(RequestFKTP.request_id == request_id).first()

    if not r:
        return jsonify({"status": "not_found"})

    if r.status == "pending":
        return jsonify({"status": "pending"})

    return jsonify({"status": "replied", "raw_reply": r.raw_reply})

@app.post("/store_fktp_reply")
def store_fktp_reply():
    try:
        reqs = StoreFktpReplyReq(**request.json)
    except ValidationError as e:
        return jsonify(e.errors()), 400

    db = get_db()
    req = db.query(RequestFKTP).filter(RequestFKTP.request_id == reqs.request_id).first()
    if not req:
        return {"status": "not_found"}
    req.raw_reply = reqs.raw_reply
    if reqs.formatted_reply:
        req.formatted_reply = reqs.formatted_reply
    req.status = "replied"
    req.updated_at = datetime.utcnow()
    db.add(req)
    db.commit()

    phone = req.patient_phone
    if not phone.endswith("@lid"):
        phone = phone + "@lid"

    return {"status": "stored", "patient_phone": phone}

@app.post("/send_to_patient")
def send_to_patient():
    try:
        req = SendPatientReq(**request.json)
    except ValidationError as e:
        return jsonify(e.errors()), 400

    send_to_waha(req.patient_phone, req.message)

    db = get_db()
    db.add(MessageLog(request_id=None, sender="system", phone=req.patient_phone,
                      message=f"send_to_patient:{req.message}"))
    db.commit()

    return jsonify({"status": "sent"})



@app.get("/db_user_by_phone")
def db_user_by_phone():
    phone = request.args.get("phone")
    phone = phone.split('_')[0]

    db = get_db()
    u = db.query(User).filter(User.phone == phone).first()

    if not u:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "user_id": u.id,
        "phone": u.phone,
        "name": u.name,
        "bpjs_number": u.bpjs_number,
        "fktp_id": u.fktp_id
    })


@app.get("/db_fktp_by_id")
def db_fktp_by_id():
    fktp_id = request.args.get("fktp_id")

    db = get_db()
    f = db.query(FKTP).filter(FKTP.id == fktp_id).first()

    if not f:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "id": f.id,
        "name": f.name,
        "alamat": f.Alamat,
        "phone": f.phone
    })


@app.get("/db_fktp_by_name")
def db_fktp_by_name():
    name = request.args.get("name")
    if not name:
        return jsonify({"exists": False})

    db = get_db()
    f = db.query(FKTP).filter(FKTP.name.ilike(f"%{name}%")).first()

    if not f:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "id": f.id,
        "name": f.name,
        "alamat": f.Alamat,
        "phone": f.phone
    })


@app.get("/db_list_fktp")
def db_list_fktp():
    db = get_db()
    rows = db.query(FKTP).all()

    data = []
    for f in rows:
        data.append({
            "id": f.id,
            "name": f.name,
            "alamat": f.Alamat,
            "phone": f.phone
        })

    return jsonify({"fktp": data})


@app.get("/db_request_by_id")
def db_request_by_id():
    request_id = request.args.get("request_id")

    db = get_db()
    r = db.query(RequestFKTP).filter(RequestFKTP.request_id == request_id).first()

    if not r:
        return jsonify({"exists": False})

    return jsonify({
        "exists": True,
        "request_id": r.request_id,
        "user_id": r.user_id,
        "patient_phone": r.patient_phone,
        "fktp_id": r.fktp_id,
        "bpjs_number": r.bpjs_number,
        "message": r.message,
        "status": r.status,
        "raw_reply": r.raw_reply,
        "formatted_reply": r.formatted_reply
    })

# ---------------------------------------------------
# WAHA WEBHOOK 
# ---------------------------------------------------
@app.post("/bot")
def webhook_waha():
    data = request.get_json(force=True)
    print("\n Incoming WAHA webhook") #debugging
    # print(data)

    if data.get("event") != "message":
        return "OK"

    payload = data["payload"]
    json_payload = json.dumps(payload)
    with open("last_waha_payload.json", "w") as f:
        f.write(json_payload)


    phone = payload["from"]
    print(f" From: {phone}")
    text = payload["body"]
    session_id = f"{phone}_{date.today().isoformat()}"

    try:
        result = call_flowise_predict(
            FLOWISE_PREDICTION_URL,
            session_id,
            text,
            {"user_phone": phone, "raw_message": text}
        )
    except Exception as e:
        print("[Flowise error]", e)
        send_to_waha(phone, "⚠ Sistem sedang sibuk, silakan coba lagi nanti.")
        return jsonify({"status": "flowise_error"})

    reply_text = (
        result.get("text")
        or result.get("answer")
        or result.get("output_text")
        or "Pesan telah diteruskan."
    )

    send_to_waha(phone, reply_text)
    return "OK"


# ---------------------------------------------------
# HEALTH ENDPOINT
# ---------------------------------------------------
@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "db": DATABASE_URL,
        "flowise": FLOWISE_PREDICTION_URL,
        "waha": WAHA_SEND_URL
    })


# ---------------------------------------------------
# RUN SERVER
# ---------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
