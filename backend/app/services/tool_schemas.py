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
            "name": "search_restaurants_by_item",
            "description": (
                "Find open restaurants whose menu has an item matching a dish name, "
                "e.g. 'biryani', 'pizza', 'chowmein'. Call this as soon as the customer "
                "says what they want to eat, BEFORE asking them to pick a restaurant — "
                "never guess which restaurants serve something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The dish or food keyword the customer said, e.g. 'chicken biryani'.",
                    }
                },
                "required": ["query"],
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
            "name": "list_active_deals",
            "description": (
                "Check whether a restaurant has any promotional deals running RIGHT "
                "NOW (e.g. '20% off biryani this weekend', 'Rs. 500 off orders over "
                "Rs. 2000'). Use this when the customer picks a restaurant, or asks "
                "'any deals?', or when a deal would be relevant marketing (e.g. after "
                "showing the menu, mention any active deal once). Returns 0-N deals "
                "with a title, human-readable discount string, and the date window. "
                "The deal is informational — quote the title and discount verbatim, "
                "but don't promise a specific price at checkout (the discount is not "
                "yet auto-applied at place_order)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant_id": {
                        "type": "string",
                        "description": "The restaurant's id from list_restaurants.",
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
            "name": "suggest_addons",
            "description": (
                "Get ONE contextual upsell for the cart's current restaurant, or "
                "nothing. Call this right after a successful add_to_cart, at most "
                "ONCE per customer turn — never twice in the same turn, never "
                "before the cart has any items. Response has a `suggestion_type`: "
                "'promotion' means an active restaurant deal you should mention "
                "(quote the title + discount verbatim, do not promise a checkout "
                "price); 'addon' means a complementary menu item you should offer "
                "as a light nudge like 'want some Loaded Fries with that?'; "
                "'none' means say nothing about upsells — do NOT invent something. "
                "Never suggest twice in a row if the customer has already declined."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
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
                            "How the customer wants to pay. Must match what the customer "
                            "explicitly chose when you asked them (only ask if more than one "
                            "payment method is available; otherwise use the only one). "
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
                "required": ["payment_method"],
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
            "name": "find_past_order",
            "description": (
                "Search this customer's own past orders for a keyword the customer just "
                "used — an item name (e.g. 'biryani', 'zinger'), a restaurant, or a "
                "phrase from their own order notes (e.g. 'Eid', 'office lunch'). Use "
                "this WHENEVER the customer references a specific past order in words, "
                "before calling reorder_last. Returns 0-5 candidate orders newest-first "
                "with items, totals, and the placed_at date. If exactly one candidate "
                "comes back, confirm it with the customer and then reorder. If more "
                "than one, ask a clarifying question (mention the two most recent). If "
                "none, offer to build a fresh order — never guess."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "One keyword or short phrase from the customer's own words: "
                            "an item name, a restaurant, or a snippet from their past "
                            "order notes."
                        ),
                    },
                },
                "required": ["query"],
            },
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
