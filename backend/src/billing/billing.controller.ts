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
import { IsInt, IsOptional, IsString, Length, Max, Min } from "class-validator";
import type { RawBodyRequest } from "@nestjs/common";
import type { Request } from "express";

import { AdminKeyGuard, AuthGuard, CurrentCaller, RegisteredGuard, type Caller } from "../auth/auth.guard";
import { BillingService, type AdminConsultationRow, type ConsultationOut, type DashboardStats } from "./billing.service";
import { PaymobService, type PaymobTransaction } from "./paymob.service";

export class CreateConsultationDto {
  /** The conversation a lawyer would take over. Optional: someone can buy a
   * consultation without having chatted first. */
  @IsOptional()
  @IsString()
  @Length(1, 32)
  ai_session_id?: string;

  // Bounded on both ends. No maximum means a typo can charge someone 500,000
  // EGP; no minimum means a zero-value order the gateway rejects confusingly.
  @IsInt()
  @Min(100)
  @Max(10_000_000)
  amount_cents!: number;

  @IsOptional()
  @IsString()
  @Length(3, 3)
  currency?: string;

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
  constructor(private readonly billing: BillingService) {}

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
      amountCents: body.amount_cents,
      currency: body.currency ?? "EGP",
      billing: {
        first_name: (body.full_name ?? "Client").split(" ")[0],
        last_name: (body.full_name ?? "Client").split(" ").slice(1).join(" ") || "Client",
        phone_number: body.phone ?? "+200000000000",
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

@Controller("admin/billing")
@UseGuards(AdminKeyGuard)
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
