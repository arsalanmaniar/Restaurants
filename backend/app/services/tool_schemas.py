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
                "DEPRECATED — prefer `find_restaurants`, which is a strict "
                "superset (searches restaurant name/cuisine/description AND "
                "menu item name/description, not just item names). Kept "
                "callable for backward compatibility; do not call it for new "
                "discovery flows."
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
            "name": "find_restaurants",
            "description": (
                "Intent-based discovery — one tool for almost every 'what do "
                "you have?' style query. Call this whenever the customer "
                "names a dish ('biryani', 'pizza chahiye'), a cuisine "
                "('chinese', 'desi'), a style ('something spicy', 'light "
                "dinner', 'family meal'), or any topical phrase, BEFORE "
                "asking them to pick a restaurant. Searches restaurant name, "
                "cuisine, description, menu item name AND menu item "
                "description in one go — so 'chinese' finds Wok & Roll even "
                "though no menu item literally contains the word 'chinese'. "
                "Returns ranked, open restaurants with matched_items (real "
                "menu names when available, otherwise the cuisine text). If "
                "it returns zero, fall back to list_restaurants rather than "
                "telling the customer 'we have nothing'.\n\n"
                "When the customer mentions a budget (e.g. 'Rs. 1500 mein', "
                "'under 1000', 'budget 800') OR a party size (e.g. '6 logo "
                "ke liye', 'family of 4'), pass `budget` and `party_size` "
                "too — the response will then include a per-restaurant "
                "`estimate` (cheapest matched item × party_size + delivery, "
                "clamped to the restaurant's minimum order) and a "
                "`fits_budget` flag. If NOTHING fits, a top-level `note` "
                "tells you the cheapest option; quote that honestly and "
                "offer to raise the budget — never pretend an option fits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A keyword or short phrase from the customer's own "
                            "words — dish, cuisine, style, or intent. Optional "
                            "ONLY when `budget` is also set (bare-budget query)."
                        ),
                    },
                    "budget": {
                        "type": "number",
                        "description": (
                            "Optional. Total budget in Pakistani Rupees the "
                            "customer mentioned, e.g. 1500 for 'Rs. 1500 mein "
                            "kya milega' or 'under 1500'. The estimate this "
                            "is compared against INCLUDES delivery fee."
                        ),
                    },
                    "party_size": {
                        "type": "integer",
                        "description": (
                            "Optional. Number of people if the customer "
                            "mentioned it — '6 logo ke liye' → 6, 'family of "
                            "4' → 4, 'mere aur meri behen' → 2. Defaults to 1."
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
            "name": "preview_bill",
            "description": (
                "Get the exact bill for the current cart BEFORE placing the order: "
                "subtotal, tax, delivery fee, and total. READ ONLY — it places "
                "nothing. The tax rate depends on how the customer pays (cash is "
                "taxed higher than online), so call this ONLY AFTER the customer "
                "has chosen a payment method, passing that method. Read the "
                "returned numbers back to the customer as the order summary, then "
                "get their confirmation, then call place_order with the SAME "
                "payment method. Never compute subtotal/tax/total yourself — always "
                "use the numbers this returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payment_method": {
                        "type": "string",
                        "enum": ["cod", "jazzcash", "easypaisa"],
                        "description": (
                            "The method the customer chose. Determines the tax rate "
                            "(cod is taxed higher than online), so the total changes "
                            "with it. Defaults to cod."
                        ),
                    },
                    "coupon_code": {
                        "type": "string",
                        "description": (
                            "A coupon code the customer wants applied, if they "
                            "mentioned one, so the preview reflects the discount. "
                            "Omit if none."
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
                    "contact_name": {
                        "type": "string",
                        "description": (
                            "Name of the person receiving the delivery, if the "
                            "customer gave one. Omit if not provided."
                        ),
                    },
                    "contact_phone": {
                        "type": "string",
                        "description": (
                            "Contact mobile number for the delivery, if the customer "
                            "gave one. This CAN differ from their WhatsApp number "
                            "(a landline, or someone else's number for the receiver). "
                            "Omit if they didn't give a separate number — the WhatsApp "
                            "number is used by default."
                        ),
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
                    "link_to_order_number": {
                        "type": "string",
                        "description": (
                            "Optional. Order number of a PREVIOUS order in "
                            "this same conversation that the customer wants "
                            "this order LINKED to (e.g. 'AB-4F2K9C'). Use "
                            "ONLY when the customer explicitly wants a "
                            "second order from a different restaurant in "
                            "the same session ('aur pizza junction se bhi', "
                            "'and add a biryani from Karachi Biryani'). "
                            "The two orders stay independent (own delivery, "
                            "own payment, own status) but share an "
                            "order_group_id so support and dashboards can "
                            "see they belong together. Omit for standalone "
                            "orders — this is NOT the default."
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
