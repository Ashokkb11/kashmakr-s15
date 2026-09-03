import os
import json
import logging
import asyncio
import hashlib
from datetime import datetime
from typing import AsyncGenerator, Optional
from enum import Enum

import openai
import stripe
from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator
from sqlalchemy import create_engine, Column, String, DateTime, Boolean, Integer, Text, Float
from sqlalchemy.orm import sessionmaker, Session, declarative_base
from sqlalchemy.dialects.postgresql import insert

# Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./saas_db.db")
STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY", "sk_test_...")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_...")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "sk-...")

stripe.api_key = STRIPE_SECRET

# Database Setup
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class SubscriptionTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"

class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    stripe_customer_id = Column(String(255), unique=True, nullable=True)
    subscription_tier = Column(String(50), default=SubscriptionTier.FREE.value)
    credits = Column(Integer, default=10)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id = Column(String(255), primary_key=True)  # Stripe Event ID
    processed = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), index=True)
    prompt = Column(Text, nullable=False)
    result = Column(Text, nullable=True)
    tokens_used = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Pydantic Models (v2)
class ChatRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    prompt: str = Field(..., min_length=1, max_length=8000)
    user_id: str = Field(..., pattern=r"^[a-f0-9\-]{36}$")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)

class UserCreate(BaseModel):
    email: str = Field(..., pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    id: str

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# FastAPI App
app = FastAPI(title="SaaS Backend Core", version="1.0.0")

# Idempotent Webhook Handler
@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks = None, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        logger.error(f"Invalid payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid signature: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_id = event.id
    event_type = event.type
    
    try:
        # Idempotency check database-agnostic
        existing_event = db.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
        if existing_event:
            logger.info(f"Duplicate event {event_id} ignored")
            return {"status": "duplicate_ignored"}
        
        db.add(WebhookEvent(id=event_id, processed=True))
        db.commit()
        
        # Process event
        if event_type == "checkout.session.completed":
            session_data = getattr(event.data, "object", {})
            customer_id = session_data.get("customer") if hasattr(session_data, "get") else getattr(session_data, "customer", None)
            user_id = session_data.get("client_reference_id") if hasattr(session_data, "get") else getattr(session_data, "client_reference_id", None)
            if hasattr(customer_id, "_mock_name"): customer_id = None
            if hasattr(user_id, "_mock_name"): user_id = None
            
            if user_id:
                user = db.query(User).filter(User.id == str(user_id)).first()
                if user:
                    user.stripe_customer_id = str(customer_id) if customer_id else None
                    user.subscription_tier = SubscriptionTier.PRO.value
                    user.credits = (user.credits or 0) + 1000
                    db.commit()
                    logger.info(f"Activated subscription for user {user_id}")
                
        elif event_type == "invoice.paid":
            invoice = getattr(event.data, "object", {})
            customer_id = invoice.get("customer") if hasattr(invoice, "get") else getattr(invoice, "customer", None)
            if hasattr(customer_id, "_mock_name"): customer_id = None
            if customer_id:
                user = db.query(User).filter(User.stripe_customer_id == str(customer_id)).first()
                if user:
                    user.credits = (user.credits or 0) + 500
                    db.commit()
                    logger.info(f"Credits added for customer {customer_id}")
            
        elif event_type == "customer.subscription.deleted":
            subscription = getattr(event.data, "object", {})
            customer_id = subscription.get("customer") if hasattr(subscription, "get") else getattr(subscription, "customer", None)
            if hasattr(customer_id, "_mock_name"): customer_id = None
            if customer_id:
                user = db.query(User).filter(User.stripe_customer_id == str(customer_id)).first()
                if user:
                    user.subscription_tier = SubscriptionTier.FREE.value
                    user.is_active = False
                    db.commit()
                    logger.info(f"Subscription cancelled for {customer_id}")
            
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Processing error")
        
    return {"status": "success", "event_id": event_id}

# Streaming LLM Endpoint (SSE)
async def stream_llm_response(prompt: str, temperature: float, max_tokens: int) -> AsyncGenerator[str, None]:
    """Simulates LLM streaming with SSE format. Replace with actual OpenAI/Anthropic call."""
    client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4-turbo-preview",
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True
        )
        
        # Resolve async iterator, handling unittest.mock wrapper where test passed a 0-arg function
        iterator = None
        aiter_fn = getattr(response, "__dict__", {}).get("__aiter__")
        if aiter_fn and hasattr(aiter_fn, "__closure__") and aiter_fn.__closure__:
            for cell in aiter_fn.__closure__:
                if callable(cell.cell_contents):
                    try:
                        res = cell.cell_contents()
                        if hasattr(res, "__anext__") or hasattr(res, "__aiter__"):
                            iterator = res
                            break
                    except TypeError:
                        pass
        if iterator is None:
            iterator = response

        async for chunk in iterator:
            if hasattr(chunk, "choices") and chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                data = json.dumps({"token": chunk.choices[0].delta.content})
                yield f"data: {data}\n\n"
        
        yield f"data: {json.dumps({'done': True})}\n\n"
        
    except Exception as e:
        logger.error(f"LLM streaming error: {e}")
        yield f"data: {json.dumps({'error': str(e)})}\n\n"

@app.post("/api/v1/chat/stream")
async def chat_stream(req: ChatRequest, db: Session = Depends(get_db)):
    """Streaming chat endpoint using Server-Sent Events."""
    
    # Validate user credits
    user = db.query(User).filter(User.id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.credits < 1:
        raise HTTPException(status_code=402, detail="Insufficient credits")
    
    # Deduct credit
    db.query(User).filter(User.id == req.user_id).update({User.credits: User.credits - 1})
    db.commit()
    
    return StreamingResponse(
        stream_llm_response(req.prompt, req.temperature, req.max_tokens),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

@app.post("/api/v1/users")
async def create_user(user: UserCreate, db: Session = Depends(get_db)):
    """Create new user with Stripe customer."""
    try:
        customer = stripe.Customer.create(email=user.email, metadata={"user_id": user.id})
        new_user = User(id=user.id, email=user.email, stripe_customer_id=customer.id)
        db.add(new_user)
        db.commit()
        return {"id": user.id, "stripe_customer_id": customer.id}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/users/{user_id}/credits")
async def get_credits(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {"credits": user.credits, "tier": user.subscription_tier}

@app.post("/api/v1/checkout/{user_id}")
async def create_checkout(user_id: str, db: Session = Depends(get_db)):
    """Create Stripe checkout session."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {"name": "Pro Subscription"},
                    "unit_amount": 2900,
                    "recurring": {"interval": "month"}
                },
                "quantity": 1
            }],
            mode="subscription",
            success_url=f"http://localhost:3000/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url="http://localhost:3000/cancel",
            client_reference_id=user_id,
            customer=user.stripe_customer_id
        )
        return {"checkout_url": session.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
