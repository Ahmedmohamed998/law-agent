import { Controller, Delete, Get, HttpCode, Param, Post, Query, UseGuards } from "@nestjs/common";

import { AdminKeyGuard } from "../auth/auth.guard";
import { ErasureService, type ErasureOut } from "./erasure.service";

/**
 * Service-to-service maintenance. Authenticated with a shared key, because the
 * caller is another service rather than a person.
 */
@Controller("admin")
@UseGuards(AdminKeyGuard)
export class ErasureAdminController {
  constructor(private readonly erasure: ErasureService) {}

  @Delete("users/:userId")
  @HttpCode(202)
  erase(@Param("userId") userId: string): Promise<ErasureOut> {
    return this.erasure.purgeUser(userId);
  }

  @Get("erasure")
  pending(): Promise<ErasureOut[]> {
    return this.erasure.pending();
  }

  @Post("erasure/retry")
  @HttpCode(200)
  retry(@Query("limit") limit?: string): Promise<ErasureOut[]> {
    return this.erasure.retry(limit ? Number(limit) : 50);
  }
}
