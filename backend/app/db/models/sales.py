"""OWNER SERVICE: sales. Cart, orders (three status axes), fulfilment, returns, payments.

``customer_id``, ``variant_id`` and ``product_id`` are plain columns: identity and catalog own
those rows.
Order lines snapshot name/sku/HSN/price/tax at checkout so later catalog edits never rewrite
history.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models._common import BIGPK, MONEY, QTY, business_fk, enum_check, id_column, utc_now
from app.db.session import Base

ORDER_STATUSES = (
    "PENDING_PAYMENT",
    "CONFIRMED",
    "PACKED",
    "SHIPPED",
    "DELIVERED",
    "CANCELLED",
    "RETURN_REQUESTED",
    "RETURNED",
)
PAYMENT_STATUSES = ("UNPAID", "PENDING", "PAID", "PARTIALLY_REFUNDED", "REFUNDED", "FAILED")
FULFILMENT_STATUSES = ("UNFULFILLED", "PACKED", "SHIPPED", "DELIVERED")


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (
        enum_check("status", ("ACTIVE", "CONVERTED", "ABANDONED"), "status"),
        Index("ix_carts_business_customer", "business_id", "customer_id"),
        Index("ix_carts_anonymous_token_hash", "anonymous_token_hash"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    customer_id: Mapped[str | None] = mapped_column(String(36))
    anonymous_token_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class CartItem(Base):
    """Only variant and quantity; prices are recomputed from the catalog at checkout."""

    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "variant_id", name="uq_cart_items_cart_variant"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[str] = id_column()
    cart_id: Mapped[str] = mapped_column(String(36), ForeignKey("carts.id", ondelete="CASCADE"))
    business_id: Mapped[str] = business_fk()
    variant_id: Mapped[str] = mapped_column(String(36))
    quantity: Mapped[Decimal] = mapped_column(QTY)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("business_id", "order_number", name="uq_orders_business_number"),
        UniqueConstraint("business_id", "idempotency_key", name="uq_orders_business_key"),
        enum_check("order_status", ORDER_STATUSES, "order_status"),
        enum_check("payment_status", PAYMENT_STATUSES, "payment_status"),
        enum_check("fulfilment_status", FULFILMENT_STATUSES, "fulfilment_status"),
        enum_check("payment_method", ("ONLINE", "COD"), "payment_method"),
        CheckConstraint("grand_total >= 0", name="grand_total_non_negative"),
        Index("ix_orders_business_customer_placed", "business_id", "customer_id", "placed_at"),
        Index("ix_orders_business_status_placed", "business_id", "order_status", "placed_at"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    customer_id: Mapped[str] = mapped_column(String(36))
    order_number: Mapped[str] = mapped_column(String(30))
    order_status: Mapped[str] = mapped_column(String(20), default="PENDING_PAYMENT")
    payment_status: Mapped[str] = mapped_column(String(20), default="UNPAID")
    fulfilment_status: Mapped[str] = mapped_column(String(12), default="UNFULFILLED")
    payment_method: Mapped[str] = mapped_column(String(8), default="ONLINE")
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    subtotal: Mapped[Decimal] = mapped_column(MONEY, default=0)
    discount_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    tax_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    shipping_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    round_off: Mapped[Decimal] = mapped_column(MONEY, default=0)
    grand_total: Mapped[Decimal] = mapped_column(MONEY, default=0)
    ship_to: Mapped[dict | None] = mapped_column(JSON)
    place_of_supply_state: Mapped[str | None] = mapped_column(String(2))
    idempotency_key: Mapped[str] = mapped_column(String(100))
    placed_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime())
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_order_items_business_order", "business_id", "order_id"),
        Index("ix_order_items_business_variant", "business_id", "variant_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"))
    product_id: Mapped[str] = mapped_column(String(36))
    variant_id: Mapped[str] = mapped_column(String(36))
    product_name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str] = mapped_column(String(60))
    options: Mapped[dict | None] = mapped_column(JSON)
    hsn_sac: Mapped[str | None] = mapped_column(String(8))
    stock_unit: Mapped[str] = mapped_column(String(10), default="PCS")
    quantity: Mapped[Decimal] = mapped_column(QTY)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    discount: Mapped[Decimal] = mapped_column(MONEY, default=0)
    taxable_value: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cgst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    sgst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    igst: Mapped[Decimal] = mapped_column(MONEY, default=0)
    cess: Mapped[Decimal] = mapped_column(MONEY, default=0)
    tax_class: Mapped[str] = mapped_column(String(10), default="NONE")
    tax_rate_version_id: Mapped[str | None] = mapped_column(String(36))
    line_total: Mapped[Decimal] = mapped_column(MONEY)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    __table_args__ = (
        enum_check("axis", ("ORDER", "PAYMENT", "FULFILMENT"), "axis"),
        Index("ix_order_status_history_business_order", "business_id", "order_id"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"))
    axis: Mapped[str] = mapped_column(String(12))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(36))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (Index("ix_shipments_business_order", "business_id", "order_id"),)

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"))
    carrier: Mapped[str | None] = mapped_column(String(60))
    tracking_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime())


class ReturnRequest(Base):
    __tablename__ = "return_requests"
    __table_args__ = (
        enum_check(
            "status", ("REQUESTED", "APPROVED", "REJECTED", "RECEIVED", "REFUNDED"), "status"
        ),
        Index("ix_return_requests_business_order", "business_id", "order_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"))
    status: Mapped[str] = mapped_column(String(10), default="REQUESTED")
    reason: Mapped[str | None] = mapped_column(String(255))
    requested_by: Mapped[str | None] = mapped_column(String(36))
    decided_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class ReturnRequestItem(Base):
    __tablename__ = "return_request_items"
    __table_args__ = (
        enum_check("item_condition", ("RESELLABLE", "DAMAGED"), "item_condition"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[str] = id_column()
    return_id: Mapped[str] = mapped_column(String(36), ForeignKey("return_requests.id"), index=True)
    business_id: Mapped[str] = business_fk()
    order_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("order_items.id"))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    item_condition: Mapped[str] = mapped_column(String(10), default="RESELLABLE")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_order_id", name="uq_payments_provider_order"),
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
        enum_check(
            "status",
            ("CREATED", "PENDING", "CAPTURED", "FAILED", "REFUNDED", "PARTIALLY_REFUNDED"),
            "status",
        ),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_payments_business_order", "business_id", "order_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"))
    provider: Mapped[str] = mapped_column(String(30))  # 'fake' locally; real providers are deferred
    provider_order_id: Mapped[str] = mapped_column(String(80))
    provider_payment_id: Mapped[str | None] = mapped_column(String(80))
    method: Mapped[str | None] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="CREATED")
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    failure_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now, onupdate=utc_now)


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_payment_webhook_events_provider_event"),
        enum_check("status", ("RECEIVED", "PROCESSED", "IGNORED", "FAILED"), "status"),
        Index("ix_payment_webhook_events_business_id", "business_id"),
    )

    id: Mapped[int] = mapped_column(BIGPK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(30))
    # set once the event is matched to an order; forged/unmatched events stay NULL (tenant unknown)
    business_id: Mapped[str | None] = business_fk(nullable=True)
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(60))
    signature_valid: Mapped[bool] = mapped_column(Boolean(), default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="RECEIVED")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime())
    error: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        UniqueConstraint("business_id", "idempotency_key", name="uq_refunds_business_key"),
        enum_check("status", ("REQUESTED", "SUCCEEDED", "FAILED"), "status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_refunds_business_payment", "business_id", "payment_id"),
    )

    id: Mapped[str] = id_column()
    business_id: Mapped[str] = business_fk()
    payment_id: Mapped[str] = mapped_column(String(36), ForeignKey("payments.id"))
    order_id: Mapped[str] = mapped_column(String(36), ForeignKey("orders.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(10), default="REQUESTED")
    idempotency_key: Mapped[str] = mapped_column(String(100))
    provider_refund_id: Mapped[str | None] = mapped_column(String(80))
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utc_now)


__all__ = [
    "Cart",
    "CartItem",
    "FULFILMENT_STATUSES",
    "ORDER_STATUSES",
    "Order",
    "OrderItem",
    "OrderStatusHistory",
    "PAYMENT_STATUSES",
    "Payment",
    "PaymentWebhookEvent",
    "Refund",
    "ReturnRequest",
    "ReturnRequestItem",
    "Shipment",
]
