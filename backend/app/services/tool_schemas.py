"""OpenAI-compatible tool schemas handed to Groq.

Kept apart from tools.py so the descriptions — which are effectively prompt text
and get tuned often — don't churn the implementation file.
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_restaurants",
            "description": (
                "List restaurants that are currently open and accepting orders. "
                "Call this when the customer asks what's available, or names a cuisine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cuisine": {
                        "type": "string",
                        "description": "Optional cuisine filter, e.g. 'pizza', 'desi', 'bbq'.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_menu",
            "description": (
                "Get the available menu items for one restaurant. Always call this "
                "before adding items to the cart — never guess item ids or prices. "
                "Identify the restaurant by id OR by name, whichever you have."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    # Typed as string, not integer: the model persistently sends the
                    # NAME here, and a type mismatch makes Groq reject the entire call
                    # as malformed. Accepting either keeps the turn alive; the backend
                    # resolves whatever arrives.
                    "restaurant_id": {
                        "type": "string",
                        "description": "The restaurant's id from list_restaurants, e.g. '3'.",
                    },
                    "restaurant_name": {
                        "type": "string",
                        "description": "The restaurant's name, e.g. 'Pizza Junction'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": (
                "Add one menu item to the customer's cart. Call once per distinct item. "
                "The menu_item_id must come from get_menu."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "menu_item_id": {"type": "integer", "description": "Item id from get_menu."},
                    "quantity": {"type": "integer", "description": "How many. Defaults to 1."},
                    "notes": {
                        "type": "string",
                        "description": "Customer's special request, e.g. 'extra spicy', 'no onions'.",
                    },
                },
                "required": ["menu_item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_cart",
            "description": (
                "Empty the cart. Use when the customer wants to start over, or when they "
                "confirm switching to a different restaurant mid-order."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": (
                "Place the order for everything in the cart. Only call this AFTER you have "
                "read the full order back to the customer with the total and they have "
                "explicitly confirmed. If no delivery address is known, ask for one first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "delivery_address": {
                        "type": "string",
                        "description": "Delivery address, if the customer gave one in this chat.",
                    },
                    "payment_method": {
                        "type": "string",
                        "enum": ["cod", "jazzcash", "easypaisa"],
                        "description": (
                            "How the customer wants to pay. Defaults to cash on delivery. "
                            "If they choose jazzcash or easypaisa the order is NOT confirmed "
                            "until they pay — the tool result will include a payment_link "
                            "you must send them."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Any order-level note for the restaurant or rider.",
                    },
                    "coupon_code": {
                        "type": "string",
                        "description": (
                            "A coupon code the customer wants to apply, if they mentioned one. "
                            "Omit if they did not mention a code — never invent one."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": (
                "Look up an order's status. With no order_number, returns the customer's "
                "most recent order — which is what 'where's my order?' usually means."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_number": {
                        "type": "string",
                        "description": "Order reference like 'AB-4F2K9C'. Omit for the latest order.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_favorite",
            "description": (
                "Save a restaurant to the customer's favorites, e.g. when they say "
                "'save this one' or 'add to favorites'. Identify by id or by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant_id": {
                        "type": "string",
                        "description": "The restaurant's id, e.g. '3'.",
                    },
                    "restaurant_name": {
                        "type": "string",
                        "description": "The restaurant's name, e.g. 'Pizza Junction'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_favorite",
            "description": "Remove a restaurant from the customer's favorites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant_id": {
                        "type": "string",
                        "description": "The restaurant's id, e.g. '3'.",
                    },
                    "restaurant_name": {
                        "type": "string",
                        "description": "The restaurant's name, e.g. 'Pizza Junction'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_favorites",
            "description": (
                "List the customer's saved favorite restaurants. Call when they ask "
                "'what are my favorites?' or want to order from one again."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reorder_last",
            "description": (
                "Rebuild the cart from the customer's most recent order, e.g. 'order the "
                "same as last time' or 'get me my usual'. Prices and availability are "
                "re-checked against the current menu — always tell the customer about "
                "any item that could not be re-added before confirming the order."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]
