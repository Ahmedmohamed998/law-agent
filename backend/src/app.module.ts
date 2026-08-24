import { Module } from "@nestjs/common";

import { AiModule } from "./ai/ai.module";
import { AuthModule } from "./auth/auth.module";
import { BillingModule } from "./billing/billing.module";
import { ErasureModule } from "./erasure/erasure.module";
import { HealthModule } from "./health/health.module";
import { OrgsModule } from "./orgs/orgs.module";
import { PrismaModule } from "./prisma/prisma.module";
import { SecurityModule } from "./security/security.module";
import { WellKnownModule } from "./wellknown/wellknown.module";

@Module({
  imports: [
    PrismaModule,
    SecurityModule,
    AiModule,
    WellKnownModule,
    AuthModule,
    OrgsModule,
    BillingModule,
    ErasureModule,
    HealthModule,
  ],
})
export class AppModule {}
