import { Controller, Get } from "@nestjs/common";

import { PrismaService } from "../prisma/prisma.service";
import { KeyService } from "../security/key.service";

@Controller()
export class HealthController {
  constructor(
    private readonly prisma: PrismaService,
    private readonly keys: KeyService,
  ) {}

  /** Liveness: the process is up. Says nothing about whether it can work. */
  @Get("healthz")
  healthz() {
    return { ok: true };
  }

  /**
   * Readiness: it can actually issue a token.
   *
   * A keyless process would accept traffic and fail every single login, which
   * is worse than being marked unready — so the signing key is checked, not
   * just the database. Point the load balancer here.
   */
  @Get("readyz")
  async readyz() {
    const active = await this.keys.active();
    await this.prisma.$queryRaw`SELECT 1`;
    return { ok: true, kid: active.kid };
  }
}
