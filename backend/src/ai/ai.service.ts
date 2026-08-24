/**
 * The outbound half of the boundary: the two calls this service makes to the
 * AI service.
 *
 * Both exist because there is no foreign key between the two domains and
 * therefore nothing cascades or triggers across it. What replaces a database
 * constraint here is an HTTP call plus a durable record of whether it landed —
 * `erasure_requests.status` and `consultations.escalated_at`. A best-effort
 * call with no record is how you end up with a deleted user whose
 * conversations remain, or a paying client whose lawyer never received the
 * case.
 *
 * Authenticated with a shared admin key, not a user token: erasure is
 * cross-tenant by definition, and a payment webhook carries no user identity
 * at all.
 */

import { Injectable, Logger } from "@nestjs/common";

import { loadConfig } from "../config/configuration";

export interface DeliveryResult {
  delivered: boolean;
  error: string | null;
  data?: any;
}

@Injectable()
export class AiService {
  private static readonly log = new Logger(AiService.name);
  private readonly config = loadConfig();

  private get headers(): Record<string, string> {
    return {
      "X-Admin-Key": this.config.adminApiKey,
      "Content-Type": "application/json",
    };
  }

  private async call(
    method: "DELETE" | "POST",
    path: string,
    what: string,
  ): Promise<DeliveryResult> {
    const url = `${this.config.aiServiceUrl.replace(/\/+$/, "")}${path}`;
    try {
      const response = await fetch(url, {
        method,
        headers: this.headers,
        signal: AbortSignal.timeout(10_000),
      });

      if (response.ok) {
        try {
          const data = await response.json();
          return { delivered: true, error: null, data };
        } catch {
          return { delivered: true, error: null };
        }
      }

      if (response.status === 404) {
        // Both endpoints answer success for things that are already gone, so a
        // 404 means the ROUTE is missing — a wrong AI_SERVICE_URL, or a build
        // predating these endpoints. Never treat it as "already done": that
        // marks an undelivered instruction complete.
        return {
          delivered: false,
          error: `no ${what} endpoint at ${url} — check AI_SERVICE_URL`,
        };
      }

      const body = await response.text();
      return { delivered: false, error: `HTTP ${response.status}: ${body.slice(0, 300)}` };
    } catch (err) {
      return {
        delivered: false,
        error: `${(err as Error).name}: ${(err as Error).message}`.slice(0, 400),
      };
    }
  }

  /**
   * Delete everything the AI service holds for a user.
   *
   * Idempotent on that side — purging an already-purged user deletes nothing
   * and still returns 200 — so retrying after an ambiguous failure is safe.
   */
  async purgeUser(userId: string): Promise<DeliveryResult> {
    const result = await this.call("DELETE", `/v1/users/${userId}/data`, "erasure");
    if (!result.delivered) {
      AiService.log.warn(`erasure not delivered for ${userId}: ${result.error}`);
    }
    return result;
  }

  /**
   * Hand a conversation to a lawyer.
   *
   * This is what a paid consultation triggers. After it lands, every later
   * message on that session returns 409 and the model is never called again.
   * Idempotent, because payment gateways retry their webhooks.
   */
  async escalateSession(sessionId: string): Promise<DeliveryResult> {
    const result = await this.call(
      "POST",
      `/v1/admin/sessions/${sessionId}/escalate`,
      "escalation",
    );
    if (!result.delivered) {
      AiService.log.warn(`escalation not delivered for ${sessionId}: ${result.error}`);
    }
    return result;
  }
}
