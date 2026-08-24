/**
 * The database connection.
 *
 * Prisma 7 requires a driver adapter rather than a connection string in the
 * schema, which suits this project: the adapter here uses DATABASE_URL — the
 * application role, which owns nothing — while `prisma.config.ts` points the
 * CLI at DATABASE_MIGRATE_URL, the owner. The split means the running service
 * cannot alter its own tables even if a bug tries to.
 */

import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from "@nestjs/common";
import { PrismaPg } from "@prisma/adapter-pg";

import { PrismaClient } from "../../generated/prisma/client";

@Injectable()
export class PrismaService extends PrismaClient implements OnModuleInit, OnModuleDestroy {
  private static readonly log = new Logger(PrismaService.name);

  constructor() {
    super({
      adapter: new PrismaPg({ connectionString: process.env.DATABASE_URL }),
    });
  }

  async onModuleInit(): Promise<void> {
    // Connect at boot rather than lazily on the first request, so a bad
    // DATABASE_URL fails while someone is watching the logs instead of during
    // a user's first login.
    await this.$connect();
    PrismaService.log.log("database connected");
  }

  async onModuleDestroy(): Promise<void> {
    await this.$disconnect();
  }
}
