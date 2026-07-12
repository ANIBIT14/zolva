"""MockBank: an in-memory 'core banking system' exposing tools the agent may use."""

from __future__ import annotations

from zolva import tool

_LOANS: dict[str, dict[str, object]] = {
    "c1": {"dues": 4200, "due_date": "2026-07-20", "options": [4200, 2100, 1400]},
}


@tool
def get_dues(customer_id: str) -> dict[str, object]:
    """Fetch outstanding dues and due date for a customer."""
    loan = _LOANS[customer_id]
    return {"amount": loan["dues"], "due_date": loan["due_date"]}


@tool
def get_repayment_options(customer_id: str) -> list[object]:
    """Fetch available repayment amounts (full and part payments)."""
    return list(_LOANS[customer_id]["options"])  # type: ignore[arg-type]


@tool
def send_payment_link(customer_id: str, amount: int) -> dict[str, str]:
    """Send a payment link for the agreed amount. Irreversible customer contact."""
    return {"status": "sent", "link": f"https://pay.mockbank.example/{customer_id}/{amount}"}
