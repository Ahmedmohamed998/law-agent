/**
 * Paymob: order creation and callback verification.
 *
 * Two things here are the entire security surface of taking money, and both
 * are easy to get subtly wrong in ways that only show up in production.
 *
 * 1. THE HMAC IS OVER A FIXED FIELD ORDER, NOT THE BODY.
 *    Paymob does not sign the raw payload. It concatenates twenty specific
 *    fields, in one specific order, and HMAC-SHA512s that string. Get the
 *    order wrong, coerce a boolean to "True" instead of "true", or drop a
 *    null, and every signature fails — with no indication which of the twenty
 *    is the problem. The order below is Paymob's documented transaction
 *    callback order and must not be "tidied".
 *
 * 2. THE CALLBACK IS NOT AUTHENTICATED ANY OTHER WAY.
 *    There is no bearer token on a webhook; the gateway does not know our
 *    users. The HMAC *is* the authentication. A handler that parses the
 *    payload before verifying is a handler that can be driven by anyone who
 *    finds the URL.
 *
 * Live calls to Paymob need real credentials. Everything else — signature
 * verification, idempotency, the escalation hop — is exercised by
 * `npm run webhook:simulate` without them, which is deliberate: the parts that
 * can lose money or leak access should be testable without a payment provider.
 */

import { Injectable, Logger } from "@nestjs/common";
import { createHmac, timingSafeEqual } from "node:crypto";

export const PAYMOB_BASE = "https://ksa.paymob.com/api";

/**
 * The exact fields Paymob concatenates for a transaction callback, in the
 * exact order. Do not sort, do not reorder, do not "clean up".
 */
export const HMAC_FIELDS = [
  "amount_cents",
  "created_at",
  "currency",
  "error_occured",
  "has_parent_transaction",
  "id",
  "integration_id",
  "is_3d_secure",
  "is_auth",
  "is_capture",
  "is_refunded",
  "is_standalone_payment",
  "is_voided",
  "order.id",
  "owner",
  "pending",
  "source_data.pan",
  "source_data.sub_type",
  "source_data.type",
  "success",
] as const;

export interface PaymobTransaction {
  id: number | string;
  success?: boolean;
  pending?: boolean;
  is_refunded?: boolean;
  is_voided?: boolean;
  amount_cents?: number | string;
  currency?: string;
  order?: { id?: number | string; merchant_order_id?: string | null };
  [key: string]: unknown;
}

function pluck(obj: Record<string, unknown>, path: string): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (acc, part) =>
        acc && typeof acc === "object"
          ? (acc as Record<string, unknown>)[part]
          : undefined,
      obj,
    );
}

/**
 * Stringify a field the way Paymob does before hashing.
 *
 * Booleans are lowercase `true`/`false` — JavaScript's default, and the one
 * place a Python port would differ, since Python renders `True`. Null and
 * undefined become the empty string rather than "null", because the field is
 * absent from the concatenation, not present as the word null.
 */
export function hmacValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

@Injectable()
export class PaymobService {
  private static readonly log = new Logger(PaymobService.name);

  private get apiKey(): string {
    return process.env.PAYMOB_API_KEY ?? "";
  }

  private get hmacSecret(): string {
    return process.env.PAYMOB_HMAC_SECRET ?? "";
  }

  private get integrationId(): string {
    return process.env.PAYMOB_INTEGRATION_ID ?? "";
  }

  get configured(): boolean {
    return Boolean(this.apiKey && this.hmacSecret && this.integrationId);
  }

  /** Build the string Paymob signs, for one transaction object. */
  concatenatedFor(transaction: Record<string, unknown>): string {
    return HMAC_FIELDS.map((f) => hmacValue(pluck(transaction, f))).join("");
  }

  /**
   * Is this callback really from Paymob?
   *
   * Constant-time comparison: a plain `!==` on a hex digest leaks it a
   * character at a time to anyone willing to measure enough requests, and a
   * forged callback marks a consultation paid.
   */
  verifyHmac(transaction: Record<string, unknown>, provided: string): boolean {
    if (!this.hmacSecret || !provided) return false;

    const expected = createHmac("sha512", this.hmacSecret)
      .update(this.concatenatedFor(transaction), "utf8")
      .digest("hex");

    const a = Buffer.from(expected, "utf8");
    const b = Buffer.from(provided.toLowerCase(), "utf8");
    return a.length === b.length && timingSafeEqual(a, b);
  }

  // ── outbound: creating a checkout ─────────────────────────────────────
  //
  // Paymob's classic flow is three calls: authenticate, create an order, then
  // exchange it for a payment key the hosted iframe accepts.

  private async post<T>(path: string, body: unknown): Promise<T> {
    const response = await fetch(`${PAYMOB_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`paymob ${path} -> ${response.status}: ${text.slice(0, 300)}`);
    }
    return (await response.json()) as T;
  }

  private async authenticate(): Promise<string> {
    const { token } = await this.post<{ token: string }>("/auth/tokens", {
      api_key: this.apiKey,
    });
    return token;
  }

  /**
   * Create an order and return the payment key the frontend hands to the
   * hosted checkout.
   *
   * `merchantOrderId` is our consultation id, echoed back on the callback. It
   * is how a webhook — which knows nothing about our users — is matched to the
   * thing that was bought.
   */
  async createCheckout(params: {
    merchantOrderId: string;
    amountCents: number;
    currency: string;
    billing: Record<string, string>;
  }): Promise<{ paymentKey: string; orderId: string }> {
    if (!this.configured) {
      throw new Error(
        "Paymob is not configured — set PAYMOB_API_KEY, PAYMOB_HMAC_SECRET " +
          "and PAYMOB_INTEGRATION_ID in backend/.env",
      );
    }

    const authToken = await this.authenticate();

    const order = await this.post<{ id: number }>("/ecommerce/orders", {
      auth_token: authToken,
      delivery_needed: false,
      amount_cents: String(params.amountCents),
      currency: params.currency,
      merchant_order_id: params.merchantOrderId,
      items: [],
    });

    const key = await this.post<{ token: string }>("/acceptance/payment_keys", {
      auth_token: authToken,
      amount_cents: String(params.amountCents),
      expiration: 3600,
      order_id: String(order.id),
      billing_data: params.billing,
      currency: params.currency,
      integration_id: Number(this.integrationId),
    });

    PaymobService.log.log(
      `paymob order ${order.id} created for consultation ${params.merchantOrderId}`,
    );
    return { paymentKey: key.token, orderId: String(order.id) };
  }
}
