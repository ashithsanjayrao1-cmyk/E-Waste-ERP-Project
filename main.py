from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from datetime import datetime, date
from pydantic import BaseModel
from database import engine, get_db
from typing import Optional
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, StreamingResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.colors import HexColor
import models
import hashlib
import qrcode
import base64
import os
import io
import csv
from io import StringIO
from passlib.context import CryptContext
from jose import jwt, JWTError
from sqlalchemy import Column, Integer, String
from PIL import Image
from reportlab.lib.utils import ImageReader

class StaffPassword(models.Base):
    __tablename__ = "staff_passwords"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, index=True)
    plain_password = Column(String(255))

def create_qr_base64(data_string: str):
    qr = qrcode.QRCode(version=1, box_size=4, border=1)
    qr.add_data(data_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")

models.Base.metadata.create_all(bind = engine)
app = FastAPI(title="E-Waste ERP - Complete System")

if not os.path.exists("static"):
    os.makedirs("static")
    
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory = "templates")

SECRET_KEY = "ewaste_super_secret_master_key"
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# BEARER TOKEN SECURITY DEPENDENCY
security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security), db: Session = Depends(get_db)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid authentication token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate authentication credentials")
    
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def get_password_hash(password): return pwd_context.hash(password)
def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)

@app.on_event("startup")
def seed_users():
    db = next(get_db())
    defaults = [
        ("admin", "admin123", "admin"),
        ("worker", "worker123", "worker"),
        ("driver", "driver123", "driver")
    ]
    for uname, pword, role in defaults:
        user = db.query(models.User).filter(models.User.username == uname).first()
        if not user:
            db.add(models.User(username=uname, hashed_password=get_password_hash(pword), role=role))
            db.add(StaffPassword(username=uname, plain_password=pword))
            db.commit()
        else:
            pw_record = db.query(StaffPassword).filter(StaffPassword.username == uname).first()
            if not pw_record:
                db.add(StaffPassword(username=uname, plain_password=pword))
                db.commit()

class LoginRequest(BaseModel): username: str; password: str
class UserCreate(BaseModel): username: str; password: str; role: str

@app.post("/api/login")
def login_user(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.username == req.username).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid Credentials")
    token = jwt.encode({"sub": user.username, "role": user.role}, SECRET_KEY, algorithm=ALGORITHM)
    buyer_id = None
    if user.role == "buyer":
        buyer_record = db.query(models.Buyer).filter(models.Buyer.username == user.username).first()
        if buyer_record: buyer_id = buyer_record.id
    return {"access_token": token, "role": user.role, "buyer_id": buyer_id}

@app.post("/api/register-employee")
def register_employee(req: UserCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    existing = db.query(models.User).filter(models.User.username == req.username).first()
    if existing: raise HTTPException(status_code=400, detail="Username already exists!")
    db.add(models.User(username=req.username, hashed_password=get_password_hash(req.password), role=req.role))
    db.add(StaffPassword(username=req.username, plain_password=req.password))
    db.commit()
    return {"message": "Account created successfully!"}

@app.get("/api/users/")
def get_users(role: Optional[str] = None, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    query = db.query(models.User)
    if role: query = query.filter(models.User.role == role)
    users = query.all()
    res = []
    for u in users:
        pw_record = db.query(StaffPassword).filter(StaffPassword.username == u.username).first()
        pw = pw_record.plain_password if pw_record else "******"
        res.append({"id": u.id, "username": u.username, "role": u.role, "password": pw})
    return {"data": res}

class ClientCreate(BaseModel): client_type: str; name: str; gst_number: str; address: str
class PickupTaskCreate(BaseModel): client_id: int; expected_weight_kg: float; pickup_date: date; driver_name: str
class InboundWasteCreate(BaseModel): task_id: int; actual_arrived_weight: float; discrepancy_note: Optional[str] = None

@app.post("/clients/")
def create_client(client: ClientCreate , db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db.add(models.Client(client_type=client.client_type, name=client.name, gst_number=client.gst_number, address=client.address))
    db.commit()
    return {"message": "Client created"}

@app.get("/api/clients/")
def get_all_clients(db :Session = Depends(get_db), current_user: models.User = Depends(get_current_user)): return {"data": db.query(models.Client).all()}

@app.post("/pickups/")
def create_pickup(pickup: PickupTaskCreate, db:Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    if pickup.pickup_date < date.today():
        raise HTTPException(status_code=400, detail="Cannot schedule a truck dispatch in the past!")
    db.add(models.PickupTask(client_id=pickup.client_id, expected_weight_kg=pickup.expected_weight_kg, pickup_date=pickup.pickup_date, driver_name=pickup.driver_name))
    db.commit()
    return {"message": "Pickup Task Assigned"}

@app.get("/api/pickups/")
def get_all_pickups(db : Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    pickups = db.query(models.PickupTask).all()
    res = []
    for p in pickups:
        waste = db.query(models.InboundWaste).filter(models.InboundWaste.task_id == p.id).first()
        actual_w = waste.actual_arrived_weight if waste else None
        res.append({"id": p.id, "client_id": p.client_id, "driver_name": p.driver_name, "expected_weight_kg": p.expected_weight_kg, "status": p.status, "actual_arrived_weight": actual_w})
    return {"data": res}

class DriverStatusUpdate(BaseModel): task_id: int; new_status: str

@app.post("/api/driver/update-status")
def driver_update_status(req: DriverStatusUpdate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    task = db.query(models.PickupTask).filter(models.PickupTask.id == req.task_id).first()
    task.status = req.new_status
    db.commit()
    return {"status": "success"}

@app.post("/inbound-waste/")
def log_inbound_waste(waste: InboundWasteCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_task = db.query(models.PickupTask).filter(models.PickupTask.id == waste.task_id).first()
    existing_waste = db.query(models.InboundWaste).filter(models.InboundWaste.task_id == waste.task_id).first()
    if existing_waste:
        existing_waste.actual_arrived_weight = waste.actual_arrived_weight
        db.commit()
        return {"message": "Waste weight corrected successfully!"}
    db.add(models.InboundWaste(task_id=waste.task_id, actual_arrived_weight=waste.actual_arrived_weight, discrepancy_note=waste.discrepancy_note))
    db_task.status = "DELIVERED"
    db.commit()
    return {"message": "Waste logged and marked Delivered!"}

@app.get("/api/inventory/")
def get_inventory(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    result = []
    for w in db.query(models.InboundWaste).all():
        client_name = "Unknown Client"
        task = db.query(models.PickupTask).filter(models.PickupTask.id == w.task_id).first()
        if task:
            client = db.query(models.Client).filter(models.Client.id == task.client_id).first()
            if client: client_name = client.name
            
        jobs = db.query(models.JobCard).filter(models.JobCard.batch_id == w.batch_id).all()
        job_ids = [j.id for j in jobs]
        extracted_materials = db.query(models.ExtractedMaterial).filter(models.ExtractedMaterial.job_id.in_(job_ids)).all() if job_ids else []
        
        extracted_sum = sum([m.yield_weight_kg for m in extracted_materials])
        remaining_weight = max(0.0, w.actual_arrived_weight - extracted_sum)
        
        qr_text = f"E-WASTE SYSTEM RECORD\nBatch ID: #{w.batch_id}\nSource Client: {client_name}\nArrived Weight: {w.actual_arrived_weight} kg"
        qr_base64 = create_qr_base64(qr_text)
        
        result.append({
            "batch_id": w.batch_id, "task_id": w.task_id, "client_name": client_name, 
            "actual_arrived_weight": w.actual_arrived_weight, "remaining_weight": remaining_weight, 
            "status": w.status, "qr_code": qr_base64
        })
    return {"data": result}

@app.post("/api/inventory/{batch_id}/close")
def close_batch(batch_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_waste = db.query(models.InboundWaste).filter(models.InboundWaste.batch_id == batch_id).first()
    if db_waste:
        db_waste.status = "COMPLETED"
        db.commit()
    return {"message": "Batch marked as fully dismantled!"}

class JobCardCreate(BaseModel): batch_id: int; worker_name: str; instructions: str
class ExtractedMaterialCreate(BaseModel): job_id: int; material_type: str; yield_weight_kg: float

@app.post("/job-cards/")
def create_job_card(job: JobCardCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_waste = db.query(models.InboundWaste).filter(models.InboundWaste.batch_id == job.batch_id).first()
    db.add(models.JobCard(batch_id=job.batch_id, worker_name=job.worker_name, instructions=job.instructions))
    db_waste.status = "BEING_DISMANTLED"
    db.commit()
    return {"message": "Job Card assigned"}

@app.get("/api/jobs/")
def get_job_cards(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    jobs = db.query(models.JobCard).all()
    res = []
    for j in jobs:
        client_name = "Unknown Client"
        initial_weight = 0.0
        waste = db.query(models.InboundWaste).filter(models.InboundWaste.batch_id == j.batch_id).first()
        if waste:
            initial_weight = waste.actual_arrived_weight
            task = db.query(models.PickupTask).filter(models.PickupTask.id == waste.task_id).first()
            if task:
                client = db.query(models.Client).filter(models.Client.id == task.client_id).first()
                if client: client_name = client.name
                
        all_jobs_for_batch = db.query(models.JobCard).filter(models.JobCard.batch_id == j.batch_id).all()
        job_ids = [jb.id for jb in all_jobs_for_batch]
        exts = db.query(models.ExtractedMaterial).filter(models.ExtractedMaterial.job_id.in_(job_ids)).all() if job_ids else []
        total_extracted = sum([e.yield_weight_kg for e in exts])
        remaining = max(0.0, initial_weight - total_extracted)
                
        res.append({
            "id": j.id, "batch_id": j.batch_id, "client_name": client_name, 
            "batch_initial_weight": initial_weight, "batch_remaining_weight": remaining,
            "worker_name": j.worker_name, "instructions": j.instructions, "status": j.status
        })
    return {"data": res}

@app.post("/api/jobs/{job_id}/close")
def close_job_card(job_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_job = db.query(models.JobCard).filter(models.JobCard.id == job_id).first()
    if db_job:
        db_job.status = "COMPLETED"
        db.commit()
    return {"message": "Your assignment is marked complete!"}

@app.post("/extracted-materials/")
def log_extracted_material(material: ExtractedMaterialCreate, db :Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    job = db.query(models.JobCard).filter(models.JobCard.id == material.job_id).first()
    waste = db.query(models.InboundWaste).filter(models.InboundWaste.batch_id == job.batch_id).first()
    
    all_jobs = db.query(models.JobCard).filter(models.JobCard.batch_id == job.batch_id).all()
    job_ids = [j.id for j in all_jobs]
    exts = db.query(models.ExtractedMaterial).filter(models.ExtractedMaterial.job_id.in_(job_ids)).all() if job_ids else []
    
    total_extracted = sum([e.yield_weight_kg for e in exts])
    remaining = waste.actual_arrived_weight - total_extracted
    
    if material.yield_weight_kg > remaining:
         raise HTTPException(status_code=400, detail=f"Exceeds Limits! Only {remaining:.2f}kg of physical waste remains in this batch.")
         
    db.add(models.ExtractedMaterial(job_id=material.job_id, material_type=material.material_type, yield_weight_kg=material.yield_weight_kg))
    db.commit()
    return {"message" : "Material logged successfully"}

@app.get("/api/extractions/")
def get_all_extractions(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    extractions = db.query(models.ExtractedMaterial).order_by(models.ExtractedMaterial.id.desc()).all()
    res = []
    for ext in extractions:
        job = db.query(models.JobCard).filter(models.JobCard.id == ext.job_id).first()
        if not job: continue
        
        client_name = "Unknown Client"
        waste = db.query(models.InboundWaste).filter(models.InboundWaste.batch_id == job.batch_id).first()
        if waste:
            task = db.query(models.PickupTask).filter(models.PickupTask.id == waste.task_id).first()
            if task:
                client = db.query(models.Client).filter(models.Client.id == task.client_id).first()
                if client: client_name = client.name
        res.append({"id": ext.id, "worker_name": job.worker_name, "batch_id": job.batch_id, "client_name": client_name, "material_type": ext.material_type, "yield_weight_kg": ext.yield_weight_kg})
    return {"data": res}

class PublicBuyerCreate(BaseModel): username: str; password: str; company_name: str; license_number: str; contact_email: str

@app.post("/api/register-public-buyer")
def register_public_buyer(req: PublicBuyerCreate, db: Session = Depends(get_db)):
    if db.query(models.User).filter(models.User.username == req.username).first():
        raise HTTPException(status_code=400, detail="Username taken")
    db.add(models.User(username=req.username, hashed_password=get_password_hash(req.password), role="buyer"))
    db.add(models.Buyer(username=req.username, company_name=req.company_name, license_number=req.license_number, contact_email=req.contact_email))
    db.commit()
    return {"message": "Buyer registered successfully!"}

@app.get("/api/live-inventory")
def get_live_inventory(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    materials = db.query(models.ExtractedMaterial).all()
    sales = db.query(models.SalesOrder).all()
    inv = {"Copper": 0, "Aluminum": 0, "Plastic": 0, "Glass": 0, "Lithium Batteries (Toxic)": 0, "Chemical Residue (Toxic)": 0}
    for m in materials:
        if m.material_type in inv: inv[m.material_type] += m.yield_weight_kg
    for s in sales:
        if s.status == "APPROVED" or s.status.startswith("DISPOSED:") or s.status.startswith("PENDING_DISP|") or s.status.startswith("EN_ROUTE_DISP|"):
            if s.material_type in inv: inv[s.material_type] -= s.quantity_sold_kg
    return {"data": [{"material": k, "available_kg": v} for k, v in inv.items()]}

class SalesOrderCreate(BaseModel): buyer_id: int; material_type: str; quantity_sold_kg: float; total_price: float

@app.post("/sales-order/")
def create_sales_order(order: SalesOrderCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    materials = db.query(models.ExtractedMaterial).filter(models.ExtractedMaterial.material_type == order.material_type).all()
    sales = db.query(models.SalesOrder).filter(models.SalesOrder.material_type == order.material_type).all()
    extracted = sum([m.yield_weight_kg for m in materials])
    deducted = sum([s.quantity_sold_kg for s in sales if s.status == "APPROVED" or "DISP" in s.status])
    available = extracted - deducted
    
    if order.quantity_sold_kg > available:
        raise HTTPException(status_code=400, detail=f"Insufficient Market Stock! Only {available:.2f}kg available for purchase.")
        
    db.add(models.SalesOrder(buyer_id=order.buyer_id, material_type=order.material_type, quantity_sold_kg=order.quantity_sold_kg, total_price=order.total_price, status="PENDING"))
    db.commit()
    return {"message": "Order Submitted"}

@app.post("/api/approve-order/{order_id}")
def approve_order(order_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    order = db.query(models.SalesOrder).filter(models.SalesOrder.id == order_id).first()
    if order.status != "PENDING":
        raise HTTPException(status_code=400, detail="Order already processed!")
        
    materials = db.query(models.ExtractedMaterial).filter(models.ExtractedMaterial.material_type == order.material_type).all()
    sales = db.query(models.SalesOrder).filter(models.SalesOrder.material_type == order.material_type).all()
    extracted = sum([m.yield_weight_kg for m in materials])
    deducted = sum([s.quantity_sold_kg for s in sales if s.status == "APPROVED" or "DISP" in s.status])
    available = extracted - deducted
    
    if order.quantity_sold_kg > available:
        raise HTTPException(status_code=400, detail=f"Approval Blocked! Only {available:.2f}kg left in stock.")
        
    order.status = "APPROVED"
    db.commit()
    return {"message": "Order Approved"}

@app.post("/api/reject-order/{order_id}")
def reject_order(order_id: int, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    order = db.query(models.SalesOrder).filter(models.SalesOrder.id == order_id).first()
    order.status = "REJECTED"
    db.commit()
    return {"message": "Order Rejected"}

class DisposalManifestCreate(BaseModel):
    material_type: str
    quantity_kg: float
    facility_name: str
    driver_name: str
    disposal_cost: float

@app.post("/api/disposal-manifest/")
def create_disposal_manifest(req: DisposalManifestCreate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    materials = db.query(models.ExtractedMaterial).filter(models.ExtractedMaterial.material_type == req.material_type).all()
    sales = db.query(models.SalesOrder).all()
    
    total_extracted = sum([m.yield_weight_kg for m in materials])
    total_deducted = sum([s.quantity_sold_kg for s in sales if s.material_type == req.material_type and (s.status == "APPROVED" or "DISP" in s.status)])
    available_stock = total_extracted - total_deducted
    
    if req.quantity_kg > available_stock:
        raise HTTPException(status_code=400, detail=f"Insufficient Stock! Only {available_stock:.2f}kg available for safe disposal.")
        
    total_revenue = sum([s.total_price for s in sales if s.status == "APPROVED"])
    total_costs = sum([s.total_price for s in sales if "DISP" in s.status])
    available_funds = total_revenue - total_costs
    
    if req.disposal_cost > available_funds:
        raise HTTPException(status_code=400, detail=f"Insufficient Agency Funds! You only have ₹{available_funds:.2f} in Approved Revenue to pay.")
        
    db.add(models.SalesOrder(buyer_id=0, material_type=req.material_type, quantity_sold_kg=req.quantity_kg, total_price=req.disposal_cost, status=f"PENDING_DISP|{req.driver_name}|{req.facility_name}"))
    db.commit()
    return {"message": "Disposal logged and assigned to driver!"}

class DriverDisposalUpdate(BaseModel): task_id: int; action: str 

@app.post("/api/driver/disposal-update")
def driver_disposal_update(req: DriverDisposalUpdate, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    task = db.query(models.SalesOrder).filter(models.SalesOrder.id == req.task_id).first()
    if "|" in task.status:
        parts = task.status.split("|")
        driver = parts[1]
        facility = parts[2]
    else:
        return {"status": "Already processed"}
        
    if req.action == "START":
        task.status = f"EN_ROUTE_DISP|{driver}|{facility}"
    elif req.action == "DELIVER":
        task.status = f"DISPOSED:{facility} (By: {driver})"
    db.commit()
    return {"status": "success"}

@app.get("/api/driver/disposals/")
def get_driver_disposals(driver_name: str, db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    tasks = db.query(models.SalesOrder).filter(
        models.SalesOrder.status.like(f"%|{driver_name}|%") | 
        models.SalesOrder.status.like(f"%By: {driver_name})%")
    ).all()
    res = []
    for t in tasks:
        if "|" in t.status:
            parts = t.status.split("|")
            status_val = parts[0]
            facility = parts[2] if len(parts) > 2 else "Unknown"
        else:
            status_val = "DISPOSED"
            facility = t.status.split(":")[1].split(" (")[0] if ":" in t.status else "Unknown"
        res.append({"id": t.id, "material_type": t.material_type, "weight": t.quantity_sold_kg, "facility": facility, "status": status_val})
    return {"data": res}

@app.get("/api/sales/")
def get_sales(db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    sales = db.query(models.SalesOrder).all()
    data = []
    for s in sales:
        buyer = db.query(models.Buyer).filter(models.Buyer.id == s.buyer_id).first() if s.buyer_id > 0 else None
        status_str = s.status
        buyer_name = buyer.company_name if buyer else "Unknown"
        
        if s.status.startswith("DISPOSED:"):
            status_str = "SAFE DISPOSAL COMPLETED"
            buyer_name = s.status.split(":", 1)[1]
        elif s.status.startswith("PENDING_DISP|"):
            status_str = "PENDING DISPOSAL TRUCK"
            parts = s.status.split("|")
            buyer_name = f"{parts[2]} (Driver: {parts[1]})"
        elif s.status.startswith("EN_ROUTE_DISP|"):
            status_str = "TOXIC WASTE EN ROUTE"
            parts = s.status.split("|")
            buyer_name = f"{parts[2]} (Driver: {parts[1]})"
            
        data.append({"id": s.id, "buyer_id": s.buyer_id, "buyer_name": buyer_name, "material_type": s.material_type, "quantity_sold_kg": s.quantity_sold_kg, "total_price": s.total_price, "status": status_str})
    return {"data": data}

class ComplianceCertificateCreate(BaseModel): client_id : int; batch_id : int

@app.post("/compliance-certificates/")
def generate_certificate(cert : ComplianceCertificateCreate , db: Session = Depends(get_db), current_user: models.User = Depends(get_current_user)):
    db_client = db.query(models.Client).filter(models.Client.id == cert.client_id).first()
    if not db_client:
        raise HTTPException(status_code=400, detail="Invalid Client Selected.")
        
    jobs = db.query(models.JobCard).filter(models.JobCard.batch_id == cert.batch_id).all()
    workers = list(set([j.worker_name for j in jobs]))
    worker_name_str = ", ".join(workers) if workers else "Authorised Staff"
    secure_hash = hashlib.sha256(f"{cert.client_id}-{cert.batch_id}-{datetime.now()}".encode()).hexdigest()
    db.add(models.ComplianceCertificate(client_id=cert.client_id, batch_id=cert.batch_id, certificate_hash=secure_hash))
    db.commit()
    return{"hash": secure_hash, "client_name": db_client.name, "worker_name": worker_name_str}

@app.get("/api/export/sales")
def export_sales_csv(db: Session = Depends(get_db)):
    sales_records = db.query(models.SalesOrder).all() 
    
    output = StringIO()
    writer = csv.writer(output)
    
    writer.writerow(["========================================================================================"])
    writer.writerow(["E-WASTE MASTER ERP - OFFICIAL GOVERNMENT SALES & DISPOSAL AUDIT REPORT"])
    writer.writerow(["Generated On:", datetime.now().strftime('%d %B %Y, %H:%M:%S')])
    writer.writerow(["Facility Location:", "123 Eco-Factory Industrial Area, Bengaluru, Karnataka, India"])
    writer.writerow(["========================================================================================"])
    writer.writerow([])
    
    writer.writerow(["Record ID", "Partner / Buyer ID", "Material Tracked", "Weight Processed", "Financial Value (INR)", "Status", "Audit Timestamp"])
    
    total_revenue = 0
    total_costs = 0
    total_weight = 0
    
    for s in sales_records: 
        val_str = f"Rs. {s.total_price:,.2f}"
        
        if "DISP" in s.status or "TOXIC" in s.status or "DISPOSED" in s.status:
            val_str = f"- Rs. {s.total_price:,.2f} (Agency Cost)"
            total_costs += s.total_price
        else:
            if s.status == "APPROVED":
                total_revenue += s.total_price
        
        total_weight += s.quantity_sold_kg
        
        writer.writerow([
            f"AUDIT-REC-{s.id:04d}", 
            f"PRTNR-{s.buyer_id:04d}", 
            s.material_type, 
            f"{s.quantity_sold_kg:.2f} kg", 
            val_str, 
            s.status.replace('_', ' '), 
            datetime.now().strftime('%Y-%m-%d')
        ])
    
    writer.writerow([])
    writer.writerow(["========================================================================================"])
    writer.writerow(["END OF AUDIT REPORT SUMMARY"])
    writer.writerow(["Total Material Processed:", f"{total_weight:,.2f} kg"])
    writer.writerow(["Total Approved Revenue:", f"Rs. {total_revenue:,.2f}"])
    writer.writerow(["Total Safe Disposal Costs:", f"Rs. {total_costs:,.2f}"])
    writer.writerow(["Net Environmental Profit:", f"Rs. {(total_revenue - total_costs):,.2f}"])
    writer.writerow(["========================================================================================"])

    csv_bytes = output.getvalue().encode('utf-8')
    
    return StreamingResponse(
        iter([csv_bytes]), 
        media_type="text/csv", 
        headers={"Content-Disposition": "attachment; filename=EWaste_Full_Audit_Report.csv"}
    )

@app.get("/api/export/certificate")
def download_certificate_pdf(batch_id: str, client_id: str, hash_val: str, client_name: str, worker_name: str, db: Session = Depends(get_db)):
    try:
        cid = int(client_id)
        bid = int(batch_id)
        
        client = db.query(models.Client).filter(models.Client.id == cid).first()
        client_address = client.address if client and client.address else "Address details not provided"
        
        waste = db.query(models.InboundWaste).filter(models.InboundWaste.batch_id == bid).first()
        weight = float(waste.actual_arrived_weight) if waste and waste.actual_arrived_weight else 0.0
        
        buffer = io.BytesIO()
        p = canvas.Canvas(buffer, pagesize=landscape(letter))
        width, height = landscape(letter)
        
        primary_green = "#0B5B3E"
        light_green = "#4CAF50"

        p.setFillColor(HexColor(primary_green))
        path = p.beginPath()
        path.moveTo(0, height)
        path.lineTo(480, height)
        path.lineTo(410, height-110)
        path.lineTo(0, height-110)
        path.close()
        p.drawPath(path, stroke=0, fill=1)
        
        p.setFillColor(HexColor("#ffffff"))
        p.setFont("Helvetica-Bold", 18)
        p.drawString(20, height - 40, "E-Waste Master ERP Solutions")
        p.setFont("Helvetica", 10)
        p.drawString(20, height - 65, "123 Eco-Factory Industrial Area, Bengaluru, Karnataka - 560067")
        p.drawString(20, height - 85, "Environmental Protection & Safe Recycling Logistics")

        if os.path.exists("static/logo.png"):
            try:
                p.drawImage("static/logo.png", width - 200, height - 90, width=160, height=60, preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 26)
        p.drawCentredString(width / 2, height - 160, "RECYCLING CERTIFICATE")
        p.setStrokeColor(HexColor(primary_green))
        p.setLineWidth(2)
        p.line(width / 2 - 180, height - 168, width / 2 + 180, height - 168)

        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, height - 210, "THIS IS TO CERTIFY THAT -")

        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica-Bold", 20)
        p.drawCentredString(width / 2 + 20, height - 210, client_name)

        p.setStrokeColor(HexColor(light_green))
        p.setDash(2, 2)
        p.line(50, height - 220, width - 50, height - 220)

        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, height - 270, "LOCATED AT -")
        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica", 14)
        p.drawCentredString(width / 2 + 20, height - 270, client_address)
        p.line(50, height - 280, width - 50, height - 280)

        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, height - 330, "E-WASTE PROCESSED -")
        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(220, height - 330, f"(Weight - {weight:.2f} Kgs).")
        
        p.setFillColor(HexColor(primary_green))
        p.drawString(480, height - 330, "COMPLETION DATE -")
        p.setFillColor(HexColor("#000000"))
        p.drawString(620, height - 330, datetime.now().strftime('%d-%m-%Y'))
        p.line(50, height - 340, width - 50, height - 340)
        
        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, height - 390, "PROCESSED MATERIALS -")
        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica", 12)
        p.drawString(220, height - 390, "Mixed E-Electronics, Cables")
        p.line(50, height - 400, width - 50, height - 400)

        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, height - 450, "PROCESSING SUPERVISED BY -")
        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(250, height - 450, worker_name)
        p.line(50, height - 460, width - 50, height - 460)

        p.setDash()
        
        if os.path.exists("static/3920190247829.png"):
            try:
                sig = Image.open("static/3920190247829.png").convert("RGBA")
                bg = Image.new("RGB", sig.size, (255, 255, 255))
                bg.paste(sig, (0, 0), sig)
                p.drawImage(ImageReader(bg), 50, 90, width=120, height=60, preserveAspectRatio=True)
            except Exception:
                pass
        
        p.setStrokeColor(HexColor(light_green))
        p.setLineWidth(1)
        p.line(50, 85, 250, 85)
        
        p.setFillColor(HexColor(primary_green))
        p.setFont("Helvetica-Bold", 12)
        p.drawString(50, 70, "Manager Signature")
        
        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica-Bold", 10)
        p.drawString(50, 55, "[ Secure Cryptographic Signature ]")
        p.setFont("Courier", 8)
        p.drawString(50, 43, hash_val[:32])
        p.drawString(50, 33, hash_val[32:])

        if os.path.exists("static/seal.png"):
            try:
                seal_img = Image.open("static/seal.png").convert("RGBA")
                seal_bg = Image.new("RGB", seal_img.size, (255, 255, 255))
                seal_bg.paste(seal_img, (0, 0), seal_img)
                p.drawImage(ImageReader(seal_bg), 620, 50, width=110, height=110, preserveAspectRatio=True)
            except Exception:
                pass

        p.setFillColor(HexColor("#000000"))
        p.setFont("Helvetica-Bold", 12)
        p.drawCentredString(480, 110, datetime.now().strftime('%d - %b - %Y'))
        p.setStrokeColor(HexColor(light_green))
        p.line(410, 100, 550, 100)
        p.setFillColor(HexColor(primary_green))
        p.drawCentredString(480, 85, "Date of Issue")

        p.setFillColor(HexColor("#8BC34A")) 
        path1 = p.beginPath()
        path1.moveTo(width-120, 0)
        path1.lineTo(width, 180)
        path1.lineTo(width, 0)
        path1.close()
        p.drawPath(path1, stroke=0, fill=1)
        
        p.setFillColor(HexColor(primary_green))
        path2 = p.beginPath()
        path2.moveTo(width-60, 0)
        path2.lineTo(width, 90)
        path2.lineTo(width, 0)
        path2.close()
        p.drawPath(path2, stroke=0, fill=1)

        p.showPage()
        p.save()
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Compliance_Certificate_Batch_{batch_id}.pdf"})
        
    except Exception as e:
        return HTMLResponse(content=f"<h1>PDF Generation Failed</h1><h2 style='color:red;'>Python Error: {str(e)}</h2><p>Check your backend variables.</p>", status_code=500)

@app.get("/api/export/invoice/{order_id}")
def download_invoice(order_id: int, db: Session = Depends(get_db)):
    order = db.query(models.SalesOrder).filter(models.SalesOrder.id == order_id).first()
    if not order or order.buyer_id == 0:
        raise HTTPException(status_code=404, detail="Invoice not found or invalid.")
    buyer = db.query(models.Buyer).filter(models.Buyer.id == order.buyer_id).first()
    
    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    
    primary_green = "#3E7341"
    p.setFillColor(HexColor(primary_green))
    p.rect(0, height - 40, width, 40, fill=1, stroke=0)
    
    p.setFillColor(HexColor("#333333"))
    p.setFont("Helvetica", 36)
    p.drawString(50, height - 100, "TAX INVOICE")
    
    if os.path.exists("static/logo.png"):
        try:
            p.drawImage("static/logo.png", width - 200, height - 110, width=150, height=45, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
            
    p.setFont("Helvetica-Bold", 10)
    p.setFillColor(HexColor("#7f8c8d"))
    p.drawString(50, height - 150, "BILL TO")
    p.drawString(220, height - 150, "SHIP TO")
    p.drawString(420, height - 150, "INVOICE #")
    p.drawString(420, height - 165, "INVOICE DATE")
    p.drawString(420, height - 180, "GOV LICENSE")
    
    p.setFillColor(HexColor("#000000"))
    p.setFont("Helvetica", 10)
    company_name = buyer.company_name if buyer else "Marketplace Partner"
    license_no = buyer.license_number if buyer else "N/A"
    
    p.drawString(50, height - 165, company_name)
    p.drawString(50, height - 180, "Corporate Headquarters")
    p.drawString(220, height - 165, company_name)
    p.drawString(220, height - 180, "Receiving Facility")
    
    p.drawString(510, height - 150, f"INV-{order.id:04d}")
    p.drawString(510, height - 165, datetime.now().strftime('%d/%m/%Y'))
    p.drawString(510, height - 180, license_no)
    
    p.setStrokeColor(HexColor("#cccccc"))
    p.line(50, height - 210, width - 50, height - 210)
    
    p.setFont("Helvetica", 16)
    p.drawCentredString(width / 2, height - 240, "E-Waste Master ERP Solutions")
    p.setFont("Helvetica", 10)
    p.drawCentredString(width / 2, height - 255, "123 Eco-Factory Industrial Area")
    p.drawCentredString(width / 2, height - 270, "Bengaluru, Karnataka, India")
    
    p.line(50, height - 290, width - 50, height - 290)
    
    p.setFillColor(HexColor(primary_green))
    p.rect(50, height - 330, width - 100, 25, fill=1, stroke=0)
    p.setFillColor(HexColor("#ffffff"))
    p.setFont("Helvetica-Bold", 10)
    p.drawString(60, height - 322, "DESCRIPTION")
    p.drawString(330, height - 322, "UNIT PRICE")
    p.drawString(420, height - 322, "QTY")
    p.drawString(500, height - 322, "AMOUNT")
    
    p.setFillColor(HexColor("#000000"))
    p.setFont("Helvetica", 10)
    rate = order.total_price / order.quantity_sold_kg if order.quantity_sold_kg > 0 else 0
    
    p.setFillColor(HexColor("#f9f9f9"))
    p.rect(50, height - 355, width - 100, 25, fill=1, stroke=0)
    
    p.setFillColor(HexColor("#000000"))
    p.drawString(60, height - 347, order.material_type)
    p.drawString(330, height - 347, f"₹{rate:.2f}")
    p.drawString(420, height - 347, f"{order.quantity_sold_kg:.2f}")
    p.drawString(500, height - 347, f"₹{order.total_price:.2f}")
    
    gst = order.total_price * 0.18
    grand_total = order.total_price + gst
    
    p.setFont("Helvetica", 10)
    p.setFillColor(HexColor("#7f8c8d"))
    p.drawString(380, height - 400, "SUBTOTAL")
    p.drawString(380, height - 415, "GST (18%)")
    
    p.setFillColor(HexColor("#000000"))
    p.drawString(500, height - 400, f"₹{order.total_price:.2f}")
    p.drawString(500, height - 415, f"₹{gst:.2f}")
    
    p.line(380, height - 430, width - 50, height - 430)
    
    p.setFont("Helvetica", 12)
    p.setFillColor(HexColor("#7f8c8d"))
    p.drawString(380, height - 455, "TAX INVOICE TOTAL")
    p.setFont("Helvetica-Bold", 12)
    p.setFillColor(HexColor("#000000"))
    p.drawString(500, height - 455, f"₹{grand_total:.2f}")
    
    if os.path.exists("static/3920190247829.png"):
        try:
            sig = Image.open("static/3920190247829.png").convert("RGBA")
            bg = Image.new("RGB", sig.size, (255, 255, 255))
            bg.paste(sig, (0, 0), sig)
            p.drawImage(ImageReader(bg), 400, height - 540, width=150, height=75, preserveAspectRatio=True)
        except Exception:
            pass
            
    if os.path.exists("static/seal.png"):
        try:
            seal_img = Image.open("static/seal.png").convert("RGBA")
            seal_bg = Image.new("RGB", seal_img.size, (255, 255, 255))
            seal_bg.paste(seal_img, (0, 0), seal_img)
            p.drawImage(ImageReader(seal_bg), 430, height - 640, width=90, height=90, preserveAspectRatio=True)
        except Exception:
            pass

    p.setFont("Helvetica-Bold", 10)
    p.setFillColor(HexColor("#7f8c8d"))
    p.drawString(50, 100, "TERMS & CONDITIONS")
    p.setFillColor(HexColor("#000000"))
    p.drawString(50, 85, "Payment is due within 15 days")
    p.setFont("Helvetica", 10)
    p.drawString(50, 60, "Please make checks/transfers payable to: E-Waste Master ERP Solutions")
    
    p.setFillColor(HexColor(primary_green))
    p.rect(0, 0, width, 25, fill=1, stroke=0)
    
    p.showPage()
    p.save()
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=Invoice_INV-{order.id:04d}.pdf"})

@app.get("/dashboard", response_class = HTMLResponse)
async def serve_ui(request: Request):
    return templates.TemplateResponse({"request": request}, name ="index.html")