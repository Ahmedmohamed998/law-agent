import {
  Body,
  Controller,
  Get,
  HttpCode,
  Param,
  Patch,
  Post,
  Query,
  Req,
  UseGuards,
} from "@nestjs/common";
import {
  IsBoolean,
  IsIn,
  IsInt,
  IsOptional,
  IsString,
  Length,
  Matches,
  Max,
  Min,
} from "class-validator";
import type { RawBodyRequest } from "@nestjs/common";
import type { Request } from "express";

import {
  AdminKeyOrStaffGuard,
  AuthGuard,
  CurrentCaller,
  RegisteredGuard,
  bearerFrom,
  type Caller,
} from "../auth/auth.guard";
import { AiService, type StaffConversation } from "../ai/ai.service";
import { AppError } from "../common/errors";
import { BillingService, type AdminConsultationRow, type ConsultationOut, type DashboardStats } from "./billing.service";
import { PaymobService, type PaymobTransaction } from "./paymob.service";
import { ServicesService, type ServiceOut } from "./services.service";

/**
 * What the buyer gets to choose. Note what is absent: the price.
 *
 * `amount_cents` and `currency` used to be fields here, bounded but
 * client-supplied — so the amount charged was whatever the browser sent, and
 * anyone could open devtools and buy a 500 SAR consultation for 1.00. The
 * price now comes from the service row. Because the global ValidationPipe
 * runs with `forbidNonWhitelisted`, an old client still sending them gets a
 * 422 that names the field rather than a silently ignored price.
 */
export class CreateConsultationDto {
  /**
   * Which service. Id or slug. Optional for one release: a plugin from
   * before the catalogue existed sends nothing and means a consultation.
   */
  @IsOptional()
  @IsString()
  @Length(1, 64)
  service_id?: string;

  /** The client's description of the case, for services that ask for one. */
  @IsOptional()
  @IsString()
  @Length(0, 4000)
  client_notes?: string;

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

/** Every field optional so the same shape serves create and partial update. */
export class ServiceDto {
  @IsOptional()
  @IsString()
  @Matches(/^[a-z0-9]+(-[a-z0-9]+)*$/)
  @Length(2, 64)
  slug?: string;

  @IsOptional()
  @IsString()
  @Length(1, 160)
  name?: string;

  @IsOptional()
  @IsString()
  @Length(0, 4000)
  description?: string;

  @IsOptional()
  @IsInt()
  @Min(1)
  @Max(100_000_000)
  price_cents?: number;

  @IsOptional()
  @IsString()
  @Length(3, 3)
  currency?: string;

  @IsOptional()
  @IsBoolean()
  active?: boolean;

  @IsOptional()
  @IsInt()
  @Min(0)
  @Max(10_000)
  sort_order?: number;

  @IsOptional()
  @IsBoolean()
  needs_conversation?: boolean;

  @IsOptional()
  @IsBoolean()
  needs_notes?: boolean;

  @IsOptional()
  @IsString()
  @Length(0, 2000)
  ai_hint?: string;

  @IsOptional()
  @IsBoolean()
  suggestable?: boolean;
}

export class UpdateOrderStatusDto {
  @IsIn(["in_progress", "completed", "cancelled"])
  status!: string;
}

/**
 * The catalogue, as the site and the AI service read it.
 *
 * No guard, on purpose: a price list is public information, the site shows
 * it to visitors who have no token yet, and the AI service fetches it with
 * no user in hand. Nothing here can be changed through this controller.
 */
@Controller("services")
export class ServicesController {
  constructor(private readonly services: ServicesService) {}

  @Get()
  list(): Promise<ServiceOut[]> {
    return this.services.listPublic();
  }
}

@Controller("consultations")
@UseGuards(AuthGuard)
export class ConsultationsController {
  constructor(
    private readonly billing: BillingService,
    private readonly services: ServicesService,
  ) {}

  /**
   * What a consultation costs — the default service's price.
   *
   * Kept for the plugin release that predates the catalogue; new clients read
   * GET /services. Declared before `:id` so Nest does not route "price" into
   * the lookup.
   */
  @Get("price")
  async price() {
    const svc = await this.services.forPurchase(undefined);
    return { amount_cents: svc.price_cents, currency: svc.currency };
  }

  /**
   * Buy a service.
   *
   * Registered users only. An anonymous visitor can ask questions, but paying
   * for a lawyer's time needs an account to attach the result to.
   */
  @Post()
  @HttpCode(201)
  @UseGuards(RegisteredGuard)
  async create(@CurrentCaller() caller: Caller, @Body() body: CreateConsultationDto) {
    // The row the price comes from. Inactive or unknown stops here.
    const service = await this.services.forPurchase(body.service_id);

    if (service.needs_notes && !(body.client_notes ?? "").trim()) {
      throw new AppError(400, "notes_required", "this service needs a description of the case");
    }

    await this.billing.assertNoOpenConsultation(caller.userId, service.service_id, body.ai_session_id);

    const { consultation, paymentKey } = await this.billing.createConsultation({
      userId: caller.userId,
      service,
      aiSessionId: body.ai_session_id,
      clientNotes: body.client_notes,
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
  constructor(
    private readonly billing: BillingService,
    private readonly ai: AiService,
    private readonly servicesRepo: ServicesService,
  ) {}

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
   * Full order list for the admin dashboard.
   * Optional ?status=…, ?service=<id|slug>, and pagination.
   */
  @Get("consultations")
  listAll(
    @Query("status") status?: string,
    @Query("service") service?: string,
    @Query("limit") limit?: string,
    @Query("offset") offset?: string,
  ): Promise<AdminConsultationRow[]> {
    return this.billing.listAll({
      status,
      service,
      limit: limit ? Number(limit) : undefined,
      offset: offset ? Number(offset) : undefined,
    });
  }

  /** A lawyer moving the work along: in_progress, completed, or cancelled. */
  @Patch("consultations/:id/status")
  setStatus(
    @Param("id") id: string,
    @Body() body: UpdateOrderStatusDto,
  ): Promise<AdminConsultationRow> {
    return this.billing.updateStatus(id, body.status);
  }

  // ── the catalogue ──────────────────────────────────────────────────────

  @Get("services")
  services(): Promise<ServiceOut[]> {
    return this.servicesRepo.listAll();
  }

  @Post("services")
  @HttpCode(201)
  createService(@Body() body: ServiceDto): Promise<ServiceOut> {
    return this.servicesRepo.create(body);
  }

  @Patch("services/:id")
  updateService(@Param("id") id: string, @Body() body: ServiceDto): Promise<ServiceOut> {
    return this.servicesRepo.update(id, body);
  }

  /**
   * One case: the consultation, the client, the payment, and the conversation.
   *
   * The conversation lives in the AI service's schema, which this service's
   * database role cannot read — so it is fetched over HTTP, with the caller's
   * own token forwarded. If that fails the rest of the case still renders,
   * with the reason, rather than the whole page erroring.
   */
  @Get("consultations/:id")
  async getOne(
    @Param("id") id: string,
    @Req() req: Request,
  ): Promise<
    AdminConsultationRow & {
      conversation: StaffConversation | null;
      conversation_error: string | null;
    }
  > {
    const row = await this.billing.getForAdmin(id);
    if (!row.ai_session_id) {
      return { ...row, conversation: null, conversation_error: null };
    }
    const result = await this.ai.staffConversation(row.ai_session_id, bearerFrom(req));
    return result.ok
      ? { ...row, conversation: result.data, conversation_error: null }
      : { ...row, conversation: null, conversation_error: result.error };
  }

  /**
   * Aggregate dashboard stats: total, paid, pending, revenue.
   */
  @Get("stats")
  stats(): Promise<DashboardStats> {
    return this.billing.stats();
  }
}
