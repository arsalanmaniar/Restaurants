export type OrderStatus =
  | "awaiting_payment"
  | "pending"
  | "accepted"
  | "preparing"
  | "ready"
  | "out_for_delivery"
  | "delivered"
  | "cancelled";

export type RestaurantStatus = "pending" | "active" | "suspended";

export interface LoginResponse {
  access_token: string;
  token_type: string;
  role: "admin" | "restaurant";
  name: string;
  restaurant_id?: number | null;
  restaurant_name?: string | null;
}

export interface Category {
  id: number;
  name: string;
  sort_order: number;
  is_active: boolean;
}

export interface MenuItem {
  id: number;
  category_id: number | null;
  name: string;
  description: string | null;
  price: string;
  image_url: string | null;
  is_available: boolean;
  sort_order: number;
}

export interface OrderItem {
  id: number;
  item_name: string;
  quantity: number;
  price_at_order: string;
  line_total: string;
  notes: string | null;
}

export interface Order {
  id: number;
  order_number: string;
  status: OrderStatus;
  subtotal: string;
  delivery_fee: string;
  total_amount: string;
  commission_amount: string;
  delivery_address_text: string | null;
  notes: string | null;
  placed_at: string;
  items: OrderItem[];
}

export interface AdminOrder extends Order {
  restaurant_id: number;
  restaurant_name: string;
  customer_number: string;
}

export interface Restaurant {
  id: number;
  name: string;
  description: string | null;
  phone: string;
  address: string | null;
  cuisine_type: string | null;
  status: RestaurantStatus;
  commission_rate: string;
  delivery_fee: string;
  min_order_amount: string;
  is_accepting_orders: boolean;
  subscription_plan_id: number | null;
  created_at: string;
}

export interface RestaurantSummary extends Restaurant {
  order_count: number;
  total_revenue: string;
  total_commission: string;
}

export interface OwnerCreated {
  email: string;
}

export interface RestaurantCreateResponse {
  restaurant: Restaurant;
  owner: OwnerCreated;
}

export interface RestaurantStats {
  orders_24h: number;
  revenue_24h: string;
  active_orders: number;
}

export interface PlatformStats {
  total_orders: number;
  gross_revenue: string;
  platform_commission: string;
  orders_today: number;
  revenue_today: string;
  commission_today: string;
  orders_7d: number;
  revenue_7d: string;
  commission_7d: string;
  active_restaurants: number;
  pending_approval: number;
  total_customers: number;
}

export interface WorkingHoursPeriod {
  id?: number;
  day_of_week: number; // 0 = Monday … 6 = Sunday
  opens_at: string; // "12:00:00"
  closes_at: string;
  crosses_midnight: boolean;
}

export const DAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];

export interface OpenState {
  is_open: boolean;
  is_accepting_orders: boolean;
  has_schedule: boolean;
}

export interface SubscriptionPlan {
  id: number;
  name: string;
  description: string | null;
  monthly_fee: string;
  commission_rate: string | null;
  features: string[];
  is_active: boolean;
  sort_order: number;
  restaurant_count: number;
}

export interface Rating {
  id: number;
  order_id: number;
  rating: number;
  comment: string | null;
  source: string;
  created_at: string;
  order_number: string;
  customer_number: string;
}

export type RefundStatus = "pending" | "completed" | "failed";

export interface Refund {
  id: number;
  order_id: number;
  payment_id: number | null;
  amount: string;
  reason: string;
  status: RefundStatus;
  issued_by: number;
  provider_ref: string | null;
  failure_reason: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface OrderRefundState {
  order_number: string;
  total_amount: string;
  amount_paid: string;
  amount_refunded: string;
  refundable: string;
  refunds: Refund[];
}

export interface RatingSummary {
  average: number | null;
  count: number;
  breakdown: Record<string, number>;
}

export const RESTAURANT_STATUS_LABELS: Record<RestaurantStatus, string> = {
  pending: "Awaiting approval",
  active: "Live",
  suspended: "Suspended",
};

/** Which statuses a restaurant may move an order to. Mirrors ALLOWED_TRANSITIONS
 *  in backend/app/api/restaurant.py — the backend is the real enforcer; this just
 *  keeps the UI from offering buttons that would 409. */
export const NEXT_STATUSES: Record<OrderStatus, OrderStatus[]> = {
  // A restaurant can do nothing with an unpaid order — it never even sees one.
  awaiting_payment: [],
  pending: ["accepted", "cancelled"],
  accepted: ["preparing", "cancelled"],
  preparing: ["ready", "cancelled"],
  ready: ["out_for_delivery", "delivered"],
  out_for_delivery: ["delivered"],
  delivered: [],
  cancelled: [],
};

export type CouponDiscountType = "percentage" | "fixed";

export interface Coupon {
  id: number;
  code: string;
  discount_type: CouponDiscountType;
  value: string;
  restaurant_id: number | null;
  restaurant_name: string | null;
  min_order_amount: string;
  max_discount_amount: string | null;
  usage_limit: number | null;
  valid_from: string | null; // "2026-07-14"
  valid_to: string | null;
  is_active: boolean;
  times_redeemed: number;
  created_at: string;
}

export interface ReportCategory {
  name: string;
  revenue: string;
  order_count: number;
}

export interface ReportItem {
  name: string;
  revenue: string;
  quantity_sold: number;
}

export interface Report {
  gross_sales: string;
  cancelled_amount: string;
  net_sales: string;
  cash_amount: string;
  online_amount: string;
  order_count: number;
  customer_count: number;
  avg_order_amount: string;
  // No delivery-vs-pickup column exists on the order model — every order is a
  // delivery today, so both are always 0. See the backend report notes.
  delivery_count: number;
  pickup_count: number;
  top_categories: ReportCategory[];
  top_items: ReportItem[];
}

export const STATUS_LABELS: Record<OrderStatus, string> = {
  awaiting_payment: "Awaiting payment",
  pending: "New",
  accepted: "Accepted",
  preparing: "Preparing",
  ready: "Ready",
  out_for_delivery: "Out for delivery",
  delivered: "Delivered",
  cancelled: "Cancelled",
};
