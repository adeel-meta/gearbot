"""Approved public Gear FAQ answers sourced from Gear's published policies."""

from __future__ import annotations

import re


TERMS_URL = "https://gearapp.ae/terms"
CANCELLATION_URL = "https://gearapp.ae/cancellation-policy"
SUPPORT_URL = "https://gearapp.ae/support"


def faq_answer(message: str) -> tuple[str, list[str]] | None:
    lower = message.lower()
    if re.search(r"\b(cancel|cancell?ation|refund|no[ -]?show)\b", lower):
        return (
            "You can cancel from My Bookings in the Gear app. Fees, notice periods, and "
            "refund eligibility depend on the rental partner; cancellations within 24 hours "
            "may be non-refundable, and a no-show is non-refundable. If approved, refunds are "
            "normally initiated within 7–10 business days. Review the current policy: "
            f"{CANCELLATION_URL}",
            ["Find a car", "What documents do I need?", "How booking works"],
        )
    if re.search(r"\b(document|documents|driving licen[cs]e|passport|emirates id|eligib)\b", lower):
        return (
            "Gear requires users to be at least 18 and have a valid government-issued ID and "
            "driving licence; tourists may use a passport as applicable. Account registration "
            "may require Emirates ID, passport for tourists, and driving-licence scans. Rental "
            f"partners can apply additional terms. See {TERMS_URL}",
            ["Find a car", "Cancellation policy", "How booking works"],
        )
    if re.search(r"\b(security deposit|payment|fees?)\b", lower):
        return (
            "Gear displays rental charges before booking confirmation. Payments use a third-party "
            "gateway, and the rental partner may require a security deposit under its own terms. "
            f"Review the vehicle terms and {TERMS_URL} before paying.",
            ["Find a car", "Cancellation policy", "What documents do I need?"],
        )
    if re.search(r"\b(contact|support|help desk|phone number|email)\b", lower):
        return (
            "Gear support is available at business@gearapp.ae and through the official support "
            f"page: {SUPPORT_URL}",
            ["Find a car", "How booking works"],
        )
    return None
