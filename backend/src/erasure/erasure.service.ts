/**
 * Erasure: delete the user here, and make sure the AI service is told.
 *
 * The ordering is the whole design. Because `ai.sessions` has no foreign key
 * to `public.users`, deleting a user here cascades NOTHING there — and those
 * conversations contain dismissals, unpaid-wage disputes and salary details.
 *
 *   1. Delete the user and write an `erasure_requests` row FIRST, in one
 *      transaction. Once that commits, the instruction is durable: a crash
 *      costs a retry, never the record.
 *   2. THEN attempt the HTTP call, outside that transaction. A slow or dead
 *      AI service must not hold a database transaction open, and its failure
 *      must not roll back a deletion the user is entitled to.
 *
 * Doing it the other way — call first, delete second — loses the instruction
 * whenever the process dies between the two, and that is a silent compliance
 * failure rather than a loud one.
 */

import { Injectable, Logger } from "@nestjs/common";

import { AiService } from "../ai/ai.service";
import { newId } from "../common/ids";
import { PrismaService } from "../prisma/prisma.service";

// How many attempts before it needs a human. Erasure has a legal clock on it,
// so failing quietly forever is not an option — this is what makes a stuck one
// visible.
const MAX_ATTEMPTS = 10;

export interface ErasureOut {
  user_id: string;
  status: string;
  attempts: number;
  last_error: string | null;
}

@Injectable()
export class ErasureService {
  private static readonly log = new Logger(ErasureService.name);

  constructor(
    private readonly prisma: PrismaService,
    private readonly ai: AiService,
  ) {}

  async purgeUser(userId: string): Promise<ErasureOut> {
    // Step 1 — local delete and the durable instruction, atomically.
    const requestId = newId();
    await this.prisma.$transaction(async (tx) => {
      await tx.erasure_requests.create({
        data: { id: requestId, user_id: userId, status: "pending", attempts: 0 },
      });
      // Memberships, refresh tokens, consultations and payments all cascade.
      // The erasure_requests row deliberately does not — it has to outlive the
      // user it describes, which is why it carries no foreign key.
      await tx.users.deleteMany({ where: { id: userId } });
    });

    // Step 2 — the cross-service call, outside the transaction.
    const { delivered, error } = await this.ai.purgeUser(userId);

    const row = await this.prisma.erasure_requests.update({
      where: { id: requestId },
      data: {
        attempts: { increment: 1 },
        status: delivered ? "delivered" : "pending",
        delivered_at: delivered ? new Date() : null,
        last_error: delivered ? null : error,
      },
    });

    if (!delivered) {
      ErasureService.log.warn(`erasure pending for ${userId}: ${error}`);
    }

    return {
      user_id: row.user_id,
      status: row.status,
      attempts: row.attempts,
      last_error: row.last_error,
    };
  }

  /** Everything not yet confirmed delivered. Point a monitor at this. */
  async pending(limit = 100): Promise<ErasureOut[]> {
    const rows = await this.prisma.erasure_requests.findMany({
      where: { status: { not: "delivered" } },
      orderBy: { requested_at: "asc" },
      take: Math.min(limit, 500),
    });
    return rows.map((r) => ({
      user_id: r.user_id,
      status: r.status,
      attempts: r.attempts,
      last_error: r.last_error,
    }));
  }

  /**
   * Re-attempt delivery.
   *
   * Idempotent on the AI side by construction — purging an already-purged user
   * deletes nothing and returns success — so retrying is always safe.
   */
  async retry(limit = 50): Promise<ErasureOut[]> {
    const rows = await this.prisma.erasure_requests.findMany({
      where: { status: "pending" },
      orderBy: { requested_at: "asc" },
      take: Math.min(limit, 200),
    });

    const out: ErasureOut[] = [];
    for (const row of rows) {
      const { delivered, error } = await this.ai.purgeUser(row.user_id);
      const attempts = row.attempts + 1;
      const updated = await this.prisma.erasure_requests.update({
        where: { id: row.id },
        data: {
          attempts,
          status: delivered ? "delivered" : attempts >= MAX_ATTEMPTS ? "failed" : "pending",
          delivered_at: delivered ? new Date() : null,
          last_error: delivered ? null : error,
        },
      });
      out.push({
        user_id: updated.user_id,
        status: updated.status,
        attempts: updated.attempts,
        last_error: updated.last_error,
      });
    }
    return out;
  }
}
