from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import os
import jwt
import bcrypt
import uuid
from bson import ObjectId

# Database setup
MONGO_URL = os.environ.get('MONGO_URL', 'mongodb://localhost:27017')
client = AsyncIOMotorClient(MONGO_URL)
db = client.admin_dashboard

app = FastAPI()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# JWT Configuration
JWT_SECRET = "your-secret-key-change-in-production"
JWT_ALGORITHM = "HS256"
security = HTTPBearer()

# Pydantic Models
class User(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str
    email: str
    role: str = "admin"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class UserCreate(BaseModel):
    username: str
    email: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class Product(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    category: str
    price: float
    stock_quantity: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class ProductCreate(BaseModel):
    name: str
    description: str = ""
    category: str
    price: float
    stock_quantity: int = 0

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    price: Optional[float] = None
    stock_quantity: Optional[int] = None

class FinancialRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str  # "income" or "expense"
    amount: float
    description: str
    category: str
    date: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by: str

class FinancialRecordCreate(BaseModel):
    type: str
    amount: float
    description: str
    category: str

class DashboardStats(BaseModel):
    total_products: int
    total_revenue: float
    total_expenses: float
    low_stock_products: int
    recent_transactions: int

# Helper functions
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")
        
        user = await db.users.find_one({"username": username})
        if user is None:
            raise HTTPException(status_code=401, detail="User not found")
        
        return User(**user)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Initialize admin user
@app.on_event("startup")
async def startup_event():
    # Create default admin user
    admin_exists = await db.users.find_one({"username": "admin"})
    if not admin_exists:
        admin_user = {
            "id": str(uuid.uuid4()),
            "username": "admin",
            "email": "admin@example.com",
            "password": hash_password("admin123"),
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.users.insert_one(admin_user)

# Authentication Routes
@app.post("/api/auth/register")
async def register(user: UserCreate):
    # Check if user already exists
    existing_user = await db.users.find_one({"$or": [{"username": user.username}, {"email": user.email}]})
    if existing_user:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    
    # Create new user
    user_dict = user.dict()
    user_dict["password"] = hash_password(user.password)
    user_dict["id"] = str(uuid.uuid4())
    user_dict["role"] = "admin"
    user_dict["created_at"] = datetime.now(timezone.utc).isoformat()
    
    await db.users.insert_one(user_dict)
    
    # Create access token
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer", "user": User(**user_dict)}

@app.post("/api/auth/login")
async def login(user: UserLogin):
    # Find user
    db_user = await db.users.find_one({"username": user.username})
    if not db_user or not verify_password(user.password, db_user["password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    # Create access token
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer", "user": User(**db_user)}

@app.get("/api/auth/me", response_model=User)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user

# Dashboard Routes
@app.get("/api/dashboard/stats", response_model=DashboardStats)
async def get_dashboard_stats(current_user: User = Depends(get_current_user)):
    # Get total products
    total_products = await db.products.count_documents({})
    
    # Get financial data
    income_pipeline = [{"$match": {"type": "income"}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]
    expense_pipeline = [{"$match": {"type": "expense"}}, {"$group": {"_id": None, "total": {"$sum": "$amount"}}}]
    
    income_result = await db.financial_records.aggregate(income_pipeline).to_list(length=1)
    expense_result = await db.financial_records.aggregate(expense_pipeline).to_list(length=1)
    
    total_revenue = income_result[0]["total"] if income_result else 0
    total_expenses = expense_result[0]["total"] if expense_result else 0
    
    # Get low stock products (stock < 10)
    low_stock_products = await db.products.count_documents({"stock_quantity": {"$lt": 10}})
    
    # Get recent transactions (last 7 days)
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent_transactions = await db.financial_records.count_documents({"date": {"$gte": seven_days_ago}})
    
    return DashboardStats(
        total_products=total_products,
        total_revenue=total_revenue,
        total_expenses=total_expenses,
        low_stock_products=low_stock_products,
        recent_transactions=recent_transactions
    )

# Product Routes
@app.get("/api/products", response_model=List[Product])
async def get_products(current_user: User = Depends(get_current_user)):
    products = await db.products.find().to_list(length=None)
    return [Product(**product) for product in products]

@app.post("/api/products", response_model=Product)
async def create_product(product: ProductCreate, current_user: User = Depends(get_current_user)):
    product_dict = product.dict()
    product_dict["id"] = str(uuid.uuid4())
    product_dict["created_at"] = datetime.now(timezone.utc).isoformat()
    product_dict["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    await db.products.insert_one(product_dict)
    return Product(**product_dict)

@app.get("/api/products/{product_id}", response_model=Product)
async def get_product(product_id: str, current_user: User = Depends(get_current_user)):
    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return Product(**product)

@app.put("/api/products/{product_id}", response_model=Product)
async def update_product(product_id: str, product_update: ProductUpdate, current_user: User = Depends(get_current_user)):
    product = await db.products.find_one({"id": product_id})
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    update_data = {k: v for k, v in product_update.dict().items() if v is not None}
    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()
    
    await db.products.update_one({"id": product_id}, {"$set": update_data})
    
    updated_product = await db.products.find_one({"id": product_id})
    return Product(**updated_product)

@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str, current_user: User = Depends(get_current_user)):
    result = await db.products.delete_one({"id": product_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Product not found")
    return {"message": "Product deleted successfully"}

# Financial Routes
@app.get("/api/financial", response_model=List[FinancialRecord])
async def get_financial_records(current_user: User = Depends(get_current_user)):
    records = await db.financial_records.find().sort("date", -1).to_list(length=None)
    return [FinancialRecord(**record) for record in records]

@app.post("/api/financial", response_model=FinancialRecord)
async def create_financial_record(record: FinancialRecordCreate, current_user: User = Depends(get_current_user)):
    record_dict = record.dict()
    record_dict["id"] = str(uuid.uuid4())
    record_dict["date"] = datetime.now(timezone.utc).isoformat()
    record_dict["created_by"] = current_user.username
    
    await db.financial_records.insert_one(record_dict)
    return FinancialRecord(**record_dict)

@app.delete("/api/financial/{record_id}")
async def delete_financial_record(record_id: str, current_user: User = Depends(get_current_user)):
    result = await db.financial_records.delete_one({"id": record_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Financial record not found")
    return {"message": "Financial record deleted successfully"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)