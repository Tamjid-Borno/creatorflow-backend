from django.urls import path
from .views import (
    paddle_webhook,
    health,
    generate_review,
    finalize_checkout,
    confirm_plan,
    refresh_credits,
    select_basic,   # <-- add this
)

urlpatterns = [
    path("generate-review/", generate_review, name="generate_review"),
    path("paddle-webhook/", paddle_webhook, name="paddle_webhook"),
    path("confirm-plan/", confirm_plan, name="confirm_plan"),
    path("health/", health, name="health"),
    path("finalize-checkout/", finalize_checkout, name="finalize_checkout"),
    path("refresh-credits/", refresh_credits, name="refresh_credits"),
    path("select-basic/", select_basic, name="select_basic"),  # <-- add this
]
