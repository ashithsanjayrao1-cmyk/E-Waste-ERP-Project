from sqlalchemy import Column, Integer, String, Float, Date
from database import Base
import datetime

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True)
    hashed_password = Column(String(255))
    role = Column(String(50))

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    client_type = Column(String(50))
    name = Column(String(100))
    gst_number = Column(String(50))
    address = Column(String(255))

class PickupTask(Base):
    __tablename__ = "pickup_tasks"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer)
    expected_weight_kg = Column(Float)
    pickup_date = Column(String(50))
    driver_name = Column(String(50))
    status = Column(String(50), default="ASSIGNED")

class InboundWaste(Base):
    __tablename__ = "inbound_waste"
    batch_id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer)
    actual_arrived_weight = Column(Float)
    discrepancy_note = Column(String(255), nullable=True)
    status = Column(String(50), default="RAW")

class JobCard(Base):
    __tablename__ = "job_cards"
    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer)
    worker_name = Column(String(50))
    instructions = Column(String(255))
    status = Column(String(50), default="IN_PROGRESS")

class ExtractedMaterial(Base):
    __tablename__ = "extracted_materials"
    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer)
    material_type = Column(String(50))
    yield_weight_kg = Column(Float)

class Buyer(Base):
    __tablename__ = "buyers"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True)
    company_name = Column(String(100))
    license_number = Column(String(50))
    contact_email = Column(String(100))

class SalesOrder(Base):
    __tablename__ = "sales_orders"
    id = Column(Integer, primary_key=True, index=True)
    buyer_id = Column(Integer)
    material_type = Column(String(50))
    quantity_sold_kg = Column(Float)
    total_price = Column(Float)
    status = Column(String(50), default="PENDING")

class ComplianceCertificate(Base):
    __tablename__ = "compliance_certificates"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer)
    batch_id = Column(Integer)
    issue_date = Column(Date, default=datetime.date.today)
    certificate_hash = Column(String(255))