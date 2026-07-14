"""Seed demo data for the Phase 0 demo.

    python -m app.seed

Idempotent: re-running wipes the demo restaurants and rebuilds them. It never
touches customers, conversations, or orders.
"""

from datetime import time
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import (
    AdminUser,
    MenuCategory,
    MenuItem,
    Restaurant,
    RestaurantStaff,
    RestaurantStatus,
    RestaurantWorkingHours,
    StaffRole,
    SubscriptionPlan,
)

DEMO = [
    {
        "name": "Karachi Biryani House",
        "cuisine_type": "Desi",
        "phone": "923001110001",
        "address": "Block 5, Gulshan-e-Iqbal, Karachi",
        "description": "Authentic Sindhi biryani and BBQ, family recipe since 1987.",
        "delivery_fee": Decimal("80.00"),
        "min_order_amount": Decimal("500.00"),
        "commission_rate": Decimal("15.00"),
        "staff_email": "owner@karachibiryani.pk",
        "plan": "Growth",
        "menu": {
            "Biryani": [
                ("Chicken Biryani", "Single plate, boneless chicken, aromatic basmati", "450.00"),
                ("Beef Biryani", "Slow-cooked beef, spicy Sindhi masala", "550.00"),
                ("Special Family Biryani", "Serves 4, mixed chicken and beef", "1650.00"),
            ],
            "BBQ": [
                ("Chicken Tikka", "Char-grilled leg piece, 2 pcs", "480.00"),
                ("Seekh Kebab", "Minced beef skewers, 4 pcs", "520.00"),
            ],
            "Sides": [
                ("Raita", "Fresh mint yoghurt", "80.00"),
                ("Soft Drink", "500ml bottle", "120.00"),
            ],
        },
    },
    {
        "name": "Pizza Junction",
        "cuisine_type": "Pizza",
        "phone": "923001110002",
        "address": "DHA Phase 6, Lahore",
        "description": "Hand-tossed pizzas, loaded fries, and shakes.",
        "delivery_fee": Decimal("100.00"),
        "min_order_amount": Decimal("700.00"),
        "commission_rate": Decimal("18.00"),
        "staff_email": "owner@pizzajunction.pk",
        "plan": "Starter",
        "menu": {
            "Pizza": [
                ("Chicken Tikka Pizza (Medium)", "Desi-spiced chicken, onions, capsicum", "1150.00"),
                ("Pepperoni Pizza (Medium)", "Classic pepperoni, mozzarella", "1250.00"),
                ("Fajita Pizza (Large)", "Chicken fajita, jalapenos, extra cheese", "1750.00"),
            ],
            "Sides": [
                ("Loaded Fries", "Cheese sauce, jalapenos", "380.00"),
                ("Garlic Bread", "5 pcs with dip", "300.00"),
            ],
            "Drinks": [
                ("Chocolate Shake", "Thick shake, 400ml", "350.00"),
            ],
        },
    },
    {
        "name": "Wok & Roll",
        "cuisine_type": "Chinese",
        "phone": "923001110003",
        "address": "F-7 Markaz, Islamabad",
        "description": "Fast Chinese — noodles, rice bowls, and soups.",
        "delivery_fee": Decimal("120.00"),
        "min_order_amount": Decimal("600.00"),
        "commission_rate": Decimal("15.00"),
        "staff_email": "owner@wokandroll.pk",
        "plan": "Pro",
        "menu": {
            "Noodles & Rice": [
                ("Chicken Chowmein", "Stir-fried egg noodles, veggies", "620.00"),
                ("Egg Fried Rice", "Wok-tossed with spring onion", "540.00"),
                ("Chilli Chicken with Rice", "Spicy, served with steamed rice", "780.00"),
            ],
            "Soup": [
                ("Hot & Sour Soup", "Chicken, single serving", "320.00"),
            ],
        },
    },
]

DEMO_PASSWORD = "demo1234"

PLANS = [
    {
        "name": "Starter",
        "description": "Commission only. No monthly fee — good for new restaurants.",
        "monthly_fee": Decimal("0.00"),
        "commission_rate": Decimal("18.00"),
        "features": ["WhatsApp ordering", "Menu management", "Order dashboard"],
        "sort_order": 0,
    },
    {
        "name": "Growth",
        "description": "Lower commission for a flat monthly fee.",
        "monthly_fee": Decimal("5000.00"),
        "commission_rate": Decimal("15.00"),
        "features": [
            "Everything in Starter",
            "Lower commission",
            "Customer ratings",
            "Sales reports",
        ],
        "sort_order": 1,
    },
    {
        "name": "Pro",
        "description": "Lowest commission, priority placement.",
        "monthly_fee": Decimal("12000.00"),
        "commission_rate": Decimal("12.00"),
        "features": [
            "Everything in Growth",
            "Lowest commission",
            "Priority listing",
            "Promo campaigns",
        ],
        "sort_order": 2,
    },
]

# 0 = Monday … 6 = Sunday. Lunch and dinner as separate periods (a split shift),
# with a late-night window on Fri/Sat that runs past midnight.
STANDARD_HOURS = [
    *[(day, time(12, 0), time(15, 30), False) for day in range(0, 7)],
    *[(day, time(18, 30), time(23, 30), False) for day in (0, 1, 2, 3, 6)],
    *[(day, time(18, 30), time(1, 30), True) for day in (4, 5)],  # Fri/Sat till 1:30am
]


def seed() -> None:
    db = SessionLocal()
    try:
        plans_by_name: dict[str, SubscriptionPlan] = {}
        for spec in PLANS:
            plan = db.scalar(select(SubscriptionPlan).where(SubscriptionPlan.name == spec["name"]))
            if plan is None:
                plan = SubscriptionPlan(**spec)
                db.add(plan)
                db.flush()
                print(f"seeded plan {plan.name} (Rs. {plan.monthly_fee}/mo)")
            plans_by_name[plan.name] = plan

        for spec in DEMO:
            fields = {
                "name": spec["name"],
                "description": spec["description"],
                "phone": spec["phone"],
                "address": spec["address"],
                "cuisine_type": spec["cuisine_type"],
                "status": RestaurantStatus.ACTIVE,
                "commission_rate": spec["commission_rate"],
                "delivery_fee": spec["delivery_fee"],
                "min_order_amount": spec["min_order_amount"],
                "is_accepting_orders": True,
                "subscription_plan_id": plans_by_name[spec["plan"]].id,
            }

            restaurant = db.scalar(select(Restaurant).where(Restaurant.name == spec["name"]))
            if restaurant is None:
                restaurant = Restaurant(**fields)
                db.add(restaurant)
                db.flush()
            else:
                # Update in place. Deleting and recreating would blow up now that these
                # restaurants have orders: orders.restaurant_id is ON DELETE RESTRICT,
                # deliberately, so demo data can never destroy order history.
                for field, value in fields.items():
                    setattr(restaurant, field, value)
                db.flush()

                # The menu and schedule ARE rebuilt each run, so edits made while
                # demoing get reset to a known-good state.
                db.execute(
                    delete(RestaurantWorkingHours).where(
                        RestaurantWorkingHours.restaurant_id == restaurant.id
                    )
                )
                # Menu items are detached from past orders (menu_item_id is SET NULL and
                # order_items keeps its own name/price snapshot), so this is safe.
                db.execute(delete(MenuItem).where(MenuItem.restaurant_id == restaurant.id))
                db.execute(
                    delete(MenuCategory).where(MenuCategory.restaurant_id == restaurant.id)
                )
                db.flush()

            for day, opens, closes, overnight in STANDARD_HOURS:
                db.add(
                    RestaurantWorkingHours(
                        restaurant_id=restaurant.id,
                        day_of_week=day,
                        opens_at=opens,
                        closes_at=closes,
                        crosses_midnight=overnight,
                    )
                )

            # Staff survive a re-seed (unique email), so only create the owner once.
            owner = db.scalar(
                select(RestaurantStaff).where(RestaurantStaff.email == spec["staff_email"])
            )
            if owner is None:
                db.add(
                    RestaurantStaff(
                        restaurant_id=restaurant.id,
                        name=f"{spec['name']} Owner",
                        email=spec["staff_email"],
                        phone=spec["phone"],
                        password_hash=hash_password(DEMO_PASSWORD),
                        role=StaffRole.OWNER,
                    )
                )

            for order_index, (category_name, items) in enumerate(spec["menu"].items()):
                category = MenuCategory(
                    restaurant_id=restaurant.id, name=category_name, sort_order=order_index
                )
                db.add(category)
                db.flush()

                for item_index, (name, description, price) in enumerate(items):
                    db.add(
                        MenuItem(
                            restaurant_id=restaurant.id,
                            category_id=category.id,
                            name=name,
                            description=description,
                            price=Decimal(price),
                            sort_order=item_index,
                        )
                    )

            print(f"seeded {restaurant.name} ({len(spec['menu'])} categories)")

        admin_email = "admin@abhiaya.pk"
        if not db.scalar(select(AdminUser).where(AdminUser.email == admin_email)):
            db.add(
                AdminUser(
                    name="AbhiAya Admin",
                    email=admin_email,
                    password_hash=hash_password(DEMO_PASSWORD),
                )
            )
            print(f"seeded admin {admin_email}")

        db.commit()
        print(f"\ndone. all demo logins use password: {DEMO_PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
