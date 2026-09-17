/**
 * The catalogue.
 *
 * One table, edited by staff in the dashboard, read by three consumers with
 * three different needs:
 *
 *   * the site, which shows active services with a price (`listPublic`);
 *   * the order flow, which needs the price of one service at the moment of
 *     purchase and refuses anything inactive (`forPurchase`);
 *   * the AI service, which reads the same public list to know what it may
 *     suggest, and does so over HTTP because its database role cannot see
 *     this schema.
 *
 * No delete. An order references the service it bought; removing the row
 * would orphan history. Deactivate instead.
 */

import { Injectable } from "@nestjs/common";

import { AppError, conflict, notFound } from "../common/errors";
import { newId } from "../common/ids";
import { PrismaService } from "../prisma/prisma.service";

export interface ServiceOut {
  service_id: string;
  slug: string;
  name: string;
  description: string;
  price_cents: number;
  currency: string;
  active: boolean;
  sort_order: number;
  needs_conversation: boolean;
  needs_notes: boolean;
  ai_hint: string;
  suggestable: boolean;
  updated_at: Date;
}

export interface ServiceInput {
  slug?: string;
  name?: string;
  description?: string;
  price_cents?: number;
  currency?: string;
  active?: boolean;
  sort_order?: number;
  needs_conversation?: boolean;
  needs_notes?: boolean;
  ai_hint?: string;
  suggestable?: boolean;
}

/** The service every order was, before there were others. */
export const DEFAULT_SERVICE_SLUG = "consultation";

const SLUG = /^[a-z0-9]+(-[a-z0-9]+)*$/;

@Injectable()
export class ServicesService {
  constructor(private readonly prisma: PrismaService) {}

  static shape(r: {
    id: string;
    slug: string;
    name_ar: string;
    description_ar: string;
    price_cents: number;
    currency: string;
    active: boolean;
    sort_order: number;
    needs_conversation: boolean;
    needs_notes: boolean;
    ai_hint: string;
    suggestable: boolean;
    updated_at: Date;
  }): ServiceOut {
    return {
      service_id: r.id,
      slug: r.slug,
      name: r.name_ar,
      description: r.description_ar,
      price_cents: r.price_cents,
      currency: r.currency,
      active: r.active,
      sort_order: r.sort_order,
      needs_conversation: r.needs_conversation,
      needs_notes: r.needs_notes,
      ai_hint: r.ai_hint,
      suggestable: r.suggestable,
      updated_at: r.updated_at,
    };
  }

  /** What the site offers. Active only, in display order. */
  async listPublic(): Promise<ServiceOut[]> {
    const rows = await this.prisma.services.findMany({
      where: { active: true },
      orderBy: [{ sort_order: "asc" }, { created_at: "asc" }],
    });
    return rows.map(ServicesService.shape);
  }

  /** Everything, for the dashboard. */
  async listAll(): Promise<ServiceOut[]> {
    const rows = await this.prisma.services.findMany({
      orderBy: [{ active: "desc" }, { sort_order: "asc" }, { created_at: "asc" }],
    });
    return rows.map(ServicesService.shape);
  }

  /**
   * The service an order is about to buy. Accepts an id or a slug, because
   * the site links by slug and the AI suggests by slug, while orders store
   * the id. Inactive is a 409, not a 404: the thing exists, it just is not
   * for sale — and a client who reaches this had a stale page.
   */
  async forPurchase(idOrSlug: string | undefined): Promise<ServiceOut> {
    const key = idOrSlug ?? DEFAULT_SERVICE_SLUG;
    const row = await this.prisma.services.findFirst({
      where: { OR: [{ id: key }, { slug: key }] },
    });
    if (!row) throw notFound("service_not_found", "no such service");
    if (!row.active) throw conflict("service_inactive", "this service is not offered at the moment");
    return ServicesService.shape(row);
  }

  async get(id: string): Promise<ServiceOut> {
    const row = await this.prisma.services.findUnique({ where: { id } });
    if (!row) throw notFound("service_not_found", "no such service");
    return ServicesService.shape(row);
  }

  async create(input: ServiceInput): Promise<ServiceOut> {
    const slug = (input.slug ?? "").trim();
    const name = (input.name ?? "").trim();
    if (!SLUG.test(slug)) throw invalid("slug must be lowercase letters, digits and hyphens");
    if (!name) throw invalid("name is required");
    if (!Number.isInteger(input.price_cents) || (input.price_cents as number) <= 0) {
      throw invalid("price_cents must be a positive integer");
    }
    const taken = await this.prisma.services.findUnique({ where: { slug } });
    if (taken) throw conflict("slug_taken", "a service with this slug already exists");

    const row = await this.prisma.services.create({
      data: {
        id: newId(),
        slug,
        name_ar: name,
        description_ar: (input.description ?? "").trim(),
        price_cents: input.price_cents as number,
        currency: (input.currency ?? "SAR").toUpperCase(),
        active: input.active ?? true,
        sort_order: input.sort_order ?? 100,
        needs_conversation: input.needs_conversation ?? false,
        needs_notes: input.needs_notes ?? false,
        ai_hint: (input.ai_hint ?? "").trim(),
        suggestable: input.suggestable ?? true,
      },
    });
    return ServicesService.shape(row);
  }

  /**
   * Partial update. The slug is immutable once created: URLs, suggestions
   * stored on messages and evaluation sets all refer to it.
   */
  async update(id: string, input: ServiceInput): Promise<ServiceOut> {
    const existing = await this.prisma.services.findUnique({ where: { id } });
    if (!existing) throw notFound("service_not_found", "no such service");
    if (input.slug !== undefined && input.slug !== existing.slug) {
      throw invalid("slug cannot be changed");
    }
    if (input.price_cents !== undefined &&
        (!Number.isInteger(input.price_cents) || input.price_cents <= 0)) {
      throw invalid("price_cents must be a positive integer");
    }
    if (input.name !== undefined && !input.name.trim()) throw invalid("name cannot be empty");

    const row = await this.prisma.services.update({
      where: { id },
      data: {
        ...(input.name !== undefined && { name_ar: input.name.trim() }),
        ...(input.description !== undefined && { description_ar: input.description.trim() }),
        ...(input.price_cents !== undefined && { price_cents: input.price_cents }),
        ...(input.currency !== undefined && { currency: input.currency.toUpperCase() }),
        ...(input.active !== undefined && { active: input.active }),
        ...(input.sort_order !== undefined && { sort_order: input.sort_order }),
        ...(input.needs_conversation !== undefined && { needs_conversation: input.needs_conversation }),
        ...(input.needs_notes !== undefined && { needs_notes: input.needs_notes }),
        ...(input.ai_hint !== undefined && { ai_hint: input.ai_hint.trim() }),
        ...(input.suggestable !== undefined && { suggestable: input.suggestable }),
        updated_at: new Date(),
      },
    });
    return ServicesService.shape(row);
  }
}

function invalid(message: string): AppError {
  return new AppError(400, "invalid_service", message);
}
