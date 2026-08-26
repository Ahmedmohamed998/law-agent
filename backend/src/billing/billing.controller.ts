import {
  Body,
  Controller,
  Get,
  HttpCode,
  Param,
  Post,
  Query,
  Req,
  UseGuards,
} from "@nestjs/common";
import { IsOptional, IsString, Length } from "class-validator";
import type { RawBodyRequest } from "@nestjs/common";
import type { Request } from "express";

import { AdminKeyOrStaffGuard, AuthGuard, CurrentCaller, RegisteredGuard, type Caller } from "../auth/auth.guard";
import { BillingService, type AdminConsultationRow, type ConsultationOut, type DashboardStats } from "./billing.service";
import { PaymobService, type PaymobTransaction } from "./paymob.service";
import { loadConfig } from "../config/configuration";

/**
 * What the buyer gets to choose. Note what is absent: the price.
 *
 * `amount_cents` and `currency` used to be fields here, bounded but
 * client-supplied — so the amount charged was whatever the browser sent, and
 * anyone could open devtools and buy a 500 SAR consultation for 1.00. They now
 * come from configuration. Because the global ValidationPipe runs with
 * `forbidNonWhitelisted`, an old client still sending them gets a 422 that
 * names the field rather than a silently ignored price.
 */
export class CreateConsultationDto {
  /** The conversation a lawyer would take over. Optional: someone can buy a
   * consultation without having chatted first. */
  @IsOptional()
  @IsString()
  @Length(1, 32)
  ai_session_id?: string;

  @IsOptional()
  @IsString()
  @Length(1, 120)
  full_name?: string;

  @IsOptional()
  @IsString()
  @Length(3, 32)
  phone?: string;
}

@Controller("consultations")
@UseGuards(AuthGuard)
export class ConsultationsController {
  private readonly config = loadConfig();

  constructor(private readonly billing: BillingService) {}

  /**
   * What a consultation costs.
   *
   * Declared before `:id` so Nest does not route "price" into the lookup.
   *
   * The frontend needs a number to put on the button, and the only safe way
   * to give it one is to serve the same number the charge is built from. A
   * price rendered from the client's own config is a price that can disagree
   * with the invoice.
   *
   * Authenticated but not registered-only: an anonymous visitor sees the
   * offer before they have an account, which is the whole funnel.
   */
  @Get("price")
  price() {
    return {
      amount_cents: this.config.consultationPriceCents,
      currency: this.config.consultationCurrency,
    };
  }

  /**
   * Buy a consultation.
   *
   * Registered users only. An anonymous visitor can ask questions, but paying
   * for a lawyer's time needs an account to attach the result to.
   */
  @Post()
  @HttpCode(201)
  @UseGuards(RegisteredGuard)
  async create(@CurrentCaller() caller: Caller, @Body() body: CreateConsultationDto) {
    await this.billing.assertNoOpenConsultation(caller.userId, body.ai_session_id);

    const { consultation, paymentKey } = await this.billing.createConsultation({
      userId: caller.userId,
      aiSessionId: body.ai_session_id,
      // From configuration, never from the request.
      amountCents: this.config.consultationPriceCents,
      currency: this.config.consultationCurrency,
      billing: {
        first_name: (body.full_name ?? "Client").split(" ")[0],
        last_name: (body.full_name ?? "Client").split(" ").slice(1).join(" ") || "Client",
        // Paymob requires a phone; this filler is a Saudi number because the
        // account is ksa.paymob.com. It is only ever used when the caller
        // supplies none.
        phone_number: body.phone ?? "+966500000000",
        email: "billing@lawagent.local",
        // Paymob rejects missing address fields; "NA" is its documented filler.
        apartment: "NA", floor: "NA", street: "NA", building: "NA",
        shipping_method: "NA", postal_code: "NA", city: "NA",
        state: "NA", country: "NA",
      },
    });

    return { ...consultation, payment_key: paymentKey };
  }

  @Get()
  list(@CurrentCaller() caller: Caller): Promise<ConsultationOut[]> {
    return this.billing.listForUser(caller.userId);
  }

  @Get(":id")
  get(@CurrentCaller() caller: Caller, @Param("id") id: string): Promise<ConsultationOut> {
    return this.billing.getForUser(caller.userId, id);
  }
}

@Controller("webhooks")
export class WebhooksController {
  constructor(
    private readonly billing: BillingService,
    private readonly paymob: PaymobService,
  ) {}

  /**
   * Paymob transaction callback.
   *
   * NO AUTH GUARD, deliberately — the gateway has no user token and does not
   * know our users. The HMAC is the authentication, and it is checked before
   * anything in the payload is trusted.
   *
   * Always 200, even for a bad signature. A payment gateway that receives a
   * 4xx retries, and retrying a forged callback forever helps nobody; the
   * rejection is recorded in `webhook_events` where it can be looked at.
   */
  @Post("paymob")
  @HttpCode(200)
  async paymobCallback(
    @Req() req: RawBodyRequest<Request>,
    @Query("hmac") hmacQuery?: string,
  ) {
    const body = req.body as { obj?: PaymobTransaction; hmac?: string };
    const transaction = body?.obj;
    if (!transaction || transaction.id === undefined) {
      return { received: true, status: "ignored" };
    }

    const provided = hmacQuery ?? body.hmac ?? "";
    const valid = this.paymob.verifyHmac(
      transaction as unknown as Record<string, unknown>,
      provided,
    );

    if (!valid) {
      // Recorded, not processed. Anyone who finds this URL can reach it, so a
      // failed signature must change nothing.
      await this.billing.handleWebhookRejection(transaction, req.body);
      return { received: true, status: "invalid_signature" };
    }

    const result = await this.billing.handleWebhook(transaction, true, req.body);
    return { received: true, ...result };
  }
}

/**
 * The dashboard's read surface, plus the escalation retry.
 *
 * Reachable two ways (see AdminKeyOrStaffGuard): a service with the admin key,
 * or a signed-in owner/admin. The dashboard uses the second, so the shared key
 * never has to be handed to a browser.
 */
@Controller("admin/billing")
@UseGuards(AdminKeyOrStaffGuard)
export class BillingAdminController {
  constructor(private readonly billing: BillingService) {}

  /** Paid but never handed to a lawyer. The queue that must not grow. */
  @Get("unescalated")
  unescalated(): Promise<ConsultationOut[]> {
    return this.billing.unescalated();
  }

  @Post("retry-escalations")
  @HttpCode(200)
  retry(@Query("limit") limit?: string): Promise<ConsultationOut[]> {
    return this.billing.retryEscalations(limit ? Number(limit) : 50);
  }

  /**
   * Full consultation list for the admin dashboard.
   * Supports optional ?status=paid|pending and pagination.
   */
  @Get("consultations")
  listAll(
    @Query("status") status?: string,
    @Query("limit") limit?: string,
    @Query("offset") offset?: string,
  ): Promise<AdminConsultationRow[]> {
    return this.billing.listAll({
      status,
      limit: limit ? Number(limit) : undefined,
      offset: offset ? Number(offset) : undefined,
    });
  }

  /**
   * Aggregate dashboard stats: total, paid, pending, revenue.
   */
  @Get("stats")
  stats(): Promise<DashboardStats> {
    return this.billing.stats();
  }
}
