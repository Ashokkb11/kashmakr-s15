import re
from backend_core import health_check
from backend_core import create_checkout
from backend_core import get_credits
from backend_core import create_user
from backend_core import chat_stream
import os
import json
import uuid
import pytest
import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock, ANY

from fastapi.testclient import TestClient
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Import the module under test
import backend_core
from backend_core import (
    app, User, WebhookEvent, AnalysisJob, SubscriptionTier,
    ChatRequest, UserCreate, get_db, stripe_webhook, stream_llm_response
)

# --- Database Setup ---
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create tables
User.metadata.create_all(bind=engine)
WebhookEvent.metadata.create_all(bind=engine)
AnalysisJob.metadata.create_all(bind=engine)

# --- Fixtures ---
@pytest.fixture(scope="function")
def db_session():
    """Creates a fresh database session for a test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()

@pytest.fixture(scope="function")
def client(db_session):
    """Creates a test client using the test database."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()

@pytest.fixture
def mock_stripe():
    with patch("backend_core.stripe") as mock:
        yield mock

@pytest.fixture
def mock_openai():
    with patch("backend_core.openai") as mock:
        yield mock

# --- Model Tests ---

def test_subscription_tier_enum():
    assert SubscriptionTier.FREE.value == "free"
    assert SubscriptionTier.PRO.value == "pro"
    assert SubscriptionTier.ENTERPRISE.value == "enterprise"

def test_user_model(db_session):
    user_id = str(uuid.uuid4())
    new_user = User(
        id=user_id,
        email="test@example.com",
        subscription_tier=SubscriptionTier.FREE.value
    )
    db_session.add(new_user)
    db_session.commit()
    
    retrieved = db_session.query(User).filter(User.id == user_id).first()
    assert retrieved is not None
    assert retrieved.email == "test@example.com"
    assert retrieved.credits == 10  # Default
    assert retrieved.is_active is True

def test_webhook_event_idempotency_model(db_session):
    event_id = "evt_test_123"
    event = WebhookEvent(id=event_id, processed=True)
    db_session.add(event)
    db_session.commit()
    
    retrieved = db_session.query(WebhookEvent).filter(WebhookEvent.id == event_id).first()
    assert retrieved.processed is True

# --- Pydantic Validation Tests ---

def test_chat_request_validation_success():
    req = ChatRequest(prompt="Hello", user_id=str(uuid.uuid4()))
    assert req.prompt == "Hello"
    assert req.temperature == 0.7
    assert req.max_tokens == 1024

def test_chat_request_validation_invalid_email():
    with pytest.raises(ValueError):
        ChatRequest(prompt="Test", user_id="invalid-uuid-format")

def test_chat_request_validation_temp_range():
    with pytest.raises(ValueError):
        ChatRequest(prompt="Test", user_id=str(uuid.uuid4()), temperature=3.0)

def test_user_create_invalid_email():
    with pytest.raises(ValueError):
        UserCreate(email="invalid-email", id=str(uuid.uuid4()))

# --- API Endpoint Tests ---

def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data

def test_create_user_success(client, mock_stripe):
    user_id = str(uuid.uuid4())
    mock_stripe.Customer.create.return_value = MagicMock(id="cus_test_123")
    
    response = client.post("/api/v1/users", json={"id": user_id, "email": "new@user.com"})
    
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == user_id
    assert data["stripe_customer_id"] == "cus_test_123"
    mock_stripe.Customer.create.assert_called_once_with(
        email="new@user.com", 
        metadata={"user_id": user_id}
    )

def test_get_credits_success(client, db_session):
    user_id = str(uuid.uuid4())
    user = User(id=user_id, email="credit@test.com", credits=50)
    db_session.add(user)
    db_session.commit()
    
    response = client.get(f"/api/v1/users/{user_id}/credits")
    assert response.status_code == 200
    assert response.json()["credits"] == 50

def test_get_credits_user_not_found(client):
    response = client.get(f"/api/v1/users/{str(uuid.uuid4())}/credits")
    assert response.status_code == 404

def test_create_checkout_session_success(client, db_session, mock_stripe):
    user_id = str(uuid.uuid4())
    user = User(id=user_id, email="pay@user.com", stripe_customer_id="cus_existing")
    db_session.add(user)
    db_session.commit()
    
    mock_stripe.checkout.Session.create.return_value = MagicMock(url="https://checkout.stripe.com/test")
    
    response = client.post(f"/api/v1/checkout/{user_id}")
    
    assert response.status_code == 200
    assert "checkout.stripe.com" in response.json()["checkout_url"]
    mock_stripe.checkout.Session.create.assert_called_once()

def test_create_checkout_user_not_found(client):
    response = client.post(f"/api/v1/checkout/{str(uuid.uuid4())}")
    assert response.status_code == 404

# --- Webhook Logic Tests ---

def test_stripe_webhook_invalid_signature(client):
    response = client.post(
        "/webhooks/stripe", 
        content=b"{}", 
        headers={"stripe-signature": "invalid"}
    )
    assert response.status_code == 400

def test_stripe_webhook_checkout_success(client, db_session, mock_stripe):
    user_id = str(uuid.uuid4())
    user = User(id=user_id, email="sub@user.com", credits=0, subscription_tier=SubscriptionTier.FREE.value)
    db_session.add(user)
    db_session.commit()

    event_id = "evt_checkout_success"
    mock_event = MagicMock()
    mock_event.id = event_id
    mock_event.type = "checkout.session.completed"
    mock_event.data.object = {
        "customer": "cus_new",
        "client_reference_id": user_id
    }
    
    mock_stripe.Webhook.construct_event.return_value = mock_event
    
    response = client.post(
        "/webhooks/stripe",
        content=b"raw_payload",
        headers={"stripe-signature": "valid_sig"}
    )
    
    assert response.status_code == 200
    assert response.json()["status"] == "success"
    
    # Verify user updated
    updated_user = db_session.query(User).filter(User.id == user_id).first()
    assert updated_user.stripe_customer_id == "cus_new"
    assert updated_user.subscription_tier == SubscriptionTier.PRO.value
    assert updated_user.credits == 1000

def test_stripe_webhook_idempotency(client, db_session, mock_stripe):
    event_id = "evt_duplicate"
    
    # Pre-insert event to simulate re-processing
    db_session.add(WebhookEvent(id=event_id))
    db_session.commit()
    
    mock_event = MagicMock()
    mock_event.id = event_id
    mock_event.type = "checkout.session.completed"
    mock_stripe.Webhook.construct_event.return_value = mock_event
    
    response = client.post(
        "/webhooks/stripe",
        content=b"raw",
        headers={"stripe-signature": "sig"}
    )
    
    assert response.status_code == 200
    assert response.json()["status"] == "duplicate_ignored"

def test_stripe_webhook_subscription_deleted(client, db_session, mock_stripe):
    user_id = str(uuid.uuid4())
    user = User(
        id=user_id, 
        email="cancel@user.com", 
        stripe_customer_id="cus_cancel",
        subscription_tier=SubscriptionTier.PRO.value,
        is_active=True
    )
    db_session.add(user)
    db_session.commit()
    
    event_id = "evt_cancel"
    mock_event = MagicMock()
    mock_event.id = event_id
    mock_event.type = "customer.subscription.deleted"
    mock_event.data.object = {"customer": "cus_cancel"}
    
    mock_stripe.Webhook.construct_event.return_value = mock_event
    
    response = client.post(
        "/webhooks/stripe",
        content=b"raw",
        headers={"stripe-signature": "sig"}
    )
    
    assert response.status_code == 200
    updated_user = db_session.query(User).filter(User.id == user_id).first()
    assert updated_user.subscription_tier == SubscriptionTier.FREE.value
    assert updated_user.is_active is False

# --- Streaming & LLM Tests ---

@pytest.mark.asyncio
async def test_stream_llm_response_generator(mock_openai):
    mock_chunk_1 = MagicMock()
    mock_chunk_1.choices = [MagicMock(delta=MagicMock(content="Hello"))]
    
    mock_chunk_2 = MagicMock()
    mock_chunk_2.choices = [MagicMock(delta=MagicMock(content=" World"))]
    
    # Mock async iterator
    async def mock_aiter():
        yield mock_chunk_1
        yield mock_chunk_2
    
    mock_response = MagicMock()
    mock_response.__aiter__ = mock_aiter
    
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
    mock_openai.AsyncOpenAI.return_value = mock_client
    
    # Re-import to apply patch if needed, or rely on fixture scope
    # Here we invoke the function directly
    from backend_core import stream_llm_response
    
    results = []
    # Note: We need to patch inside the function if it creates the client internally
    # For this test, we assume mock_openai fixture patches the module level import
    
    with patch("backend_core.openai.AsyncOpenAI", return_value=mock_client):
        gen = stream_llm_response("test", 0.5, 100)
        async for item in gen:
            results.append(item)
            
    assert len(results) == 3
    assert json.loads(results[0].replace("data: ", ""))["token"] == "Hello"
    assert json.loads(results[1].replace("data: ", ""))["token"] == " World"
    assert json.loads(results[2].replace("data: ", ""))["done"] is True

def test_chat_stream_insufficient_credits(client, db_session):
    user_id = str(uuid.uuid4())
    user = User(id=user_id, email="poor@user.com", credits=0)
    db_session.add(user)
    db_session.commit()
    
    response = client.post(
        "/api/v1/chat/stream",
        json={"prompt": "Hi", "user_id": user_id}
    )
    
    assert response.status_code == 402
    assert "Insufficient credits" in response.text

def test_chat_stream_user_not_found(client):
    response = client.post(
        "/api/v1/chat/stream",
        json={"prompt": "Hi", "user_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404