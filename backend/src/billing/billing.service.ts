/**
 * Consultations, payments, and the hop that closes the product loop.
 *
 * The ordering in `handleWebhook` is the part that matters. A payment webhook
 * has to do three things — record that it arrived, record the money, and tell
 * the AI service to hand the conversation to a lawyer — and they have very
 * different failure characteristics:
 *
 *   * Recording is local and reliable.
 *   * Escalation is a cross-service HTTP call that can time out.
 *
 * So the money is committed FIRST and the escalation attempted after, with its
 * outcome written back. If escalation fails, we have a paid consultation with
 * `escalated_at IS NULL` — visible, retryable, and monitorable. If it were
 * done the other way round, a timeout would roll back a payment the gateway
 * has already taken.
 */

import { Injectable, Logger } from "@nestjs/common";

import { AiService } from "../ai/ai.service";
import { conflict, notFound } from "../common/errors";
import { newId } from "../common/ids";
import { PrismaService } from "../prisma/prisma.service";
import { PaymobService, type PaymobTransaction } from "./paymob.service";

const PROVIDER = "paymob";

export interface ConsultationOut {
  consultation_id: string;
  status: string;
  amount_cents: number;
  currency: string;
  ai_session_id: string | null;
  created_at: Date;
  paid_at: Date | null;
  escalated: boolean;
  escalation_error?: string | null;
  chat_language?: string | null;
  escalation_summary?: string | null;
  user?: any;
}

@Injectable()
export class BillingService {
  private static readonly log = new Logger(BillingService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly paymob: PaymobService,
    private readonly ai: AiService,
  ) {}

  private shape(row: {
    id: string;
    status: string;
    amount_cents: number;
    currency: string;
    ai_session_id: string | null;
    created_at: Date;
    paid_at: Date | null;
    escalated_at: Date | null;
    chat_language: string | null;
    escalation_summary: string | null;
  }): ConsultationOut {
    return {
      consultation_id: row.id,
      status: row.status,
      amount_cents: row.amount_cents,
      currency: row.currency,
      ai_session_id: row.ai_session_id,
      created_at: row.created_at,
      paid_at: row.paid_at,
      escalated: row.escalated_at !== null,
      escalation_error: null, // Basic shape doesn't include it
      chat_language: row.chat_language,
      escalation_summary: row.escalation_summary,
      user: null,
    };
  }

  /**
   * Start a consultation. Creates the row, then asks Paymob for a checkout.
   *
   * The row exists before the gateway is contacted so the callback always has
   * something to match against — a webhook arriving for an order we have no
   * record of is unresolvable, and gateways can be faster than you expect.
   */
  async createConsultation(params: {
    userId: string;
    aiSessionId?: string;
    amountCents: number;
    currency: string;
    billing: Record<string, string>;
  }): Promise<{ consultation: ConsultationOut; paymentKey: string | null }> {
    const row = await this.prisma.consultations.create({
      data: {
        id: newId(),
        user_id: params.userId,
        ai_session_id: params.aiSessionId ?? null,
        amount_cents: params.amountCents,
        currency: params.currency,
        status: "pending",
      },
    });

    let paymentKey: string | null = null;
    if (this.paymob.configured) {
      const checkout = await this.paymob.createCheckout({
        merchantOrderId: row.id,
        amountCents: params.amountCents,
        currency: params.currency,
        billing: params.billing,
      });
      paymentKey = checkout.paymentKey;
    } else {
      // Without credentials the consultation is still created, so the whole
      // flow after checkout can be exercised. Loud, because silently returning
      // no payment key would look like a frontend bug.
      BillingService.log.warn(
        "Paymob not configured — consultation created without a payment key",
      );
    }

    return { consultation: this.shape(row), paymentKey };
  }

  async listForUser(userId: string): Promise<ConsultationOut[]> {
    const rows = await this.prisma.consultations.findMany({
      where: { user_id: userId },
      orderBy: { created_at: "desc" },
      take: 50,
    });
    return rows.map((r) => this.shape(r));
  }

  async getForUser(userId: string, id: string): Promise<ConsultationOut> {
    const row = await this.prisma.consultations.findFirst({
      where: { id, user_id: userId },
    });
    // Same 404 whether it never existed or belongs to someone else.
    if (!row) throw notFound("consultation_not_found", "no such consultation");
    return this.shape(row);
  }

  // ── the webhook ───────────────────────────────────────────────────────

  /**
   * Process a verified Paymob callback.
   *
   * Assumes the signature has ALREADY been checked by the controller — this
   * method trusts its input, and it must never be reachable with unverified
   * data.
   */
  async handleWebhook(
    transaction: PaymobTransaction,
    signatureValid: boolean,
    rawPayload: unknown,
  ): Promise<{ status: string; consultation_id: string | null }> {
    const txnId = String(transaction.id);
    const orderId = transaction.order?.id ? String(transaction.order.id) : null;
    const merchantOrderId = transaction.order?.merchant_order_id ?? null;

    // Record the delivery first, and record failed signatures too: "we never
    // got it" and "we got it and rejected it" are different conversations to
    // have with a payment provider, and only one of them is your fault.
    const event = await this.prisma.webhook_events.upsert({
      where: {
        provider_provider_event_id: {
          provider: PROVIDER,
          provider_event_id: txnId,
        },
      },
      create: {
        id: newId(),
        provider: PROVIDER,
        provider_event_id: txnId,
        signature_valid: signatureValid,
        payload: rawPayload as never,
      },
      update: {},
    });

    if (event.processed_at) {
      // A retry of something already handled. Gateways do this routinely.
      BillingService.log.log(`webhook ${txnId} already processed, ignoring`);
      const existing = await this.prisma.payments.findUnique({
        where: {
          provider_provider_txn_id: { provider: PROVIDER, provider_txn_id: txnId },
        },
      });
      return { status: "duplicate", consultation_id: existing?.consultation_id ?? null };
    }

    if (!merchantOrderId) {
      await this.failEvent(event.id, "callback carried no merchant_order_id");
      return { status: "unmatched", consultation_id: null };
    }

    const consultation = await this.prisma.consultations.findUnique({
      where: { id: merchantOrderId },
    });
    if (!consultation) {
      await this.failEvent(event.id, `no consultation ${merchantOrderId}`);
      return { status: "unmatched", consultation_id: null };
    }

    const succeeded =
      transaction.success === true &&
      transaction.pending !== true &&
      transaction.is_refunded !== true &&
      transaction.is_voided !== true;

    // The money. `skipDuplicates` semantics come from the unique constraint on
    // (provider, provider_txn_id) — the real idempotency guard, because two
    // concurrent deliveries would both pass the processed_at check above.
    try {
      await this.prisma.payments.create({
        data: {
          id: newId(),
          consultation_id: consultation.id,
          provider: PROVIDER,
          provider_order_id: orderId,
          provider_txn_id: txnId,
          amount_cents: Number(transaction.amount_cents ?? consultation.amount_cents),
          currency: String(transaction.currency ?? consultation.currency),
          status: succeeded ? "success" : "failed",
          raw: rawPayload as never,
        },
      });
    } catch (err) {
      if ((err as { code?: string }).code === "P2002") {
        BillingService.log.log(`payment ${txnId} already recorded, ignoring`);
        await this.prisma.webhook_events.update({
          where: { id: event.id },
          data: { processed_at: new Date() },
        });
        return { status: "duplicate", consultation_id: consultation.id };
      }
      throw err;
    }

    if (!succeeded) {
      await this.prisma.webhook_events.update({
        where: { id: event.id },
        data: { processed_at: new Date() },
      });
      return { status: "failed", consultation_id: consultation.id };
    }

    await this.prisma.consultations.update({
      where: { id: consultation.id },
      data: { status: "paid", paid_at: new Date() },
    });

    // Money is committed. Now the cross-service call — after, never inside,
    // so a timeout cannot roll back a payment the gateway already took.
    await this.escalate(consultation.id, consultation.ai_session_id);

    await this.prisma.webhook_events.update({
      where: { id: event.id },
      data: { processed_at: new Date() },
    });

    return { status: "paid", consultation_id: consultation.id };
  }

  /**
   * Record a callback whose signature did not verify, and do nothing else.
   *
   * Kept separate from `handleWebhook` so there is no code path where an
   * unverified payload reaches the money. The row is the point: a forged or
   * misconfigured callback should be visible afterwards, not silently dropped.
   */
  async handleWebhookRejection(
    transaction: PaymobTransaction,
    rawPayload: unknown,
  ): Promise<void> {
    const txnId = String(transaction.id);
    BillingService.log.warn(`webhook ${txnId} rejected: bad HMAC`);
    await this.prisma.webhook_events.upsert({
      where: {
        provider_provider_event_id: { provider: PROVIDER, provider_event_id: txnId },
      },
      create: {
        id: newId(),
        provider: PROVIDER,
        provider_event_id: txnId,
        signature_valid: false,
        processed_at: new Date(),
        error: "hmac verification failed",
        payload: rawPayload as never,
      },
      update: {},
    });
  }

  private async failEvent(eventId: string, error: string): Promise<void> {
    BillingService.log.warn(`webhook ${eventId}: ${error}`);
    await this.prisma.webhook_events.update({
      where: { id: eventId },
      data: { processed_at: new Date(), error },
    });
  }

  /** Tell the AI service, and record whether it heard. */
  private async escalate(
    consultationId: string,
    sessionId: string | null,
  ): Promise<void> {
    if (!sessionId) {
      // A consultation bought outside a conversation is legitimate — there is
      // simply nothing to escalate.
      return;
    }
    const { delivered, error, data } = await this.ai.escalateSession(sessionId);
    await this.prisma.consultations.update({
      where: { id: consultationId },
      data: {
        escalated_at: delivered ? new Date() : null,
        escalation_error: delivered ? null : error,
        chat_language: data?.chat_language ?? null,
        escalation_summary: data?.summary ?? null,
      },
    });
  }

  /**
   * Re-attempt escalation for consultations that are paid but whose session
   * was never handed over.
   *
   * This is the queue that must not grow: money taken, service not delivered.
   */
  async retryEscalations(limit = 50): Promise<ConsultationOut[]> {
    const stuck = await this.prisma.consultations.findMany({
      where: { status: "paid", escalated_at: null, ai_session_id: { not: null } },
      orderBy: { paid_at: "asc" },
      take: Math.min(limit, 200),
    });

    for (const row of stuck) {
      await this.escalate(row.id, row.ai_session_id);
    }

    const refreshed = await this.prisma.consultations.findMany({
      where: { id: { in: stuck.map((s) => s.id) } },
    });
    return refreshed.map((r) => this.shape(r));
  }

  /** Paid but not handed over. Point a monitor at this. */
  async unescalated(): Promise<ConsultationOut[]> {
    const rows = await this.prisma.consultations.findMany({
      where: { status: "paid", escalated_at: null, ai_session_id: { not: null } },
      orderBy: { paid_at: "asc" },
      take: 200,
    });
    return rows.map((r) => this.shape(r));
  }

  /** Guard against double-buying the same conversation. */
  async assertNoOpenConsultation(userId: string, sessionId?: string): Promise<void> {
    if (!sessionId) return;
    const existing = await this.prisma.consultations.findFirst({
      where: { user_id: userId, ai_session_id: sessionId, status: { in: ["pending", "paid"] } },
    });
    if (existing) {
      throw conflict(
        "consultation_exists",
        "a consultation is already open for this conversation",
      );
    }
  }

  // ── Admin dashboard methods ────────────────────────────────────────────────

  /**
   * Full consultation list for the admin dashboard.
   * Joins user info and latest payment record for each consultation.
   */
  async listAll(params: {
    status?: string;
    limit?: number;
    offset?: number;
  }): Promise<AdminConsultationRow[]> {
    const where = params.status ? { status: params.status } : {};
    const rows = await this.prisma.consultations.findMany({
      where,
      orderBy: { created_at: "desc" },
      take: Math.min(params.limit ?? 100, 500),
      skip: params.offset ?? 0,
      include: BillingService.ADMIN_INCLUDE,
    });

    return rows.map((r) => BillingService.adminRow(r));
  }

  /** One consultation for the dashboard's case page, or 404. */
  async getForAdmin(id: string): Promise<AdminConsultationRow> {
    const row = await this.prisma.consultations.findUnique({
      where: { id },
      include: BillingService.ADMIN_INCLUDE,
    });
    if (!row) throw notFound("consultation_not_found", "no such consultation");
    return BillingService.adminRow(row);
  }

  private static readonly ADMIN_INCLUDE = {
    users: {
          select: {
            id: true,
            email: true,
            display_name: true,
            phone: true,
            created_at: true,
            // WordPress owns identity now; a lawyer picking this up wants to
            // know which account it is on the site they administer.
            wp_user_id: true,
            wp_role: true,
          },
        },
    payments: {
      orderBy: { created_at: "desc" as const },
      take: 1,
    },
  };

  private static adminRow(r: any): AdminConsultationRow {
    return {
      consultation_id: r.id,
      status: r.status,
      amount_cents: r.amount_cents,
      currency: r.currency,
      ai_session_id: r.ai_session_id,
      created_at: r.created_at,
      paid_at: r.paid_at,
      escalated: r.escalated_at !== null,
      escalation_error: r.escalation_error,
      chat_language: r.chat_language,
      escalation_summary: r.escalation_summary,
      user: r.users
        ? {
            id: r.users.id,
            email: r.users.email ?? null,
            display_name: r.users.display_name ?? null,
            phone: r.users.phone ?? null,
            joined_at: r.users.created_at,
            wp_user_id: r.users.wp_user_id ?? null,
            wp_role: r.users.wp_role ?? null,
          }
        : null,
      latest_payment: r.payments[0]
        ? {
            provider_txn_id: r.payments[0].provider_txn_id,
            payment_status: r.payments[0].status,
            payment_amount_cents: r.payments[0].amount_cents,
          }
        : null,
    };
  }

  /**
   * Aggregate stats for the dashboard header cards.
   */
  async stats(): Promise<DashboardStats> {
    const [total, paid, pending, failed] = await Promise.all([
      this.prisma.consultations.count(),
      this.prisma.consultations.count({ where: { status: "paid" } }),
      this.prisma.consultations.count({ where: { status: "pending" } }),
      this.prisma.payments.count({ where: { status: "failed" } }),
    ]);

    const revenueAgg = await this.prisma.payments.aggregate({
      _sum: { amount_cents: true },
      where: { status: "success" },
    });

    return {
      total_consultations: total,
      paid_consultations: paid,
      pending_consultations: pending,
      failed_payments: failed,
      total_revenue_cents: revenueAgg._sum.amount_cents ?? 0,
    };
  }
}

// ── Admin-specific output shapes ───────────────────────────────────────────

export interface AdminConsultationRow {
  consultation_id: string;
  status: string;
  amount_cents: number;
  currency: string;
  ai_session_id: string | null;
  created_at: Date;
  paid_at: Date | null;
  escalated: boolean;
  escalation_error: string | null;
  chat_language: string | null;
  escalation_summary: string | null;
  user: {
    id: string;
    email: string | null;
    display_name: string | null;
    phone: string | null;
    joined_at: Date;
    wp_user_id: number | null;
    wp_role: string | null;
  } | null;
  latest_payment: {
    provider_txn_id: string;
    payment_status: string;
    payment_amount_cents: number;
  } | null;
}

export interface DashboardStats {
  total_consultations: number;
  paid_consultations: number;
  pending_consultations: number;
  failed_payments: number;
  total_revenue_cents: number;
}

