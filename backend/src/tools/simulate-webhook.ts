/**
 * Send a Paymob-shaped callback, correctly signed with the local HMAC secret.
 *
 *   npx tsx src/tools/simulate-webhook.ts <consultationId> [--fail] [--bad-hmac]
 *
 * The parts of taking money that can actually lose money — signature
 * verification, idempotency, the escalation hop — should be testable without a
 * payment provider. This builds the concatenation exactly as PaymobService
 * does, so if the field order is wrong, this and production are wrong
 * together and the test would catch a mismatch against nothing.
 *
 * That is the honest limitation: this proves our handling is self-consistent
 * and idempotent, NOT that our field order matches Paymob's. Only a real
 * sandbox callback proves that.
 */

import "dotenv/config";

import { createHmac } from "node:crypto";

import { HMAC_FIELDS, hmacValue } from "../billing/paymob.service";

function pluck(obj: Record<string, unknown>, path: string): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (acc, part) =>
        acc && typeof acc === "object" ? (acc as Record<string, unknown>)[part] : undefined,
      obj,
    );
}

async function main(): Promise<void> {
  const [consultationId, ...flags] = process.argv.slice(2);
  if (!consultationId) {
    console.error("usage: simulate-webhook.ts <consultationId> [--fail] [--bad-hmac] [--txn <id>]");
    process.exit(2);
  }

  const shouldFail = flags.includes("--fail");
  const badHmac = flags.includes("--bad-hmac");
  const txnIndex = flags.indexOf("--txn");
  const txnId =
    txnIndex !== -1 && flags[txnIndex + 1]
      ? flags[txnIndex + 1]
      : String(Math.floor(Math.random() * 1_000_000_000));

  const transaction: Record<string, unknown> = {
    id: txnId,
    amount_cents: 50000,
    created_at: new Date().toISOString(),
    currency: "EGP",
    error_occured: false,
    has_parent_transaction: false,
    integration_id: Number(process.env.PAYMOB_INTEGRATION_ID ?? 1),
    is_3d_secure: true,
    is_auth: false,
    is_capture: false,
    is_refunded: false,
    is_standalone_payment: true,
    is_voided: false,
    order: { id: Math.floor(Math.random() * 1_000_000), merchant_order_id: consultationId },
    owner: 1,
    pending: false,
    source_data: { pan: "2346", sub_type: "MasterCard", type: "card" },
    success: !shouldFail,
  };

  const concatenated = HMAC_FIELDS.map((f) => hmacValue(pluck(transaction, f))).join("");
  const secret = process.env.PAYMOB_HMAC_SECRET ?? "";
  if (!secret) {
    console.error("PAYMOB_HMAC_SECRET is not set in backend/.env — set any value to simulate");
    process.exit(2);
  }

  const hmac = badHmac
    ? "0".repeat(128)
    : createHmac("sha512", secret).update(concatenated, "utf8").digest("hex");

  const port = process.env.PORT ?? "8001";
  const url = `http://127.0.0.1:${port}/webhooks/paymob?hmac=${hmac}`;

  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "TRANSACTION", obj: transaction }),
  });

  console.log(`txn ${txnId} -> ${response.status}`);
  console.log(await response.text());
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
